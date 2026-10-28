"""清理与收尸（FR-SYS-01 / FR-RES-06）。

这一组此前一条测试都没有，而它做的是**删文件**和**改状态**两件最危险的事。
两个真实隐患：
1. cleanup_expired 只排除了 expired，没排除还在跑的任务 ——
   任务到期那一刻正好在识别，目录会被整个删掉。
2. expire_stale_running 定义了却从没被调用过（兜底等于没做），
   而且判据用 created_at + 固定 6 小时：实测 1.5 分钟/页，
   200 页要 5 小时，OCR 稍慢就会把「一直在推进」的大任务当僵尸杀掉。
"""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import text

from app.db import q1, tx
from app.services.cleanup import cleanup_expired, expire_stale_running
from app.utils import hours_later, now_iso

from .conftest import make_task, make_user


def _set_task(tid: str, **kw) -> None:
    """改任务字段（SQL 列名直接拼，测试内部用，值全部走参数）。"""
    cols = ", ".join(f"{k}=:{k}" for k in kw)
    with tx() as conn:
        conn.execute(text(f"UPDATE tasks SET {cols} WHERE id=:id"), {**kw, "id": tid})


def _set_page_activity(tid: str, *, started: str | None = None, finished: str | None = None) -> None:
    with tx() as conn:
        conn.execute(
            text("UPDATE task_pages SET started_at=:s, finished_at=:f WHERE task_id=:t"),
            {"s": started, "f": finished, "t": tid},
        )


def _task_dir(tid: str) -> Path:
    from app.config import settings

    return Path(settings.storage_dir) / "tasks" / tid


def _touch_task_dir(tid: str) -> Path:
    d = _task_dir(tid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "page1.png").write_bytes(b"fake")
    return d


# ---------------------------------------------------------------- cleanup_expired
def test_cleanup_skips_running_task(env):
    """到期但还在跑的任务不能删目录，也不能改状态。

    删了目录 → worker 读页图全失败；直接改 expired → 绕过状态机。
    """
    uid = make_user("u_run")
    tid = make_task(uid, status="processing")
    d = _touch_task_dir(tid)
    _set_task(tid, expire_at=hours_later(-1))

    res = cleanup_expired()

    assert res["cleaned"] == 0, res
    assert d.exists(), "运行中的任务目录被删了"
    assert q1("SELECT status FROM tasks WHERE id=:i", i=tid)["status"] == "processing"


def test_cleanup_removes_expired_settled_task(env):
    """已结算且到期的任务：删目录 + 标 expired + files.cleaned=1。"""
    uid = make_user("u_old")
    tid = make_task(uid, status="settled", chars=100)
    d = _touch_task_dir(tid)
    _set_task(tid, expire_at=hours_later(-1))

    res = cleanup_expired()

    assert res["cleaned"] == 1, res
    assert not d.exists()
    row = q1("SELECT status, source_path FROM tasks WHERE id=:i", i=tid)
    assert row["status"] == "expired"
    assert row["source_path"] is None


def test_cleanup_is_idempotent(env):
    """重复清理不该重复计数（第二次已经没有可清的了）。"""
    uid = make_user("u_idem")
    tid = make_task(uid, status="settled")
    _touch_task_dir(tid)
    _set_task(tid, expire_at=hours_later(-1))

    assert cleanup_expired()["cleaned"] == 1
    assert cleanup_expired()["cleaned"] == 0


def test_cleanup_ignores_not_yet_expired(env):
    """没到期的任务不动。"""
    uid = make_user("u_fresh")
    tid = make_task(uid, status="settled")
    d = _touch_task_dir(tid)

    assert cleanup_expired()["cleaned"] == 0
    assert d.exists()
    assert q1("SELECT status FROM tasks WHERE id=:i", i=tid)["status"] == "settled"


# ---------------------------------------------------------------- expire_stale_running
def test_expire_stale_kills_truly_stuck_task(env):
    """创建很久、页也早就没动静 → 判定卡死。"""
    uid = make_user("u_stuck")
    tid = make_task(uid, status="processing")
    _set_task(tid, created_at=hours_later(-30))
    _set_page_activity(tid, started=hours_later(-30), finished=hours_later(-29))

    assert expire_stale_running() == 1
    row = q1("SELECT status, error_code FROM tasks WHERE id=:i", i=tid)
    assert row["status"] == "failed"
    assert row["error_code"] == "E_STALE"


