"""HTTP 接口层（开发规格 §10 / §13 的 P0 验收用例）。

覆盖：注册与防刷、登录锁定、上传文件头校验、页数上限、并发限制、60 秒去重、
越权访问、后台权限、人工充值确认（幂等）与驳回。
"""
from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

from app.db import q, q1, tx
from tests.conftest import make_user, png_bytes, upload


def register(client, username="alice", password="pass12345", **kw):
    return client.post(
        "/api/auth/register",
        json={"username": username, "password": password, "agree": True, **kw},
    )


def err(r) -> str:
    return r.json().get("code", "")


# ---------------------------------------------------------------- 注册
def test_register_requires_agreement(client):
    r = client.post("/api/auth/register", json={"username": "bob", "password": "pass12345"})
    assert r.status_code == 400
    assert err(r) == "E_NEED_AGREE"


def test_register_weak_password(client):
    r = client.post("/api/auth/register",
                    json={"username": "bob", "password": "123", "agree": True})
    assert r.status_code == 400
    assert err(r) == "E_WEAK_PWD"


def test_register_short_username(client):
    r = register(client, username="a")
    assert r.status_code == 400
    assert err(r) == "E_BAD_USERNAME"


def test_register_duplicate_username(client):
    assert register(client, "alice").status_code == 200
    r = register(client, "alice")
    assert r.status_code == 400
    assert err(r) == "E_USERNAME_TAKEN"


def test_register_gives_free_pages(client):
    r = register(client)
    assert r.status_code == 200
    d = r.json()["data"]
    assert d["free_pages_left"] == 5
    assert d["free_withheld"] is False
    assert d["balance_mill"] == 0


def test_register_sets_session_cookie(client):
    register(client)
    assert client.get("/api/auth/me").json()["data"]["username"] == "alice"


def test_register_withholds_free_after_device_limit(client):
    """同设备 24h 内第 3 个账号仍能注册，但不发免费额度（不误伤正常用户）。"""
    h = {"X-Device-Token": "dev-fixed-001"}
    for i in range(2):
        r = client.post(
            "/api/auth/register",
            json={"username": f"u{i}", "password": "pass12345", "agree": True},
            headers=h,
        )
        assert r.json()["data"]["free_withheld"] is False
        client.post("/api/auth/logout")

    r = client.post(
        "/api/auth/register",
        json={"username": "u2", "password": "pass12345", "agree": True},
        headers=h,
    )
    assert r.status_code == 200
    assert r.json()["data"]["free_withheld"] is True
    assert r.json()["data"]["free_pages_left"] == 0


# ---------------------------------------------------------------- 登录
def test_login_ok(client):
    register(client)
    client.post("/api/auth/logout")
    r = client.post("/api/auth/login", json={"username": "alice", "password": "pass12345"})
    assert r.status_code == 200


def test_login_wrong_password(client):
    register(client)
    client.post("/api/auth/logout")
    r = client.post("/api/auth/login", json={"username": "alice", "password": "wrong-pass"})
    assert r.status_code == 401
    assert err(r) == "E_BAD_CRED"


def test_login_locks_after_five_failures(client):
    register(client)
    client.post("/api/auth/logout")
    for _ in range(5):
        client.post("/api/auth/login", json={"username": "alice", "password": "bad"})
    r = client.post("/api/auth/login", json={"username": "alice", "password": "pass12345"})
    assert r.status_code == 429
    assert err(r) == "E_LOCKED"


def test_me_unauthenticated(client):
    r = client.get("/api/auth/me")
    assert r.status_code == 200
    assert r.json()["data"] is None


# ---------------------------------------------------------------- 上传
def test_upload_rejects_bad_extension(client):
    register(client)
    r = client.post("/api/upload/init", json={"filename": "virus.exe", "size": 1024})
    assert r.status_code == 400
    assert err(r) == "E_UNSUPPORTED"


def test_upload_rejects_empty(client):
    register(client)
    r = client.post("/api/upload/init", json={"filename": "a.png", "size": 0})
    assert r.status_code == 400
    assert err(r) == "E_BAD_SIZE"


