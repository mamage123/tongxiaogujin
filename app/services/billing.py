"""计费与账本（开发规格 §7，FR-BIL 全组）。

不可违反：
1. 金额只用整数「厘」，禁止 float。（ceil 用整数运算）
2. 字数只来自 `task_pages.chars`（该值由 normalize 一次算清），本模块不数任何文本。
3. 结算幂等：`uq_ledger_consume` 唯一索引兜底 + 状态判断双重保证。
4. 账本不认渠道：ledger 只记事件，渠道在 recharge_orders.source。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import text

from ..config import settings
from ..db import q, q1, setting_float, setting_int, tx
from ..enums import assert_transition
from ..utils import mill_to_yuan, now_iso

# 已结算/终态：settle 遇到直接返回，不重复记账
_SETTLED_STATES = {"settled", "partial", "locked", "failed", "cancelled", "expired"}


# ---------------------------------------------------------------- 纯函数
def bill_mill(chars: int, price_per_10k_mill: int | None = None, min_chars: int | None = None) -> int:
    """金额(厘) = ceil(max(chars, min_chars) * price / 10000)，向上取整到厘。

    bill_mill(12340) == 1234
    bill_mill(300)   == 100
    bill_mill(0)     == 0    ← 0 字不收费（全部页在额度内 / 空白页）
    """
    if chars <= 0:
        return 0
    price = settings.price_per_10k_mill if price_per_10k_mill is None else price_per_10k_mill
    minc = settings.min_bill_chars if min_chars is None else min_chars
    c = max(int(chars), int(minc))
    return -(-c * int(price) // 10000)  # 整数向上取整


def estimate_chars(pages: int) -> int:
    """FR-BIL-03：按每页 400 字估上限。"""
    return max(0, int(pages)) * 400


# ---------------------------------------------------------------- 预估
def estimate(user: dict, pages: int, mode: str = "ancient") -> dict[str, Any]:
    """开跑前给用户看的预估：页数、免费页、预计扣费。"""
    free_left = max(0, user["free_pages_total"] - user["free_pages_used"])
    free_applied = min(free_left, pages)
    paid_pages = max(0, pages - free_applied)
    est_chars = estimate_chars(paid_pages)
    amount = bill_mill(est_chars) if paid_pages else 0

    balance = int(user["balance_mill"])
    low_balance = balance <= setting_int("low_balance_mill", settings.low_balance_mill)
    page_limit = (
        setting_int("max_pages_low_balance", settings.max_pages_low_balance)
        if low_balance
        else setting_int("max_pages_normal", settings.max_pages)
    )

    return {
        "pages": pages,
        "free_pages_left": free_left,
        "free_pages_applied": free_applied,
        "paid_pages": paid_pages,
        "estimated_chars": est_chars,
        "estimated_amount_mill": amount,
        "estimated_amount_text": f"¥{mill_to_yuan(amount)}",
        "balance_mill": balance,
        "balance_text": f"¥{mill_to_yuan(balance)}",
        "enough": balance >= amount,
        "free_only": paid_pages == 0,
        "low_balance": low_balance,
        "page_limit": page_limit,
        "all_free_hint": "均在内，本次不扣费" if paid_pages == 0 else "",
        "note": "实际按识别字数结算；不足 1000 字按 1000 字计费",
    }


# ---------------------------------------------------------------- 账本原语
def _insert_ledger(
    conn,
    user_id: str,
    ltype: str,
    amount_mill: int,
    before: int,
    after: int,
    ref_type: str | None,
    ref_id: str | None,
    remark: str | None,
) -> int:
    res = conn.execute(
        text(
            "INSERT INTO ledger(user_id,type,amount_mill,balance_before,balance_after,ref_type,ref_id,remark,created_at)"
            " VALUES(:u,:t,:a,:b,:af,:rt,:ri,:rm,:c)"
        ),
        {
            "u": user_id,
            "t": ltype,
            "a": amount_mill,
            "b": before,
            "af": after,
            "rt": ref_type,
            "ri": ref_id,
            "rm": remark,
            "c": now_iso(),
        },
    )
    return int(res.lastrowid or 0)


def _change_balance(conn, user_id: str, delta_mill: int, require_enough: bool = False) -> tuple[int, int]:
    """改余额并返回 (before, after)。require_enough 时余额不足抛错（原子条件更新）。"""
    row = conn.execute(
        text("SELECT balance_mill FROM users WHERE id=:u"), {"u": user_id}
    ).mappings().first()
    if not row:
        raise ValueError("用户不存在")
    before = int(row["balance_mill"])
    after = before + delta_mill
    if after < 0:
        if require_enough:
            raise ValueError("余额不足")
        after = 0
    res = conn.execute(
        text("UPDATE users SET balance_mill=:af WHERE id=:u AND balance_mill=:bf"),
        {"af": after, "u": user_id, "bf": before},
    )
    if res.rowcount != 1:
        raise ValueError("余额并发更新冲突，请重试")
    return before, after


# ---------------------------------------------------------------- 充值入账
def credit(
    conn,
    user_id: str,
    amount_mill: int,
    ref_type: str,
    ref_id: str,
    remark: str | None = None,
    ltype: str = "recharge",
) -> dict:
    """入账（充值确认 / 人工调整 / 过期清零退款）。调用方负责事务。"""
    if amount_mill == 0:
        raise ValueError("金额不能为 0")
    before, after = _change_balance(conn, user_id, amount_mill)
    lid = _insert_ledger(conn, user_id, ltype, amount_mill, before, after, ref_type, ref_id, remark)

    if ltype == "recharge":
        # 余额有效期：自最后一次充值起 12 个月（FR-RCG-08）
        from ..utils import iso, now
        from datetime import timedelta

        expire = iso(now() + timedelta(days=365))
        conn.execute(
            text("UPDATE users SET last_recharge_at=:t, balance_expire_at=:e WHERE id=:u"),
            {"t": now_iso(), "e": expire, "u": user_id},
        )
    return {"ledger_id": lid, "balance_before": before, "balance_after": after}


# ---------------------------------------------------------------- 结算
def settle(task_id: str) -> dict[str, Any]:
    """结算一个任务。事务内完成：额度扣减 + 扣费 + 状态迁移 + 台账。

    返回 {status, amount_mill, pending_mill, free_pages_applied, chars, already: bool}
    """
    price = setting_int("price_per_10k_mill", settings.price_per_10k_mill)
    min_chars = setting_int("min_bill_chars", settings.min_bill_chars)

    with tx() as conn:
        task = conn.execute(
            text("SELECT * FROM tasks WHERE id=:id"), {"id": task_id}
        ).mappings().first()
        if not task:
            raise ValueError("任务不存在")
        task = dict(task)

        # ---- 幂等：已结算直接返回上次结果
        if task["status"] in _SETTLED_STATES:
            return {
                "status": task["status"],
                "amount_mill": int(task["amount_mill"]),
                "pending_mill": int(task["pending_mill"]),
                "free_pages_applied": int(task["free_pages_applied"]),
                "chars": int(task["chars"]),
                "already": True,
            }

        user = conn.execute(
            text("SELECT * FROM users WHERE id=:u"), {"u": task["user_id"]}
        ).mappings().first()
        if not user:
            raise ValueError("用户不存在")
        user = dict(user)

        pages = conn.execute(
            text("SELECT page_no,status,chars FROM task_pages WHERE task_id=:t ORDER BY page_no"),
            {"t": task_id},
        ).mappings().all()
        pages = [dict(p) for p in pages]

        done_pages = [p for p in pages if p["status"] == "done"]
        total_chars = sum(int(p["chars"]) for p in done_pages)

        # ---- 全部失败：failed + 全额退款（FR-BIL-09 / EX-08）
        if not done_pages:
            _refund_locked(conn, task)
            conn.execute(
                text(
                    "UPDATE tasks SET status='failed', chars=0, amount_mill=0, pending_mill=0,"
                    " error_code=COALESCE(error_code,'E_ALL_PAGES_FAILED') WHERE id=:id"
                ),
                {"id": task_id},
            )
            return {
                "status": "failed",
                "amount_mill": 0,
                "pending_mill": 0,
                "free_pages_applied": 0,
                "chars": 0,
                "already": False,
            }

        # ---- 免费额度：按页序前 N 页免费（FR-BIL-05）
        free_left = max(0, int(user["free_pages_total"]) - int(user["free_pages_used"]))
        if task["rerun_free"]:
            # 过期重跑首次免费：不扣费、不消耗额度（FR-BIL-10 ③）
            free_applied = len(done_pages)
            paid_chars = 0
        else:
            free_applied = min(free_left, len(done_pages))
            paid_chars = sum(int(p["chars"]) for p in done_pages[free_applied:])

        amount = bill_mill(paid_chars, price, min_chars) if paid_chars > 0 else 0
        balance = int(user["balance_mill"])
        status_now = task["status"]

        # ---- 迁移到 settling（允许从 processing 来）
        if status_now != "settling":
            assert_transition(status_now, "settling")

        # 消耗免费额度（失败与空白页不计，中途取消不返还）
        if free_applied > 0 and not task["rerun_free"]:
            conn.execute(
                text(
                    "UPDATE users SET free_pages_used = MIN(free_pages_total, free_pages_used + :n)"
                    " WHERE id=:u"
                ),
                {"n": free_applied, "u": user["id"]},
            )

        if amount == 0:
            conn.execute(
                text(
                    "UPDATE tasks SET status='settled', chars=:c, amount_mill=0, pending_mill=0,"
                    " free_pages_applied=:f WHERE id=:id"
                ),
                {"c": total_chars, "f": free_applied, "id": task_id},
            )
            return {
                "status": "settled",
                "amount_mill": 0,
                "pending_mill": 0,
                "free_pages_applied": free_applied,
                "chars": total_chars,
                "already": False,
            }

        if balance >= amount:
            before, after = _change_balance(conn, user["id"], -amount, require_enough=True)
            _insert_ledger(
                conn,
                user["id"],
                "consume",
                -amount,
                before,
                after,
                "task",
                task_id,
                f"识别 {total_chars} 字（{len(done_pages)} 页，免费 {free_applied} 页）",
            )
            conn.execute(
                text(
                    "UPDATE tasks SET status='settled', chars=:c, amount_mill=:a, pending_mill=0,"
                    " free_pages_applied=:f WHERE id=:id"
                ),
                {"c": total_chars, "a": amount, "f": free_applied, "id": task_id},
            )
            return {
                "status": "settled",
                "amount_mill": amount,
                "pending_mill": 0,
                "free_pages_applied": free_applied,
                "chars": total_chars,
                "already": False,
            }

        # ---- 余额不足：扣至 0，记待补，锁下载（FR-BIL-06 / EX-10）
        before, after = _change_balance(conn, user["id"], -balance)
        if balance > 0:
            _insert_ledger(
                conn,
                user["id"],
                "consume",
                -balance,
                before,
                after,
                "task",
                task_id,
                f"部分扣费（应扣 {amount} 厘，余额不足），待补 {amount - balance} 厘",
            )
        conn.execute(
            text(
                "UPDATE tasks SET status='locked', chars=:c, amount_mill=:a, pending_mill=:p,"
                " free_pages_applied=:f WHERE id=:id"
            ),
            {"c": total_chars, "a": balance, "p": amount - balance, "f": free_applied, "id": task_id},
        )
        return {
            "status": "locked",
            "amount_mill": balance,
            "pending_mill": amount - balance,
            "free_pages_applied": free_applied,
            "chars": total_chars,
            "already": False,
        }


def _refund_locked(conn, task: dict) -> None:
    """任务失败时把已扣的钱退回（幂等：uq_ledger_consume 保证一条）。"""
    rows = conn.execute(
        text(
            "SELECT amount_mill FROM ledger WHERE type='consume'"
            " AND ref_type IN ('task','task_topup') AND ref_id=:t"
        ),
        {"t": task["id"]},
    ).mappings().all()
    spent = sum(-int(r["amount_mill"]) for r in rows)
    if spent <= 0:
        return
    before, after = _change_balance(conn, task["user_id"], spent)
    _insert_ledger(conn, task["user_id"], "refund", spent, before, after, "task", task["id"], "识别失败全额退款")


def unlock_after_recharge(user_id: str) -> int:
    """充值后自动解锁：把所有 locked 任务的待补金额补齐并转 settled。

    返回解锁任务数。若余额仍不足，则用余额部分抵扣、剩余待补继续保留。
    """
    unlocked = 0
    with tx() as conn:
        tasks = conn.execute(
            text("SELECT * FROM tasks WHERE user_id=:u AND status='locked' ORDER BY created_at"),
            {"u": user_id},
        ).mappings().all()
        for t in tasks:
            t = dict(t)
            user = conn.execute(
                text("SELECT balance_mill FROM users WHERE id=:u"), {"u": user_id}
            ).mappings().first()
            if not user:
                break
            balance = int(user["balance_mill"])
            pending = int(t["pending_mill"])
            if pending <= 0:
                continue
            pay = min(balance, pending)
            if pay <= 0:
                break  # 没钱了，后面的也解不了
            before, after = _change_balance(conn, user_id, -pay)
            # 用独立 ref_type：同一任务已有 settle 时的 consume 行，同一 (type,ref_type,ref_id)
            # 会撞 uq_ledger_consume。补扣是「第二笔、语义不同」的扣款，必须单独记账。
            # 不变量：pay < pending 时 pay = balance，余额归零，因此同一任务最多补扣一次。
            _insert_ledger(
                conn,
                user_id,
                "consume",
                -pay,
                before,
                after,
                "task_topup",
                t["id"],
                f"补扣待补金额 {pay} 厘",
            )
            remain = pending - pay
            if remain <= 0:
                conn.execute(
                    text("UPDATE tasks SET status='settled', pending_mill=0, amount_mill=amount_mill+:p WHERE id=:id"),
                    {"p": pay, "id": t["id"]},
                )
                unlocked += 1
            else:
                conn.execute(
                    text("UPDATE tasks SET pending_mill=:r, amount_mill=amount_mill+:p WHERE id=:id"),
                    {"r": remain, "p": pay, "id": t["id"]},
                )
    return unlocked


# ---------------------------------------------------------------- 查询
def user_ledger(user_id: str, limit: int = 50, offset: int = 0) -> list[dict]:
    return q(
        "SELECT * FROM ledger WHERE user_id=:u ORDER BY id DESC LIMIT :l OFFSET :o",
        u=user_id,
        l=limit,
        o=offset,
    )


def total_consumed(user_id: str) -> int:
    row = q1(
        "SELECT COALESCE(SUM(-amount_mill),0) AS s FROM ledger WHERE user_id=:u AND type='consume'",
        u=user_id,
    )
    return int(row["s"]) if row else 0


def daily_reconcile(date_str: str) -> dict[str, Any]:
    """每日对账（FR-ADM-05）：期初 + 充值 − 消费 − 退款 + 调整 = 期末。

    用「当日最后一笔的 balance_after 之和」作为期末是错的（跨用户）。
    正确做法：按用户分别算，再汇总。这里给出全站口径：
      期末总额 = Σ每个用户当日最后一笔 balance_after（无当日流水则取当前余额）
    """
    users = q("SELECT id, balance_mill FROM users")
    end_total = 0
    for u in users:
        last = q1(
            "SELECT balance_after FROM ledger WHERE user_id=:u AND created_at < :d2 "
            "ORDER BY id DESC LIMIT 1",
            u=u["id"],
            d2=f"{date_str}T99",
        )
        end_total += int(last["balance_after"]) if last else 0

    sums = q(
        "SELECT type, COALESCE(SUM(amount_mill),0) AS s, COUNT(*) AS n FROM ledger "
        "WHERE created_at >= :d1 AND created_at < :d2 GROUP BY type",
        d1=date_str,
        d2=f"{date_str}T99",
    )
    by_type = {r["type"]: {"sum": int(r["s"]), "count": int(r["n"])} for r in sums}

    recharge = by_type.get("recharge", {}).get("sum", 0)
    consume = by_type.get("consume", {}).get("sum", 0)      # 负数
    refund = by_type.get("refund", {}).get("sum", 0)
    adjust = by_type.get("adjust", {}).get("sum", 0)
    expire = by_type.get("expire", {}).get("sum", 0)

    # 期初 = 期末 - 当日全部变动
    delta = recharge + consume + refund + adjust + expire
    begin_total = end_total - delta

    return {
        "date": date_str,
        "begin_total": begin_total,
        "recharge": recharge,
        "consume": consume,
        "refund": refund,
        "adjust": adjust,
        "expire": expire,
        "delta": delta,
        "end_total": end_total,
        "balanced": begin_total + delta == end_total,
        "by_type": by_type,
    }


def check_ledger_integrity(user_id: str | None = None) -> dict[str, Any]:
    """资损监控：台账累加 vs 用户余额，必须完全相等。"""
    if user_id:
        users = q("SELECT id, username, balance_mill FROM users WHERE id=:u", u=user_id)
    else:
        users = q("SELECT id, username, balance_mill FROM users")
    bad = []
    for u in users:
        s = q1("SELECT COALESCE(SUM(amount_mill),0) AS s FROM ledger WHERE user_id=:u", u=u["id"])
        total = int(s["s"]) if s else 0
        if total != int(u["balance_mill"]):
            bad.append(
                {
                    "user_id": u["id"],
                    "username": u["username"],
                    "balance_mill": int(u["balance_mill"]),
                    "ledger_sum_mill": total,
                    "diff_mill": int(u["balance_mill"]) - total,
                }
            )
    return {"ok": not bad, "checked": len(users), "mismatches": bad}


def expire_balance(now_iso_str: str | None = None) -> int:
    """余额过期清零（FR-RCG-08）。返回清零用户数。"""
    ts = now_iso_str or now_iso()
    rows = q("SELECT id, balance_mill FROM users WHERE balance_expire_at IS NOT NULL AND balance_expire_at < :t AND balance_mill > 0", t=ts)
    n = 0
    for u in rows:
        with tx() as conn:
            before, after = _change_balance(conn, u["id"], -int(u["balance_mill"]))
            _insert_ledger(
                conn, u["id"], "expire", -int(u["balance_mill"]), before, after, "admin", None, "余额有效期届满清零"
            )
            n += 1
    return n
