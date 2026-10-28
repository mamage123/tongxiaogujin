"""测试夹具。

三条约定：
1. 每个测试用独立的临时库与临时存储目录 —— 资金测试绝不能污染开发库。
2. 全程 OCR_MOCK=1 —— 不打真实供应商（省钱、确定、可重复）。
3. 环境变量必须在导入 app.* 之前设好；reload_settings() 是原地改写单例，
   所以后续 `from app.config import settings` 的模块都能看到新值。
"""
from __future__ import annotations

import io
import os
import sys
import tempfile
from pathlib import Path

import pytest

# 给本次会话一个**全新的**临时根目录。
#
# 为什么不能直接用系统临时目录：沙箱里 `getpass.getuser()` 取不到用户名，
# pytest 会退化成 `pytest-of-root` / `pytest-of-unknown`，而宿主把 mkdir 代理给
# 主进程后，对**已存在**的目录直接抛 `PermissionError: EEXIST`（不吃 exist_ok）。
# 于是第二次跑测试就 104 条 setup ERROR。
# `mkdtemp` 每次给一个不存在的路径，pytest 只需新建，永远碰不到第二次。
tempfile.tempdir = tempfile.mkdtemp(prefix="pyguji-")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("OCR_MOCK", "1")
os.environ.setdefault("APP_SECRET", "test-secret")
os.environ.setdefault("ADMIN_INIT_PWD", "admin-test-pwd")

from app.config import reload_settings  # noqa: E402
from app.db import init_db, q1, reset_engine, tx  # noqa: E402


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """把配置指向临时目录，并建好 schema。返回 settings。"""
    db = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}")
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path / "files"))
    monkeypatch.setenv("OCR_MOCK", "1")
    monkeypatch.setenv("APP_SECRET", "test-secret")
    s = reload_settings()
    reset_engine()
    init_db(seed=True)
    yield s
    reset_engine()


@pytest.fixture()
def client(env):
    """未登录的 HTTP 客户端（跑在内存里，不起端口）。"""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture()
def user(client):
    """注册一个普通用户，返回 (client, user_dict)。"""
    r = client.post(
        "/api/auth/register",
        json={"username": "tester", "password": "pass12345", "agree": True},
    )
    assert r.status_code == 200, r.text
    return client, r.json()["data"]


# ---------------------------------------------------------------- 造数据
def make_user(username: str = "u1", password: str = "pass12345", balance_mill: int = 0) -> str:
    """直接建用户（绕过 HTTP），balance 走 credit 保证台账平衡。"""
    from app.security import hash_password
    from app.services.billing import credit
    from app.utils import new_id, now_iso

    uid = new_id()
    with tx() as conn:
        from sqlalchemy import text

        conn.execute(
            text(
                "INSERT INTO users(id,username,pwd_hash,role,balance_mill,free_pages_total,"
                "free_pages_used,status,created_at) VALUES(:i,:u,:p,'user',0,5,0,'active',:c)"
            ),
            {"i": uid, "u": username, "p": hash_password(password), "c": now_iso()},
        )
        if balance_mill:
            credit(conn, uid, balance_mill, ref_type="test_seed", ref_id=uid,
                   remark="测试初始余额", ltype="adjust")
    return uid