def test_upload_detects_disguised_extension(client):
    """扩展名写 .pdf，内容其实是 PNG → 文件头校验必须拦下（EX-02）。"""
    register(client)
    blob = png_bytes()
    r = client.post("/api/upload/init", json={"filename": "fake.pdf", "size": len(blob)})
    up = r.json()["data"]["upload_id"]
    client.post(f"/api/upload/chunk?upload_id={up}&index=0",
                files={"file": ("blob", blob, "application/octet-stream")})
    r = client.post("/api/upload/complete", json={"upload_id": up})
    assert r.status_code == 400
    assert err(r) == "E_UNSUPPORTED"


def test_upload_incomplete_chunks(client):
    register(client)
    blob = png_bytes()
    r = client.post("/api/upload/init", json={"filename": "a.png", "size": len(blob) * 3})
    up = r.json()["data"]["upload_id"]
    r = client.post("/api/upload/complete", json={"upload_id": up})
    assert r.status_code == 400
    assert err(r) == "E_INCOMPLETE"


def test_upload_requires_login(env):
    from app.main import app

    with TestClient(app) as c:
        r = c.post("/api/upload/init", json={"filename": "a.png", "size": 100})
    assert r.status_code == 401


# ---------------------------------------------------------------- 建任务
def test_create_task_happy_path(client):
    register(client)
    up = upload(client)
    r = client.post("/api/tasks", json={"upload_id": up, "mode": "ancient"})
    assert r.status_code == 200
    d = r.json()["data"]
    assert d["status"] == "queued"
    assert d["mode"] == "ancient"
    assert d["estimate"]["free_pages_applied"] == 1
    assert d["estimate"]["estimated_amount_mill"] == 0


def test_create_task_bad_mode(client):
    register(client)
    up = upload(client)
    r = client.post("/api/tasks", json={"upload_id": up, "mode": "magic"})
    assert r.status_code == 400
    assert err(r) == "E_BAD_MODE"


def test_create_task_concurrent_limit(client):
    register(client)
    up1 = upload(client)
    assert client.post("/api/tasks", json={"upload_id": up1}).status_code == 200

    up2 = upload(client, data=png_bytes(1240, 1800))
    r = client.post("/api/tasks", json={"upload_id": up2})
    assert r.status_code == 409
    assert err(r) == "E_CONCURRENT"


def test_create_task_page_limit_low_balance(client, env):
    """余额 ≤¥1 时单次最多 10 页。"""
    import pymupdf

    register(client)
    doc = pymupdf.open()
    for _ in range(11):
        doc.new_page(width=595, height=842)
    buf = doc.tobytes()
    doc.close()

    up = upload(client, data=buf, filename="big.pdf")
    r = client.post("/api/tasks", json={"upload_id": up})
    assert r.status_code == 400
    assert err(r) == "E_PAGE_LIMIT"
    assert r.json()["detail"]["limit"] == 10


def test_create_task_dedup_within_60s(client):
    """同一文件 60 秒内重复提交 → E_DUP（防连点）。"""
    register(client)
    blob = png_bytes()
    up1 = upload(client, data=blob)
    r1 = client.post("/api/tasks", json={"upload_id": up1})
    assert r1.status_code == 200

    # 把第一个任务推到终态，绕过并发限制，只测去重
    tid = r1.json()["data"]["id"]
    with tx() as conn:
        from sqlalchemy import text

        conn.execute(text("UPDATE tasks SET status='settled' WHERE id=:i"), {"i": tid})

    up2 = upload(client, data=blob)
    r2 = client.post("/api/tasks", json={"upload_id": up2})
    assert r2.status_code == 409
    assert err(r2) == "E_DUP"


def test_create_task_needs_balance_when_free_withheld(client):
    """风控扣发免费额度且余额为 0 → 402 并给出可操作提示（不能 500）。"""
    register(client)
    me = client.get("/api/auth/me").json()["data"]
    with tx() as conn:
        from sqlalchemy import text

        conn.execute(text("UPDATE users SET free_pages_total=0 WHERE id=:i"), {"i": me["id"]})

    up = upload(client)
    r = client.post("/api/tasks", json={"upload_id": up, "mode": "ancient"})
    assert r.status_code == 402
    assert err(r) == "E_NO_BALANCE"
    assert "充值" in r.json()["message"]
    assert r.json()["detail"]["free_left"] == 0


