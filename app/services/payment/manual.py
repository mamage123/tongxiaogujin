"""人工充值通道（M1a L-min）。

流程：用户提交申请单 → 管理员转账核对 → 确认到账（幂等）→ 余额 + 台账 + 站内通知。

幂等三道：
1. `recharge_orders.status` 必须是 submitted；
2. `idempotency_key` 唯一索引（管理员的重复点击撞索引）；
3. `ledger` 唯一索引兜底（同一订单不会入账两次）。
"""
from __future__ import annotations

from sqlalchemy import text

from ...db import q1, tx
from ...utils import new_id, now_iso
from ..billing import _insert_ledger, _change_balance, unlock_after_recharge


class ManualChannel:
    name = "manual"

    # ------------------------------------------------------------ 建单
    def create_order(self, user_id: str, amount_mill: int, **kwargs) -> dict:
        from ...db import q

        # EX-17：已有待确认单时不允许再提
        pending = q1(
            "SELECT id, order_no FROM recharge_orders WHERE user_id=:u AND status='submitted' LIMIT 1",
            u=user_id,
        )
        if pending:
            raise ValueError(f"你有 1 笔待确认的充值申请（{pending['order_no']}），请等待处理")

        order_id = new_id()
        order_no = self._next_order_no()
        idem = f"manual|{order_id}"
        with tx() as conn:
            conn.execute(
                text(
                    "INSERT INTO recharge_orders(id,order_no,user_id,amount_mill,source,pay_method,"
                    "payer_note,paid_at,proof_url,proof_hash,status,idempotency_key,created_at)"
                    " VALUES(:id,:no,:u,:a,'manual',:pm,:pn,:pa,:pu,:ph,'submitted',:ik,:c)"
                ),
                {
                    "id": order_id,
                    "no": order_no,
                    "u": user_id,
                    "a": amount_mill,
                    "pm": kwargs.get("pay_method", "wechat"),
                    "pn": kwargs.get("payer_note"),
                    "pa": kwargs.get("paid_at"),
                    "pu": kwargs.get("proof_url"),
                    "ph": kwargs.get("proof_hash"),
                    "ik": idem,
                    "c": now_iso(),
                },
            )
        return {"id": order_id, "order_no": order_no, "status": "submitted"}

    @staticmethod
    def _next_order_no() -> str:
        from datetime import datetime, timezone

        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        row = q1("SELECT COUNT(*) AS n FROM recharge_orders WHERE order_no LIKE :p", p=f"RC{day}%")
        seq = int(row["n"]) + 1 if row else 1
        # 加随机后缀避免并发撞号（唯一索引兜底）
        from ...utils import short_id

        return f"RC{day}{seq:04d}{short_id(2).upper()}"

    # ------------------------------------------------------------ 确认（管理员）
    def handle_callback(self, payload: dict) -> dict:
        """人工通道的「回调」= 管理员点确认。"""
        order_id = payload["order_id"]
        admin_id = payload["admin_id"]
        idem = payload.get("idempotency_key") or f"manual|{admin_id}|{order_id}"

        with tx() as conn:
            row = conn.execute(
                text("SELECT * FROM recharge_orders WHERE id=:id"), {"id": order_id}
            ).mappings().first()
            if not row:
                raise ValueError("充值单不存在")
            row = dict(row)

            if row["status"] == "confirmed":
                raise AlreadyDone("该单已处理（已到账），请勿重复确认")
            if row["status"] != "submitted":
                # ALLOWED_RCG["submitted"] 是「可迁入的目标」，不是「当前应处的状态」
                raise ValueError(f"该单状态为「{row['status']}」，不能确认")

            # 幂等键撞唯一索引 → 说明同一管理员已确认过
            try:
                conn.execute(
                    text(
                        "UPDATE recharge_orders SET status='confirmed', admin_id=:a, "
                        "idempotency_key=:ik, confirmed_at=:c WHERE id=:id AND status='submitted'"
                    ),
                    {"a": admin_id, "ik": idem, "c": now_iso(), "id": order_id},
                )
            except Exception as e:  # 唯一索引冲突
                if "UNIQUE" in str(e).upper():
                    raise AlreadyDone("该单已处理，请勿重复确认") from e
                raise

            # 入账
            from ..billing import credit

            res = credit(
                conn,
                row["user_id"],
                int(row["amount_mill"]),
                "recharge_order",
                order_id,
                f"充值到账 {row['order_no']}",
                ltype="recharge",
            )

            conn.execute(
                text(
                    "INSERT INTO notifications(user_id,kind,title,body,link,created_at)"
                    " VALUES(:u,'recharge_confirmed',:t,:b,:l,:c)"
                ),
                {
                    "u": row["user_id"],
                    "t": f"充值 ¥{int(row['amount_mill']) // 1000} 已到账",
                    "b": f"单号 {row['order_no']}，余额已更新，可继续识别与下载。",
                    "l": "/account",
                    "c": now_iso(),
                },
            )

        unlocked = unlock_after_recharge(row["user_id"])
        return {
            "order_no": row["order_no"],
            "amount_mill": int(row["amount_mill"]),
            "balance_after": res["balance_after"],
            "unlocked_tasks": unlocked,
        }

    # ------------------------------------------------------------ 驳回
    def reject(self, order_id: str, admin_id: str, reason: str) -> dict:
        if not (reason or "").strip():
            raise MissingReason("驳回必须填写原因")
        with tx() as conn:
            row = conn.execute(
                text("SELECT * FROM recharge_orders WHERE id=:id"), {"id": order_id}
            ).mappings().first()
            if not row:
                raise ValueError("充值单不存在")
            row = dict(row)
            if row["status"] != "submitted":
                raise AlreadyDone("该单已处理，请勿重复操作")
            conn.execute(
                text(
                    "UPDATE recharge_orders SET status='rejected', admin_id=:a, reject_reason=:r,"
                    " confirmed_at=:c WHERE id=:id AND status='submitted'"
                ),
                {"a": admin_id, "r": reason.strip(), "c": now_iso(), "id": order_id},
            )
            conn.execute(
                text(
                    "INSERT INTO notifications(user_id,kind,title,body,link,created_at)"
                    " VALUES(:u,'recharge_rejected',:t,:b,:l,:c)"
                ),
                {
                    "u": row["user_id"],
                    "t": f"充值申请 {row['order_no']} 未通过",
                    "b": f"原因：{reason.strip()}。如有疑问请联系管理员。",
                    "l": "/recharge",
                    "c": now_iso(),
                },
            )
        return {"order_no": row["order_no"], "status": "rejected"}

    # ------------------------------------------------------------ 查询
    def query_order(self, order_no: str) -> dict:
        return q1("SELECT * FROM recharge_orders WHERE order_no=:n", n=order_no) or {}


class AlreadyDone(Exception):
    """幂等命中：该单已处理。"""


class MissingReason(Exception):
    """必填原因缺失。"""
