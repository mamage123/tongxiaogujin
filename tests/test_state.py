"""任务状态机与状态相关权限（开发规格 §4.1 / PRD §9.1）。

状态机会直接决定钱：允许回跳就可能重复扣费，所以这里既测「允许的迁移」，
也测「非法迁移必须抛错」。
"""
from __future__ import annotations

import pytest

from app.db import q1, tx
from app.enums import (
    ALLOWED,
    TASK_STATUS,
    IllegalTransition,
    assert_transition,
    can_cancel,
    can_download,
    can_retry_page,
    can_view_text,
)
from app.services.worker import claim_next, transition


# ---------------------------------------------------------------- 迁移表
def test_all_states_have_rule():
    """每个状态都要在 ALLOWED 里有条目，否则漏写会变成「永远不合法」。"""
    for s in TASK_STATUS:
        assert s in ALLOWED, f"{s} 缺少迁移规则"


def test_no_transition_out_of_expired():
    """expired 是终态，不许复活（复活等于绕过 72 小时保留期）。"""
    assert ALLOWED["expired"] == set()


def test_settled_cannot_go_back_to_processing():
    with pytest.raises(IllegalTransition):
        assert_transition("settled", "processing")


def test_locked_can_go_to_settled_and_expired():
    """PRD 明确：充值补足 → settled；满 72h → expired。开发规格漏了后者。"""
    assert_transition("locked", "settled")
    assert_transition("locked", "expired")


@pytest.mark.parametrize("src", ["settled", "partial", "failed", "cancelled", "locked"])
def test_all_terminal_states_can_expire(src):
    assert_transition(src, "expired")


def test_legal_happy_path():
    for src, dst in [
        ("created", "preprocessing"),
        ("preprocessing", "queued"),
        ("queued", "processing"),
        ("processing", "settling"),
        ("settling", "settled"),
    ]:
        assert_transition(src, dst)


@pytest.mark.parametrize(
    "src,dst",
    [
        ("created", "settled"),      # 跳过识别与结算
        ("queued", "settling"),      # 跳过处理
        ("processing", "settled"),   # 跳过 settling（结算必须显式一步）
        ("settling", "processing"),  # 结算中回跳
        ("cancelled", "processing"), # 已取消复活
    ],
)
def test_illegal_transitions_rejected(src, dst):
    with pytest.raises(IllegalTransition):
        assert_transition(src, dst)


# ---------------------------------------------------------------- 落库迁移
def test_transition_updates_db(env):
    from tests.conftest import make_task, make_user

    tid = make_task(make_user("t1"), status="queued")
    transition(tid, "processing")
    row = q1("SELECT status FROM tasks WHERE id=:i", i=tid)
    assert row["status"] == "processing"


def test_transition_illegal_does_not_write(env):
    from tests.conftest import make_task, make_user

    tid = make_task(make_user("t2"), status="settled")
    with pytest.raises(IllegalTransition):
        transition(tid, "processing")
    row = q1("SELECT status FROM tasks WHERE id=:i", i=tid)
    assert row["status"] == "settled"


# ---------------------------------------------------------------- 抢占
def test_claim_next_is_exclusive(env):
    """两个 worker 同时抢，只有一个能拿到（条件 UPDATE 保证）。"""
    from tests.conftest import make_task, make_user
    from app.db import q

    uid = make_user("claimer")
    make_task(uid, status="queued")
    make_task(uid, status="queued")

    first = claim_next()
    second = claim_next()
    assert first and second
    assert first["id"] != second["id"]
    assert claim_next() is None  # 队列空了
    assert len(q("SELECT id FROM tasks WHERE status='processing'")) == 2


def test_claim_next_ignores_non_queued(env):
    from tests.conftest import make_task, make_user

    uid = make_user("claimer2")
    make_task(uid, status="settled")
    make_task(uid, status="failed")
    assert claim_next() is None


# ---------------------------------------------------------------- 权限判定
@pytest.mark.parametrize(
    "status,view,download",
    [
        ("processing", True, False),   # 能边看边等
        ("settling", True, False),
        ("settled", True, True),
        ("partial", True, True),       # 部分完成也能下成功的页
        ("locked", True, False),       # 余额不足：可看不可下
        ("failed", False, False),
        ("cancelled", False, False),
        ("expired", False, False),
    ],
)
def test_view_and_download_matrix(status, view, download):
    assert can_view_text(status) is view
    assert can_download(status) is download


@pytest.mark.parametrize(
    "status,expected",
    [("created", True), ("preprocessing", True), ("queued", True), ("processing", True),
     ("settling", False), ("settled", False), ("failed", False)],
)
def test_cancel_matrix(status, expected):
    assert can_cancel(status) is expected


@pytest.mark.parametrize(
    "status,expected",
    [("processing", True), ("settled", True), ("partial", True),
     ("failed", False), ("expired", False), ("locked", False)],
)
def test_retry_page_matrix(status, expected):
    assert can_retry_page(status) is expected