# ---------------------------------------------------------------- 越权
def test_task_not_visible_to_others(env):
    from app.main import app

    with TestClient(app) as c1:
        register(c1, "owner")
        up = upload(c1)
        tid = c1.post("/api/tasks", json={"upload_id": up}).json()["data"]["id"]

    with TestClient(app) as c2:
        r = c2.post("/api/auth/register",
                    json={"username": "other", "password": "pass12345", "agree": True})
        assert r.status_code == 200
        r = c2.get(f"/api/tasks/{tid}")
        assert r.status_code in (403, 404)  # 别人的任务既不可读也不泄露存在性


def test_admin_endpoints_require_admin(client):
    register(client)
    for path in ("/admin/api/dashboard", "/admin/api/users", "/admin/api/integrity"):
        r = client.get(path)
        assert r.status_code in (401, 403), f"{path} 未拦住非管理员"


# ---------------------------------------------------------------- 充值
def _make_admin(name="admin1", password="adminpass123"):
    from app.security import hash_password
    from app.utils import new_id, now_iso

    uid = new_id()
    with tx() as conn:
        from sqlalchemy import text

        conn.execute(
            text(
                "INSERT INTO users(id,username,pwd_hash,role,balance_mill,free_pages_total,"
                "free_pages_used,status,created_at)"
                " VALUES(:i,:u,:p,'admin',0,0,0,'active',:c)"
            ),
            {"i": uid, "u": name, "p": hash_password(password), "c": now_iso()},
        )
    return uid


def test_recharge_rejects_custom_amount(client):
    register(client)
    r = client.post("/api/recharge/orders",
                    json={"amount_mill": 12_345, "pay_method": "wechat", "payer_note": "张三"})
    assert r.status_code == 400
    assert err(r) == "E_BAD_AMOUNT"


def test_recharge_requires_payer_note(client):
    register(client)
    r = client.post("/api/recharge/orders",
                    json={"amount_mill": 10_000, "pay_method": "wechat"})
    assert r.status_code == 400
    assert err(r) == "E_NO_NOTE"


def test_recharge_presets_only(client):
    register(client)
    r = client.get("/api/recharge/info")
    assert r.status_code == 200
    amounts = [p["amount_mill"] for p in r.json()["data"]["presets"]]
    assert amounts == [10_000, 30_000, 100_000, 300_000]


def test_recharge_confirm_credits_once(env):
    """管理员确认到账：余额 +1000 厘、写一条 recharge 台账；重复确认只入账一次。"""
    from app.main import app

    with TestClient(app) as cu:
        register(cu, "buyer")
        r = cu.post("/api/recharge/orders",
                    json={"amount_mill": 10_000, "pay_method": "wechat", "payer_note": "买家"})
        assert r.status_code == 200
        oid = r.json()["data"]["id"]
        uid = cu.get("/api/auth/me").json()["data"]["id"]

    _make_admin()
    with TestClient(app) as ca:
        ca.post("/api/auth/login", json={"username": "admin1", "password": "adminpass123"})
        r = ca.post(f"/admin/api/recharge/{oid}/confirm", json={})
        assert r.status_code == 200
        # 再点一次
        r2 = ca.post(f"/admin/api/recharge/{oid}/confirm", json={})
        assert r2.status_code == 409
        assert err(r2) == "E_ALREADY_DONE"

    bal = q1("SELECT balance_mill FROM users WHERE id=:u", u=uid)["balance_mill"]
    assert int(bal) == 10_000
    rows = q("SELECT * FROM ledger WHERE user_id=:u AND type='recharge'", u=uid)
    assert len(rows) == 1
    assert int(rows[0]["amount_mill"]) == 10_000


def test_recharge_confirm_no_body_ok(env):
    """前端「确认到账」不传 body（method: POST 无 json）——payload 声明成必填 dict
    时 FastAPI 会 422，管理员永远确认失败。payload 必须可省略。"""
    from app.main import app

    with TestClient(app) as cu:
        register(cu, "buyer_nb")
        r = cu.post("/api/recharge/orders",
                    json={"amount_mill": 10_000, "pay_method": "wechat", "payer_note": "买家"})
        assert r.status_code == 200
        oid = r.json()["data"]["id"]

    _make_admin()
    with TestClient(app) as ca:
        ca.post("/api/auth/login", json={"username": "admin1", "password": "adminpass123"})
        r = ca.post(f"/admin/api/recharge/{oid}/confirm")   # 不带 json body
        assert r.status_code == 200, r.text