def make_task(user_id: str, *, chars: int = 0, status: str = "queued", mode: str = "ancient",
              estimated_chars: int = 400, free_pages_applied: int = 0,
              amount_mill: int = 0, pending_mill: int = 0) -> str:
    """直接建一个任务 + 一页（含 1 个块），用于计费/导出测试。"""
    from sqlalchemy import text

    from app.services.ocr.base import INSERT_BLOCK_SQL, OcrBlock
    from app.utils import hours_later, new_id, now_iso

    tid = new_id()
    with tx() as conn:
        conn.execute(
            text(
                "INSERT INTO tasks(id,user_id,filename,file_hash,file_type,pages_total,page_range,mode,"
                "optimize,status,chars,estimated_chars,amount_mill,pending_mill,free_pages_applied,"
                "is_rerun,rerun_free,source_path,created_at,expire_at,dedup_bucket)"
                " VALUES(:id,:u,'t.png',:h,'image',1,'all',:m,1,:s,:c,:ec,:am,:pm,:fp,0,0,NULL,:ca,:e,:b)"
            ),
            {
                "id": tid, "u": user_id, "h": f"hash-{tid}", "m": mode, "s": status,
                "c": chars, "ec": estimated_chars, "am": amount_mill, "pm": pending_mill,
                "fp": free_pages_applied, "ca": now_iso(), "e": hours_later(72), "b": "b0",
            },
        )
        conn.execute(
            text(
                "INSERT INTO task_pages(task_id,page_no,source_page_no,is_split_half,status,chars,"
                "retry_count,order_check_passed,img_width,img_height,image_path)"
                " VALUES(:t,1,1,0,'done',:c,0,1,1240,1754,:img)"
            ),
            {"t": tid, "c": chars, "img": page_image(tid, 1)},
        )
        block = OcrBlock(
            block_id="b1", label="title", order=0, text="欽定四庫全書",
            bbox=(100, 100, 400, 200), polygon=[(100, 100), (400, 100), (400, 200), (100, 200)],
        )
        conn.execute(text(INSERT_BLOCK_SQL), {"t": tid, "p": 1, **block.to_row()})
        block2 = OcrBlock(
            block_id="b2", label="text", order=1, text="易曰觀乎天文以察時變，日月星辰麗乎天者也。",
            bbox=(100, 240, 1100, 320),
        )
        conn.execute(text(INSERT_BLOCK_SQL), {"t": tid, "p": 1, **block2.to_row()})
    return tid


def page_image(task_id: str, page_no: int) -> str:
    """给页落一张真实 PNG 并返回绝对路径。

    校对版 PDF 打的是 `file://` 绝对路径（Chromium 加载的是本地 HTML，
    相对路径解析不到站内路由）。夹具必须造出真图，否则测不到这条路径。
    """
    from app.config import settings

    p = Path(settings.storage_dir) / "pages" / task_id / f"p{page_no}.png"
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        p.write_bytes(png_bytes())
    return str(p)


def add_page(task_id: str, page_no: int, chars: int = 0, status: str = "done") -> None:
    """给已有任务补一页（计费需要多页来验证「前 N 页免费」）。"""
    from sqlalchemy import text

    img = page_image(task_id, page_no)
    with tx() as conn:
        conn.execute(
            text(
                "INSERT INTO task_pages(task_id,page_no,source_page_no,is_split_half,status,chars,"
                "retry_count,order_check_passed,img_width,img_height,image_path)"
                " VALUES(:t,:p,:p,0,:s,:c,0,1,1240,1754,:img)"
            ),
            {"t": task_id, "p": page_no, "s": status, "c": chars, "img": img},
        )


def png_bytes(w: int = 1240, h: int = 1754) -> bytes:
    from PIL import Image

    im = Image.new("RGB", (w, h), (250, 246, 238))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def upload(client, data: bytes | None = None, filename: str = "s.png") -> str:
    """走完整上传链路，返回 upload_id。"""
    blob = data if data is not None else png_bytes()
    r = client.post("/api/upload/init", json={"filename": filename, "size": len(blob)})
    assert r.status_code == 200, r.text
    up = r.json()["data"]["upload_id"]
    r = client.post(
        f"/api/upload/chunk?upload_id={up}&index=0",
        files={"file": ("blob", blob, "application/octet-stream")},
    )
    assert r.status_code == 200, r.text
    r = client.post("/api/upload/complete", json={"upload_id": up})
    assert r.status_code == 200, r.text
    return up


def user_balance(uid: str) -> int:
    row = q1("SELECT balance_mill FROM users WHERE id=:i", i=uid)
    return int(row["balance_mill"])
