"""计费与账本（开发规格 §7 / §13 P0 用例）。

对齐 FR-BIL 全组：金额只用整数厘、字数只来自 task_pages.chars、结算幂等、余额不足锁下载。
"""
from __future__ import annotations

import pytest

from app.db import q, q1
from app.services import billing
from tests.conftest import add_page, make_task, make_user, user_balance


# ---------------------------------------------------------------- 纯函数
@pytest.mark.parametrize(
    "chars,expected",
    [
        (0, 0),          # 0 字不收费
        (1, 100),        # 不足 1000 字按 1000 字
        (999, 100),
        (1000, 100),     # 下限正好
        (1001, 101),     # 向上取整：1001*1000/10000 = 100.1 → 101
        (12340, 1234),   # ¥1/万字 → 1234 厘 = ¥1.23
        (10000, 1000),   # 正好 ¥1
        (50000, 5000),
    ],
)
def test_bill_mill_rounding(env, chars, expected):
    assert billing.bill_mill(chars) == expected


def test_bill_mill_zero_is_free(env):
    """全在免费额度内 → 0 厘，绝不能出现最低 100 厘的误伤。"""
    assert billing.bill_mill(0) == 0


def test_bill_mill_never_float(env):
    """任何输入都必须是 int —— float 参与金额计算是本项目的红线。"""
    for c in (0, 1, 999, 1000, 12345, 987654):
        assert isinstance(billing.bill_mill(c), int)


# ---------------------------------------------------------------- 预估
def test_estimate_all_free(env):
    user = {"free_pages_total": 5, "free_pages_used": 0, "balance_mill": 0}
    e = billing.estimate(user, 3)
    assert e["free_pages_applied"] == 3
    assert e["paid_pages"] == 0
    assert e["estimated_amount_mill"] == 0
    assert e["free_only"] is True
    assert e["enough"] is True


def test_estimate_mixed_pages(env):
    user = {"free_pages_total": 5, "free_pages_used": 4, "balance_mill": 0}
    e = billing.estimate(user, 3)  # 1 页免费 + 2 页付费 = 800 字 → 最低 100 厘
    assert e["free_pages_applied"] == 1
    assert e["paid_pages"] == 2
    assert e["estimated_chars"] == 800
    assert e["estimated_amount_mill"] == 100
    assert e["enough"] is False


# ---------------------------------------------------------------- 结算
def test_settle_free_page_no_charge(env):
    uid = make_user("freeuser")
    tid = make_task(uid, chars=52, status="processing")
    r = billing.settle(tid)
    assert r["status"] == "settled"
    assert r["amount_mill"] == 0
    assert r["free_pages_applied"] == 1
    # 额度被消耗 1 页
    u = q1("SELECT free_pages_used FROM users WHERE id=:u", u=uid)
    assert int(u["free_pages_used"]) == 1
    # 没扣钱 → 不该有 consume 台账
    assert q("SELECT * FROM ledger WHERE user_id=:u AND type='consume'", u=uid) == []


def test_settle_paid_pages_charges(env):
    """5 页免费额度用完 → 第 6 页起按字计费。"""
    uid = make_user("paiduser", balance_mill=10_000)  # ¥10
    tid = make_task(uid, chars=2000, status="processing")
    # 把免费额度用满，逼出付费
    from app.db import tx
    from sqlalchemy import text

    with tx() as conn:
        conn.execute(text("UPDATE users SET free_pages_used=5 WHERE id=:u"), {"u": uid})
    r = billing.settle(tid)
    assert r["status"] == "settled"
    assert r["amount_mill"] == 200  # 2000 字 × 1000/10000 = 200 厘
    assert user_balance(uid) == 10_000 - 200
    rows = q("SELECT * FROM ledger WHERE user_id=:u AND type='consume'", u=uid)
    assert len(rows) == 1
    assert int(rows[0]["amount_mill"]) == -200
    assert int(rows[0]["balance_after"]) == 9_800


def test_settle_idempotent(env):
    """重复结算只扣一次 —— 上线后最容易出事的地方。"""
    uid = make_user("dupuser", balance_mill=10_000)
    tid = make_task(uid, chars=2000, status="processing")
    from app.db import tx
    from sqlalchemy import text

    with tx() as conn:
        conn.execute(text("UPDATE users SET free_pages_used=5 WHERE id=:u"), {"u": uid})

    r1 = billing.settle(tid)
    r2 = billing.settle(tid)
    r3 = billing.settle(tid)

    assert r1["already"] is False
    assert r2["already"] is True and r3["already"] is True
    assert user_balance(uid) == 9_800
    assert len(q("SELECT * FROM ledger WHERE user_id=:u AND type='consume'", u=uid)) == 1