def test_recharge_reject_needs_reason(env):
    from app.main import app

    with TestClient(app) as cu:
        register(cu, "buyer2")
        oid = cu.post("/api/recharge/orders",
                      json={"amount_mill": 10_000, "pay_method": "alipay",
                            "payer_note": "买家2"}).json()["data"]["id"]

    _make_admin("admin2", "adminpass123")
    with TestClient(app) as ca:
        ca.post("/api/auth/login", json={"username": "admin2", "password": "adminpass123"})
        r = ca.post(f"/admin/api/recharge/{oid}/reject", json={})
        assert r.status_code == 400


def test_recharge_pending_order_blocks_duplicate(env):
    from app.main import app

    with TestClient(app) as cu:
        register(cu, "buyer3")
        body = {"amount_mill": 10_000, "pay_method": "wechat", "payer_note": "买家3"}
        assert cu.post("/api/recharge/orders", json=body).status_code == 200
        r = cu.post("/api/recharge/orders", json=body)
        assert r.status_code == 409
        assert err(r) == "E_PENDING_EXISTS"


# ---------------------------------------------------------------- 元信息
def test_health_reports_components(client):
    r = client.get("/api/meta/health")
    assert r.status_code == 200
    d = r.json()["data"]
    assert d["db"]["ok"] is True
    assert "fonts" in d and "pdf_engine" in d and "ocr" in d


def test_config_pricing_text(client):
    r = client.get("/api/meta/config")
    assert r.status_code == 200
    d = r.json()["data"]
    assert "¥1" in d["price_text"]
    assert "1000" in d["min_bill_text"]
    assert d["free_pages"] == 5
    assert d["ttl_hours"] == 72


def test_billing_rules_match_pricing(client):
    register(client)
    r = client.get("/api/billing/rules")
    assert r.status_code == 200
    d = r.json()["data"]
    assert d["per_10k_mill"] == 1000
    assert "¥1" in d["per_10k_text"]
    assert d["min_bill_chars"] == 1000


# ------------------------------------------------ 免费额度的回环豁免（真实事故回归）
def _seed_marks(ip: str, n: int) -> None:
    """往 register_marks 塞 n 条 24h 内的注册记录，模拟配额被占满。"""
    from sqlalchemy import text

    from app.utils import now_iso

    with tx() as conn:
        for i in range(n):
            conn.execute(
                text(
                    "INSERT INTO register_marks(device_hash,ip,user_id,created_at)"
                    " VALUES(:d,:i,:u,:c)"
                ),
                {"d": f"seed-dev-{ip}-{i}", "i": ip, "u": f"seed-{i}", "c": now_iso()},
            )


def test_register_loopback_ip_never_withholds_free(client, monkeypatch):
    """回环地址不计入防刷统计。

    真实事故：自测从 127.0.0.1 注册了 30 个账号，IP 配额（5/24h）被打满，
    之后**真人在本机注册也被判超限、免费额度为 0**。本机请求不可能来自真实用户，
    所以必须豁免，否则开发机永远注册不出带额度的账号。
    """
    monkeypatch.setattr("app.routers.auth.client_ip", lambda request: "127.0.0.1")
    _seed_marks("127.0.0.1", 10)  # 远超 ip_account_limit=5

    r = register(client, username="loopbackuser")
    assert r.status_code == 200
    d = r.json()["data"]
    assert d["free_withheld"] is False
    assert d["free_pages_total"] == 5
    assert d["free_pages_left"] == 5
    assert d["risk_flag"] == 0


