"""边界探测：专挑畸形输入，看接口是否给出正确的 4xx 而不是 5xx 或误放行。

pytest 覆盖的是「已知行为」，这里覆盖的是「没想到的输入」：
负数页号、超长文件名、SQL 注入式用户名、非法枚举值、越权拿别人的文件……
判断标准只有一条：**要么明确拒绝（4xx + 错误码），要么正常处理，绝不许 5xx。**

用法：
  ADMIN_PWD=ma .venv/bin/python tools/probe_edge.py
退出码 0 = 无 5xx。
"""
from __future__ import annotations

import io
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
BASE = os.environ.get("BASE", "http://127.0.0.1:8000").rstrip("/")
ADMIN_PWD = os.environ.get("ADMIN_PWD", "")

results: list[tuple[str, int, str]] = []


def rec(name: str, r: httpx.Response) -> None:
    body = ""
    try:
        j = r.json()
        body = str(j.get("code") or j.get("ok"))
    except Exception:  # noqa: BLE001
        body = r.text[:40].replace("\n", " ")
    results.append((name, r.status_code, body))


def png_bytes(w: int = 240, h: int = 320) -> bytes:
    im = Image.new("RGB", (w, h), "white")
    d = ImageDraw.Draw(im)
    for i in range(6):
        d.text((10, 14 + i * 18), "测试文字", fill="black")
    b = io.BytesIO()
    im.save(b, "PNG")
    return b.getvalue()


