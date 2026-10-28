"""充值（人工通道，M1a L-min）。

流程与文案必须与 PRD §7.7 一致：
- 固定四档 ¥10/30/100/300，**禁止自定义金额**（避免对不上账）
- 提交申请单 → 管理员确认 → 余额 + 台账 + 站内通知
- 页面必须明示：不可提现、不可转让；平台停止服务时未使用余额全额退还
"""
from __future__ import annotations

from fastapi import APIRouter, Request

from ..config import settings
from ..db import get_setting, q, q1
from ..deps import ApiError, client_ip, get_current_user, ok
from ..enums import PAY_METHOD, RCG_STATUS_LABEL, RECHARGE_PRESETS_MILL
from ..services import audit
from ..services.payment.manual import ManualChannel
from ..utils import mill_to_yuan, now_iso

router = APIRouter(prefix="/api/recharge", tags=["recharge"])

TRUST_LINES = [
    "人工确认，工作日 9:00-23:00",
    "通常 2 小时内到账",
    "到账后可在「余额流水」随时核对",
]


@router.get("/info")
def info():
    """充值页所需的全部信息（档位、收款方式、到账说明、规则）。"""
    # 默认值必须是真实路由 /pay/qr/{method}。曾经写成 /static/pay/*.png，
    # 而 static/ 目录根本不存在 —— settings 一旦被重置（新库、误清配置），
    # 充值页的二维码就会 404 裂图，用户付不了钱却看不出原因。
    wechat = get_setting("pay_wechat_qr", "/pay/qr/wechat")
    alipay = get_setting("pay_alipay_qr", "/pay/qr/alipay")
    return ok(
        {
            "presets": [
                {"amount_mill": m, "amount_text": f"¥{mill_to_yuan(m)}"} for m in RECHARGE_PRESETS_MILL
            ],
            "pay_methods": [
                {"value": "wechat", "label": "微信", "qr": wechat},
                {"value": "alipay", "label": "支付宝", "qr": alipay},
            ],
            "admin_name": get_setting("admin_display_name", "马哥"),
            "work_hours": get_setting("admin_work_hours", "工作日 9:00-23:00"),
            "trust_lines": TRUST_LINES,
            "rules": [
                "余额仅用于本平台识别服务，不可提现、不可转让",
                "如本平台停止提供服务，未使用余额将全额退还",
                "识别失败会自动退回余额",
                "余额有效期：自最后一次充值起 12 个月",
            ],
            "note": "付款后请填写付款人昵称与转账时间并提交申请，管理员核对后手动到账。",
        }
    )


@router.post("/orders")
def create_order(request: Request, payload: dict):
    user = get_current_user(request)
    amount_mill = int(payload.get("amount_mill") or 0)
    pay_method = payload.get("pay_method") or "wechat"
    payer_note = (payload.get("payer_note") or "").strip()
    paid_at = (payload.get("paid_at") or "").strip() or None
    proof_url = (payload.get("proof_url") or "").strip() or None

    # 固定档位：禁止自定义金额（FR-RCG-01）
    if amount_mill not in RECHARGE_PRESETS_MILL:
        raise ApiError("E_BAD_AMOUNT", "请选择固定充值档位")
    if pay_method not in PAY_METHOD:
        raise ApiError("E_BAD_METHOD", "请选择付款方式")
    if not payer_note:
        raise ApiError("E_NO_NOTE", "请填写付款人昵称，否则无法核对到账")
    if len(payer_note) > 60:
        raise ApiError("E_NO_NOTE", "付款人昵称过长")
    # paid_at / proof_url 会原样入库并进后台列表，必须设上限：
    # 用户误把长文本粘进来时，库会膨胀，后台表格也会被撑坏。
    if paid_at and len(paid_at) > 32:
        raise ApiError("E_BAD_PAID_AT", "转账时间格式不对（如 2026-10-27 10:30）")
    if proof_url and len(proof_url) > 500:
        raise ApiError("E_BAD_PROOF", "凭证链接过长")

    ch = ManualChannel()
    try:
        rec = ch.create_order(
            user["id"],
            amount_mill,
            pay_method=pay_method,
            payer_note=payer_note,
            paid_at=paid_at,
            proof_url=proof_url,
        )
    except ValueError as e:
        raise ApiError("E_PENDING_EXISTS", str(e), 409) from e

    audit.log_access("recharge_submit", user["id"], client_ip(request), rec["id"],
                     f"amount={amount_mill},method={pay_method}")
    audit.event("recharge_submit", user["id"], rec["id"], amount_mill=amount_mill)
    return ok({**rec, "status_label": RCG_STATUS_LABEL["submitted"],
               "message": f"申请已提交（{rec['order_no']}），管理员核对后到账"})


@router.get("/orders")
def my_orders(request: Request, limit: int = 20):
    user = get_current_user(request)
    rows = q(
        "SELECT * FROM recharge_orders WHERE user_id=:u ORDER BY created_at DESC LIMIT :l",
        u=user["id"], l=min(limit, 100),
    )
    return ok(
        {
            "items": [
                {
                    "id": r["id"],
                    "order_no": r["order_no"],
                    "amount_mill": int(r["amount_mill"]),
                    "amount_text": f"¥{mill_to_yuan(int(r['amount_mill']))}",
                    "status": r["status"],
                    "status_label": RCG_STATUS_LABEL.get(r["status"], r["status"]),
                    "reject_reason": r["reject_reason"],
                    "created_at": r["created_at"],
                    "confirmed_at": r["confirmed_at"],
                }
                for r in rows
            ]
        }
    )


@router.post("/orders/{order_id}/close")
def close_order(request: Request, order_id: str):
    """用户撤销未处理的申请单（PRD §9.2 允许）。"""
    user = get_current_user(request)
    row = q1("SELECT * FROM recharge_orders WHERE id=:id AND user_id=:u", id=order_id, u=user["id"])
    if not row:
        raise ApiError("E_NOT_FOUND", "充值单不存在", 404)
    if row["status"] != "submitted":
        raise ApiError("E_BAD_STATE", "该单已处理，不能撤销")
    from sqlalchemy import text

    from ..db import tx

    with tx() as conn:
        conn.execute(
            text("UPDATE recharge_orders SET status='closed' WHERE id=:id AND status='submitted'"),
            {"id": order_id},
        )
    return ok({"status": "closed"})