def test_register_public_ip_over_limit_still_withholds(client, monkeypatch):
    """豁免只针对回环——公网 IP 超限照旧扣发，别把防刷整体放开。"""
    monkeypatch.setattr("app.routers.auth.client_ip", lambda request: "203.0.113.9")
    _seed_marks("203.0.113.9", 5)  # 等于 ip_account_limit=5，触发

    r = register(client, username="pubuser")
    assert r.status_code == 200
    d = r.json()["data"]
    assert d["free_withheld"] is True
    assert d["withhold_reason"] == "ip"
    assert d["free_pages_total"] == 0
    assert d["free_pages_left"] == 0


def test_login_page_explains_withheld_free():
    """扣发额度必须在前端明说。

    后端早就返回了 free_withheld/withhold_reason，但前端从未消费，
    用户只看到「免费额度剩 0 页」，只能判断为系统坏了。
    """
    from pathlib import Path

    html = (Path(__file__).resolve().parent.parent / "app/templates/login.html").read_text("utf-8")
    assert "free_withheld" in html, "注册后未消费后端扣发标记"
    assert "未发放免费额度" in html, "缺少扣发文案"


# ---------------------------------------------------------------- 后台用户管理
def test_admin_user_list_reports_total_for_pagination(client):
    """用户列表必须回 total。

    前端本来就是 limit=50 一把梭，没有总数就没法翻页——用户超过 50 个之后，
    第 51 个以后的人在后台彻底消失，查不到也管不了。
    """
    _make_admin("pgadmin", "adminpass123")
    client.post("/api/auth/login", json={"username": "pgadmin", "password": "adminpass123"})
    for i in range(3):
        make_user(f"pguser{i}")

    d = client.get("/admin/api/users?limit=200&offset=0").json()["data"]
    assert "total" in d and d["total"] == len(d["items"]) >= 4

    p1 = client.get("/admin/api/users?limit=2&offset=0").json()["data"]
    p2 = client.get("/admin/api/users?limit=2&offset=2").json()["data"]
    assert p1["total"] == p2["total"] == d["total"]
    assert len(p1["items"]) == len(p2["items"]) == 2
    assert not ({i["id"] for i in p1["items"]} & {i["id"] for i in p2["items"]}), "两页数据重叠"
    assert all("reg_ip" in i for i in p1["items"]), "缺少注册来源，排查刷号/误伤时用得上"


def test_admin_cannot_disable_self(client):
    """管理员停用自己 = 后台立刻锁死（下一次请求就 401），必须拦住。"""
    uid = _make_admin("selfadmin", "adminpass123")
    client.post("/api/auth/login", json={"username": "selfadmin", "password": "adminpass123"})

    r = client.post(f"/admin/api/users/{uid}/status", json={"status": "disabled"})
    assert r.status_code == 400
    assert err(r) == "E_SELF_DISABLE"
    assert client.get("/admin/api/dashboard").status_code == 200, "自己被停用后仍应可用"


def test_admin_can_disable_normal_user(client):
    _make_admin("supadmin", "adminpass123")
    client.post("/api/auth/login", json={"username": "supadmin", "password": "adminpass123"})
    target = make_user("badguy")

    r = client.post(f"/admin/api/users/{target}/status", json={"status": "disabled"})
    assert r.status_code == 200
    assert q1("SELECT status FROM users WHERE id=:i", i=target)["status"] == "disabled"