def test_settle_low_balance_locks(env):
    """余额不足：扣到 0，记待补，状态 locked（可看不可下）。"""
    uid = make_user("pooruser", balance_mill=50)  # 只有 50 厘
    tid = make_task(uid, chars=5000, status="processing")  # 应扣 500 厘
    from app.db import tx
    from sqlalchemy import text

    with tx() as conn:
        conn.execute(text("UPDATE users SET free_pages_used=5 WHERE id=:u"), {"u": uid})

    r = billing.settle(tid)
    assert r["status"] == "locked"
    assert r["amount_mill"] == 50
    assert r["pending_mill"] == 450
    assert user_balance(uid) == 0
    t = q1("SELECT status, pending_mill FROM tasks WHERE id=:i", i=tid)
    assert t["status"] == "locked"
    assert int(t["pending_mill"]) == 450


def test_unlock_after_recharge(env):
    """充值后自动补足并转 settled。"""
    uid = make_user("unlockuser", balance_mill=50)
    tid = make_task(uid, chars=5000, status="processing")
    from app.db import tx
    from sqlalchemy import text

    with tx() as conn:
        conn.execute(text("UPDATE users SET free_pages_used=5 WHERE id=:u"), {"u": uid})
    billing.settle(tid)

    # 充 ¥1（1000 厘）
    with tx() as conn:
        billing.credit(conn, uid, 1000, ref_type="recharge", ref_id="RC-TEST", ltype="recharge")

    n = billing.unlock_after_recharge(uid)
    assert n == 1
    t = q1("SELECT status, pending_mill, amount_mill FROM tasks WHERE id=:i", i=tid)
    assert t["status"] == "settled"
    assert int(t["pending_mill"]) == 0
    assert int(t["amount_mill"]) == 500
    # 50 已扣 + 450 补扣 = 500，余额 1000-450=550
    assert user_balance(uid) == 550


def test_settle_all_pages_failed_refunds(env):
    """全部页失败 → failed + 全额退款。"""
    uid = make_user("failuser", balance_mill=10_000)
    tid = make_task(uid, chars=0, status="processing")
    from app.db import tx
    from sqlalchemy import text

    # 先制造一次已扣费（模拟中途失败后回滚）
    with tx() as conn:
        conn.execute(text("UPDATE users SET free_pages_used=5 WHERE id=:u"), {"u": uid})
        billing._change_balance(conn, uid, -300)
        billing._insert_ledger(conn, uid, "consume", -300, 10_000, 9_700, "task", tid, "已扣")
        conn.execute(text("UPDATE task_pages SET status='failed' WHERE task_id=:t"), {"t": tid})

    r = billing.settle(tid)
    assert r["status"] == "failed"
    assert user_balance(uid) == 10_000
    refunds = q("SELECT * FROM ledger WHERE user_id=:u AND type='refund'", u=uid)
    assert len(refunds) == 1
    assert int(refunds[0]["amount_mill"]) == 300


def test_ledger_integrity_ok(env):
    uid = make_user("integrityuser", balance_mill=10_000)
    tid = make_task(uid, chars=2000, status="processing")
    from app.db import tx
    from sqlalchemy import text

    with tx() as conn:
        conn.execute(text("UPDATE users SET free_pages_used=5 WHERE id=:u"), {"u": uid})
    billing.settle(tid)

    r = billing.check_ledger_integrity()
    assert r["ok"] is True, r["mismatches"]


def test_ledger_integrity_catches_drift(env):
    """直接把余额改掉（不写台账）必须被抓到 —— 这是资损监控的根。"""
    uid = make_user("drifter", balance_mill=10_000)
    from app.db import tx
    from sqlalchemy import text

    with tx() as conn:
        conn.execute(text("UPDATE users SET balance_mill=50000 WHERE id=:u"), {"u": uid})
    r = billing.check_ledger_integrity()
    assert r["ok"] is False
    assert any(m["user_id"] == uid for m in r["mismatches"])


def test_free_pages_not_consumed_by_failed_page(env):
    """失败页不消耗免费额度（FR-BIL-05）。"""
    uid = make_user("partialuser")
    tid = make_task(uid, chars=100, status="processing")
    add_page(tid, 2, chars=0, status="failed")
    r = billing.settle(tid)
    assert r["status"] == "settled"
    u = q1("SELECT free_pages_used FROM users WHERE id=:u", u=uid)
    assert int(u["free_pages_used"]) == 1  # 只有成功的那 1 页


def test_rerun_free_charges_nothing(env):
    """过期重跑的首次免费：不扣钱、也不消耗免费额度（FR-BIL-10 ③）。"""
    uid = make_user("rerunuser")  # 余额 0、免费额度 5
    tid = make_task(uid, chars=5000, status="processing")
    from sqlalchemy import text

    from app.db import tx

    with tx() as conn:
        conn.execute(text("UPDATE tasks SET rerun_free=1 WHERE id=:i"), {"i": tid})

    r = billing.settle(tid)
    assert r["status"] == "settled"
    assert r["amount_mill"] == 0
    assert user_balance(uid) == 0
    u = q1("SELECT free_pages_used FROM users WHERE id=:u", u=uid)
    assert int(u["free_pages_used"]) == 0  # 免费重跑不吃额度