def test_expire_stale_spares_task_with_recent_page_activity(env):
    """创建 10 小时前，但最后一页 2 分钟前才识别完 → 它在推进，不能杀。

    旧逻辑只看 created_at + 固定 6 小时，这条必挂。
    """
    uid = make_user("u_slow")
    tid = make_task(uid, status="processing")
    _set_task(tid, created_at=hours_later(-10))
    _set_page_activity(tid, started=hours_later(-0.1), finished=hours_later(-0.03))

    assert expire_stale_running() == 0
    assert q1("SELECT status FROM tasks WHERE id=:i", i=tid)["status"] == "processing"


def test_expire_stale_budget_scales_with_pages(env):
    """200 页任务给 13 小时预算：创建 7 小时前、页刚动过 → 不杀。"""
    uid = make_user("u_big")
    tid = make_task(uid, status="processing")
    _set_task(tid, pages_total=200, created_at=hours_later(-7))
    _set_page_activity(tid, started=hours_later(-0.05), finished=hours_later(-0.01))

    assert expire_stale_running() == 0
    assert q1("SELECT status FROM tasks WHERE id=:i", i=tid)["status"] == "processing"


def test_expire_stale_uses_created_at_when_no_page_activity(env):
    """排队中的任务没有任何页活动 → 退回用 created_at 判，长时间没人领也收掉。"""
    uid = make_user("u_queued")
    tid = make_task(uid, status="queued")
    _set_task(tid, created_at=hours_later(-40))

    assert expire_stale_running() == 1
    assert q1("SELECT status FROM tasks WHERE id=:i", i=tid)["status"] == "failed"


def test_expire_stale_ignores_finished_tasks(env):
    """终态任务不参与收尸。"""
    uid = make_user("u_done")
    tid = make_task(uid, status="settled")
    _set_task(tid, created_at=hours_later(-100))

    assert expire_stale_running() == 0
    assert q1("SELECT status FROM tasks WHERE id=:i", i=tid)["status"] == "settled"


def test_worker_main_loop_calls_expire_stale():
    """兜底必须真的被接上：worker 入口里要出现 expire_stale_running 的调用。

    这个函数此前写完就没人调 —— 光有实现不算兜底。
    """
    src = (Path(__file__).resolve().parent.parent / "app" / "worker.py").read_text()
    assert "expire_stale_running" in src
    # 必须是在循环里被调用，而不是只 import
    assert "expire_stale_running()" in src


# ---------------------------------------------------------------- 上传 / 充值边界
def test_upload_rejects_overlong_filename(user):
    """255 字符以上的文件名直接拒，别让它进库撑坏后台表格。"""
    client, _ = user
    blob = b"x" * 16
    r = client.post("/api/upload/init", json={"filename": "a" * 300 + ".png", "size": len(blob)})
    assert r.status_code == 400, r.text
    assert r.json()["code"] == "E_BAD_NAME"


def test_recharge_rejects_overlong_paid_at(user):
    """转账时间超长（误贴长文本）应被拒，而不是原样入库。"""
    client, _ = user
    r = client.post(
        "/api/recharge/orders",
        json={"amount_mill": 10000, "payer_note": "张三", "paid_at": "y" * 500},
    )
    assert r.status_code == 400, r.text
    assert r.json()["code"] == "E_BAD_PAID_AT"


def test_recharge_rejects_overlong_proof_url(user):
    client, _ = user
    r = client.post(
        "/api/recharge/orders",
        json={"amount_mill": 10000, "payer_note": "张三", "proof_url": "http://x/" + "z" * 600},
    )
    assert r.status_code == 400, r.text
    assert r.json()["code"] == "E_BAD_PROOF"


def test_recharge_info_qr_points_to_real_route(client):
    """默认收款码地址必须是真实存在的路由。

    settings 被重置（新库/误清配置）时会回落到默认值；
    写成 /static/pay/*.png 的话充值页会静默裂图，用户付不了钱还看不出原因。
    """
    r = client.get("/api/recharge/info")
    assert r.status_code == 200
    for m in r.json()["data"]["pay_methods"]:
        assert m["qr"].startswith("/pay/qr/"), m
        assert "/static/" not in m["qr"], m


def test_pay_qr_missing_returns_404_not_broken_image(client):
    """没上传收款码时给 404 + 明确错误码，而不是一张假图。"""
    r = client.get("/pay/qr/wechat")
    assert r.status_code == 404, r.text
    assert r.json()["code"] == "E_NOT_FOUND"
    assert "尚未上传" in r.json()["message"]


def test_pay_qr_rejects_unknown_method(client):
    r = client.get("/pay/qr/bitcoin")
    assert r.status_code == 404


def test_iso_formats_are_comparable():
    """收尸逻辑靠字符串比较时间，格式必须同源（都带 +00:00）。"""
    a = now_iso()
    b = hours_later(-1)
    assert a > b
    assert a.endswith("+00:00") and b.endswith("+00:00")