# ---------------------------------------------------------------- 页面鉴权（SSR 层）
def test_admin_page_redirects_anonymous_to_login(client):
    """后台页面本身也要拦住未登录用户。

    数据接口一直是 401 的，但页面裸奔时匿名用户能把后台结构看个全 ——
    而且普通用户误入后是一堆空壳，看着像站点坏了。
    """
    r = client.get("/admin", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"].startswith("/login")
    assert "next=/admin" in r.headers["location"]


def test_admin_page_kicks_normal_user_home(client):
    client.post(
        "/api/auth/register",
        json={"username": "normalone", "password": "pass12345", "agree": True},
    )
    r = client.get("/admin", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/"


def test_account_and_task_pages_require_login(client):
    for path in ("/account", "/task/whatever"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 302, path
        assert r.headers["location"].startswith("/login"), path


# ---------------------------------------------------------------- 管理员看用户任务
def _admin_task_fixture(client):
    """造一个「普通用户的任务」+ 一个登录好的管理员，返回 (task_id, owner_id)。"""
    from tests.conftest import make_task, make_user

    owner = make_user("taskowner")
    tid = make_task(owner, status="processing", chars=120)
    _make_admin("taskadmin", "adminpass123")
    client.post("/api/auth/login", json={"username": "taskadmin", "password": "adminpass123"})
    return tid, owner


def test_admin_can_read_any_task(client):
    """后台任务列表里每个任务号都是 /task/{id} 链接，点开必须能看。

    这里曾写死 `admin=False`，结果后台点任务号是页面 200、数据全 403 的空白页。
    """
    tid, _ = _admin_task_fixture(client)

    r = client.get(f"/api/tasks/{tid}")
    assert r.status_code == 200, r.text
    assert r.json()["data"]["id"] == tid
    assert client.get(f"/api/tasks/{tid}/exports").status_code == 200
    assert client.get(f"/api/tasks/{tid}/pages/1/blocks").status_code == 200
    assert client.get(f"/api/tasks/{tid}/pages/1/image").status_code == 200
    assert client.get(f"/api/tasks/{tid}/preview").status_code == 200


def test_admin_cannot_cancel_or_retry_other_task(client):
    """写操作不给 admin 豁免：误取消、替用户重试导致重复扣费都不可撤销。"""
    tid, _ = _admin_task_fixture(client)

    r = client.post(f"/api/tasks/{tid}/cancel")
    assert r.status_code == 403, r.text
    r = client.post(f"/api/tasks/{tid}/pages/1/retry")
    assert r.status_code == 403, r.text

    from app.db import q1

    assert q1("SELECT status FROM tasks WHERE id=:i", i=tid)["status"] == "processing"


def test_normal_user_still_cannot_read_others_task(client):
    """开了 admin 豁免之后，普通用户之间的隔离不能松。"""
    from tests.conftest import make_task, make_user

    owner = make_user("isolated_owner")
    tid = make_task(owner, status="processing")

    client.post(
        "/api/auth/register",
        json={"username": "nosy", "password": "pass12345", "agree": True},
    )
    assert client.get(f"/api/tasks/{tid}").status_code == 403
    assert client.get(f"/api/tasks/{tid}/pages/1/image").status_code == 403
    assert client.post(f"/api/tasks/{tid}/cancel").status_code == 403


# ---------------------------------------------------------------- OCR 配置
def test_ocr_settings_admin_only(client):
    """OCR token 配置只能管理员改；普通用户 403。"""
    register(client, "notadmin_ocr")
    r = client.post("/admin/api/settings/ocr", json={"token": "x"})
    assert r.status_code == 403, r.text


def test_ocr_settings_saves_and_masks_token(env):
    """后台改星河 token：入库生效、返回与审计日志都只露尾 4 位，不落明文。"""
    from app.main import app
    from app.db import get_setting, q1

    _make_admin("ocradmin", "adminpass123")
    with TestClient(app) as ca:
        ca.post("/api/auth/login", json={"username": "ocradmin", "password": "adminpass123"})
        r = ca.post("/admin/api/settings/ocr", json={"token": "secret-token-abcdef123456"})
        assert r.status_code == 200, r.text
        # 返回脱敏
        assert r.json()["data"]["aistudio_token"] == "…3456"
        # 库里是明文（provider 要用），但审计日志脱敏
        assert get_setting("aistudio_token", "") == "secret-token-abcdef123456"
        row = q1("SELECT before_value FROM admin_logs WHERE action='ocr_setting' ORDER BY rowid DESC LIMIT 1")
        assert "secret-token-abcdef123456" not in (row["before_value"] or ""), "审计日志不能落明文 token"
        assert "…3456" in (row["before_value"] or "")
        # 清空后回退（settings 表值被清）
        ca.post("/admin/api/settings/ocr", json={"token": ""})
        assert get_setting("aistudio_token", "") == ""


def test_ocr_token_resolution_prefers_db(env):
    """_resolve_token 优先 settings 表，回退 .env。"""
    from app.services.ocr.xinghe import _resolve_token
    from app.db import set_setting

    set_setting("aistudio_token", "db-token-xyz")
    try:
        assert _resolve_token() == "db-token-xyz"
    finally:
        set_setting("aistudio_token", "")
