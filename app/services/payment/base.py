"""支付渠道抽象（D30，为迁移预留）。

「账本不认渠道，订单才认渠道」：
- ledger 只记事件（充值/扣费/退款/调整/过期），不含 channel 字段；
- 渠道信息只在 recharge_orders.source 与本文件的实现类里。
将来接微信支付 = 新增一个实现类，ledger 零变更。
"""
from __future__ import annotations

import hashlib
from typing import Protocol, runtime_checkable


@runtime_checkable
class PaymentChannel(Protocol):
    name: str  # manual / wechat / alipay

    def create_order(self, user_id: str, amount_mill: int) -> dict: ...
    def handle_callback(self, payload: dict) -> dict: ...
    def query_order(self, order_no: str) -> dict: ...


def make_idempotency_key(admin_id: str, proof_bytes: bytes | None, amount_mill: int, order_id: str) -> str:
    """人工通道：管理员 + 凭证哈希 + 金额（+ 订单号防两笔同额同凭证误判）。

    为什么带 order_id：两笔 ¥30 用同一张截图提交时，凭证哈希与金额都相同，
    如果不带订单号，第二笔会被唯一索引挡掉——那不是幂等，是漏单。
    """
    proof_hash = hashlib.sha256(proof_bytes or b"").hexdigest()[:16]
    return f"{admin_id}|{proof_hash}|{amount_mill}|{order_id}"
