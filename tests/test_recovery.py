"""worker 崩溃恢复与断点续跑。

背景（2026-10-28 实测）：`claim_next()` 只认 `queued`，所以 worker 一旦被杀
（重启、OOM、断电、部署），被抢占的任务就永远停在 `processing` —— 没有任何
代码会再碰它，用户看到「识别中」一路到 72 小时文件过期。115 页的书要跑
90 分钟，随便重启一次就中招。

两条一起才成立：
1. `recover_stale_tasks()`：启动时把僵尸 processing 放回队列；
2. `run_task()` 跳过已 done 的页，否则放回去等于从头重跑，重复花 OCR 的钱。
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from app.db import q1, q, tx
from app.services import worker as W
from tests.conftest import add_page, make_task, make_user


class _FakeRes:
    """recognize_page 的最小返回物。"""

    chars = 42
    plain_text = "測試文字內容"
    blocks: list = []
    order_ok = True
    _order_score = 1.0


def _fake_recognize(calls: list):
    def _fn(img, **kw):
        calls.append(img)
        return _FakeRes(), "mock", ["mock#1:ok"]

    return _fn


# ---------------------------------------------------------------- 崩溃恢复
def test_recover_requeues_zombie_task(env):
    uid = make_user("zombie", balance_mill=10000)
    tid = make_task(uid, status="processing")
    with tx() as conn:
        conn.execute(
            text("UPDATE task_pages SET status='processing' WHERE task_id=:t"), {"t": tid}
        )

    assert W.recover_stale_tasks() == 1

    assert q1("SELECT status FROM tasks WHERE id=:i", i=tid)["status"] == "queued"
    page = q1("SELECT status FROM task_pages WHERE task_id=:t", t=tid)
    # 半截的页必须回到 pending，否则续跑会把它当「正在处理」跳过
    assert page["status"] == "pending"


def test_recover_ignores_other_states(env):
    """只动 processing，别把已结算/已取消的任务翻出来。"""
    uid = make_user("quiet", balance_mill=0)
    done = make_task(uid, status="settled")
    cancelled = make_task(uid, status="cancelled")
    assert W.recover_stale_tasks() == 0
    assert q1("SELECT status FROM tasks WHERE id=:i", i=done)["status"] == "settled"
    assert q1("SELECT status FROM tasks WHERE id=:i", i=cancelled)["status"] == "cancelled"


def test_claim_next_picks_recovered_task(env):
    """恢复之后必须真的能被领走——否则「放回队列」只是一句空话。"""
    uid = make_user("claimable", balance_mill=10000)
    tid = make_task(uid, status="processing")
    W.recover_stale_tasks()
    task = W.claim_next()
    assert task is not None
    assert task["id"] == tid
    assert task["status"] == "processing"


# ---------------------------------------------------------------- 断点续跑
def test_run_task_skips_already_done_pages(env, monkeypatch):
    """3 页里 2 页已完成，续跑只能调 1 次 OCR，且结算字数要把前两页算进去。"""
    uid = make_user("resumer", balance_mill=10000)
    tid = make_task(uid, status="queued", chars=120, estimated_chars=1200)
    with tx() as conn:
        conn.execute(
            text("UPDATE task_pages SET status='done', chars=120 WHERE task_id=:t"), {"t": tid}
        )
    add_page(tid, 2, chars=0, status="done")
    with tx() as conn:
        conn.execute(
            text("UPDATE task_pages SET chars=80 WHERE task_id=:t AND page_no=2"), {"t": tid}
        )
    add_page(tid, 3, chars=0, status="pending")

    calls: list = []
    monkeypatch.setattr(W, "recognize_page", _fake_recognize(calls))

    res = W.run_task(q1("SELECT * FROM tasks WHERE id=:i", i=tid))
    assert res["status"] == "settled"
    assert len(calls) == 1, f"已完成的页被重跑了：调用了 {len(calls)} 次"

    task = q1("SELECT status, chars, pages_total FROM tasks WHERE id=:i", i=tid)
    assert task["status"] == "settled"
    # 120 + 80 + 42：断点续跑漏算已完成的页，用户会少付钱、台账还会不平衡
    assert int(task["chars"]) == 242


def test_stop_signal_requeues_instead_of_settling(env, monkeypatch):
    """收到停机信号时任务放回队列、不结算 —— 用户拿到的不能是半本书。"""
    uid = make_user("stopper", balance_mill=10000)
    tid = make_task(uid, status="queued", chars=0, estimated_chars=1200)
    add_page(tid, 2, chars=0, status="pending")

    calls: list = []
    monkeypatch.setattr(W, "recognize_page", _fake_recognize(calls))
    monkeypatch.setattr(W, "STOP_REQUESTED", True, raising=False)

    res = W.run_task(q1("SELECT * FROM tasks WHERE id=:i", i=tid))
    assert res["status"] == "requeued"
    assert calls == [], "收到停机信号后不该再开新页"

    task = q1("SELECT status, chars FROM tasks WHERE id=:i", i=tid)
    assert task["status"] == "queued"
    pages = q("SELECT status FROM task_pages WHERE task_id=:t ORDER BY page_no", t=tid)
    # 剩余页不能被标成 skipped：标了就再也补不回来了
    assert "skipped" not in {p["status"] for p in pages}
    # 没结算，余额一分不能动
    assert int(q1("SELECT balance_mill FROM users WHERE id=:i", i=uid)["balance_mill"]) == 10000