def main() -> int:
    db = ROOT / "data" / "app.db"
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    task = c.execute("select id, user_id from tasks order by created_at desc limit 1").fetchone()
    other_task = c.execute(
        "select id from tasks where user_id != ? limit 1", (task["user_id"],)
    ).fetchone()
    file_row = c.execute("select id from files order by rowid desc limit 1").fetchone()
    c.close()

    tid = task["id"]
    other_tid = other_task["id"] if other_task else ""

    anon = httpx.Client(base_url=BASE, timeout=60)
    admin = httpx.Client(base_url=BASE, timeout=60)
    if ADMIN_PWD:
        admin.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PWD})

    # ---------- 页号越界 ----------
    for pno in ("0", "-1", "999999", "abc", "1.5"):
        r = admin.get(f"/api/tasks/{tid}/pages/{pno}/blocks")
        rec(f"blocks page_no={pno}", r)
        r = admin.get(f"/api/tasks/{tid}/pages/{pno}/image")
        rec(f"image page_no={pno}", r)

    # ---------- 计费参数 ----------
    for q in (
        "pages=0&mode=std", "pages=-5&mode=std", "pages=abc&mode=std",
        "pages=1&mode=xyz", "pages=99999999&mode=ancient", "",
    ):
        r = admin.get(f"/api/billing/estimate?{q}")
        rec(f"estimate?{q or '(空)'}", r)

    # ---------- 越权拿别人的任务/文件 ----------
    if other_tid:
        r = admin.get(f"/api/tasks/{other_tid}")
        rec("admin 读他人任务", r)
        r = admin.get(f"/api/tasks/{other_tid}/preview")
        rec("admin 读他人预览", r)
    if file_row:
        r = anon.get(f"/api/exports/{file_row['id']}/download")
        rec("匿名下载导出文件", r)

    # ---------- 导出非法 kind ----------
    r = admin.post(f"/api/tasks/{tid}/exports", json={"kind": "../etc/passwd"})
    rec("导出 kind 路径穿越", r)
    r = admin.post(f"/api/tasks/{tid}/exports", json={"kind": ""})
    rec("导出 kind 空", r)
    r = admin.post(f"/api/tasks/{tid}/exports", json={})
    rec("导出 无 kind", r)

    # ---------- 上传畸形 ----------
    r = admin.post("/api/upload/init", json={"filename": "a" * 500 + ".png", "size": 1024})
    rec("上传 超长文件名", r)
    r = admin.post("/api/upload/init", json={"filename": "x.png", "size": -1})
    rec("上传 负大小", r)
    r = admin.post("/api/upload/init", json={"filename": "x.exe", "size": 1024})
    rec("上传 exe 扩展名", r)
    r = admin.post("/api/upload/init", json={})
    rec("上传 空 body", r)

    # ---------- 登录畸形 ----------
    for body in (
        {"username": "admin' OR 1=1 --", "password": "x"},
        {"username": "", "password": ""},
        {"username": "a" * 300, "password": "b" * 300},
        {"username": None, "password": None},
    ):
        r = anon.post("/api/auth/login", json=body)
        rec(f"登录 {str(body)[:44]}", r)

    # ---------- 注册畸形 ----------
    r = anon.post("/api/auth/register", json={"username": "a", "password": "1", "agree": True})
    rec("注册 超短用户名+弱密码", r)
    r = anon.post("/api/auth/register", json={"username": "ok_name", "password": "12345678", "agree": False})
    rec("注册 未同意协议", r)
    r = anon.post("/api/auth/register", json={"username": "ok_name", "password": "12345678", "agree": True, "email": "not-an-email"})
    rec("注册 非法邮箱", r)

    # ---------- 充值畸形（参数名按真实 schema：amount_mill / payer_note）----------
    r = admin.post("/api/recharge/orders", json={"amount_mill": 0, "payer_note": "x"})
    rec("充值 0 元", r)
    r = admin.post("/api/recharge/orders", json={"amount_mill": -100, "payer_note": "x"})
    rec("充值 -1 元", r)
    r = admin.post("/api/recharge/orders", json={"amount_mill": 10000, "payer_note": "x" * 500})
    rec("充值 超长付款人", r)
    r = admin.post("/api/recharge/orders", json={"amount_mill": 1000})
    rec("充值 缺付款人", r)
    r = admin.post("/api/recharge/orders", json={"amount_mill": 10000, "payer_note": "x", "pay_method": "bitcoin"})
    rec("充值 非法付款方式", r)
    r = admin.post("/api/recharge/orders", json={"amount_mill": 10000, "payer_note": "x", "paid_at": "y" * 5000})
    rec("充值 超长时间戳", r)
    r = admin.post("/api/recharge/orders", json={"amount_mill": 10000, "payer_note": "x", "proof_url": "z" * 5000})
    rec("充值 超长凭证URL", r)

    # ---------- 取消别人的任务 ----------
    if other_tid:
        r = admin.post(f"/api/tasks/{other_tid}/cancel")
        rec("admin 取消他人任务", r)
    r = admin.post("/api/tasks/not-a-real-id/cancel")
    rec("取消 不存在任务", r)

    # ---------- 密码重置畸形 ----------
    r = anon.post("/api/auth/password/reset", json={"token": "x" * 200, "password": "abcdefgh"})
    rec("重置 伪造 token", r)
    r = anon.post("/api/auth/password/reset-request", json={"username": "不存在的用户"+str(int(time.time()))})
    rec("重置申请 不存在用户", r)

    # ---------- 真上传一个畸形文件 ----------
    r = admin.post("/api/upload/init", json={"filename": "bad.png", "size": 11})
    if r.status_code == 200:
        j = r.json().get("data") or {}
        up = j.get("upload_id") or j.get("id")
        if up:
            files = {"file": ("bad.png", b"not-an-image", "image/png")}
            rr = admin.post("/api/upload/chunk", data={"upload_id": up, "index": "0", "total": "1"}, files=files)
            rec("上传 伪装 png 的垃圾", rr)
            rr = admin.post("/api/upload/complete", json={"upload_id": up})
            rec("上传 完成垃圾文件", rr)

    # ---------- 输出 ----------
    print(f"BASE={BASE}\n")
    w = max(len(n) for n, _, _ in results) + 2
    print(f"{'case'.ljust(w)}{'code':>6}  body")
    print("-" * (w + 24))
    bad = []
    for name, code, body in results:
        flag = ""
        if code >= 500:
            flag = "  <<< 5xx!"
            bad.append(f"{name} -> {code} {body}")
        elif code == 200 and ("登录" in name or "注册" in name or "重置" in name):
            flag = "  (放行)"
        print(f"{name.ljust(w)}{code:>6}  {body}{flag}")

    print()
    if bad:
        print(f"发现 {len(bad)} 个 5xx：")
        for b in bad:
            print("  " + b)
        return 1
    print("全部通过：无 5xx。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
