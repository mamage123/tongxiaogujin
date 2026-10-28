"""计费查询 API（只读：预估、流水、额度）。扣费一律在 worker 结算里发生。"""
from __future__ import annotations

from fastapi import APIRouter, Request

from ..config import settings
from ..db import q1, setting_int
from ..deps import ApiError, get_current_user, ok
from ..services.billing import estimate as billing_estimate
from ..services.billing import user_ledger
from ..utils import mill_to_yuan

router = APIRouter(prefix="/api/billing", tags=["billing"])


@router.get("/estimate")
def estimate(request: Request, pages: int = 1, task_id: str | None = None):
    """开跑前预估上限（FR-BIL-03）。也可传 task_id 查已完成任务的明细。"""
    user = get_current_user(request)
    if task_id:
        task = q1("SELECT * FROM tasks WHERE id=:id AND user_id=:u", id=task_id, u=user["id"])
        if not task:
            raise ApiError("E_NOT_FOUND", "任务不存在", 404)
        return ok(
            {
                "task_id": task_id,
                "chars": int(task["chars"]),
                "amount_mill": int(task["amount_mill"]),
                "amount_text": f"¥{mill_to_yuan(int(task['amount_mill']))}",
                "free_pages_applied": int(task["free_pages_applied"]),
                "per_10k_mill": setting_int("price_per_10k_mill", settings.price_per_10k_mill),
            }
        )
    if pages < 1:
        raise ApiError("E_BAD_PAGES", "页数必须 ≥1")
    return ok(billing_estimate(user, pages))


@router.get("/ledger")
def ledger(request: Request, limit: int = 50, offset: int = 0):
    user = get_current_user(request)
    rows = user_ledger(user["id"], limit=min(limit, 200), offset=offset)
    out = []
    for r in rows:
        out.append(
            {
                "id": r["id"],
                "type": r["type"],
                "amount_mill": int(r["amount_mill"]),
                "amount_text": f"{'+' if int(r['amount_mill']) > 0 else ''}¥{mill_to_yuan(int(r['amount_mill']))}",
                "balance_after_mill": int(r["balance_after"]),
                "balance_after_text": f"¥{mill_to_yuan(int(r['balance_after']))}",
                "ref_type": r["ref_type"],
                "ref_id": r["ref_id"],
                "task_short": (r["ref_id"] or "")[:8] if r["ref_type"] == "task" else "",
                "remark": r["remark"],
                "created_at": r["created_at"],
            }
        )
    return ok({"items": out, "limit": limit, "offset": offset})


@router.get("/rules")
def rules():
    """计费规则（前端展示用，文案必须与 PRD 一致）。"""
    price = setting_int("price_per_10k_mill", settings.price_per_10k_mill)
    min_chars = setting_int("min_bill_chars", settings.min_bill_chars)
    return ok(
        {
            "per_10k_mill": price,
            "per_10k_text": f"¥{mill_to_yuan(price)} / 万字",
            "min_bill_chars": min_chars,
            "min_bill_text": f"最低按 {min_chars} 字计费",
            "examples": [
                {"chars": 12340, "amount_text": f"¥{mill_to_yuan(12340 * price // 10000 if 12340 * price % 10000 == 0 else 12340 * price // 10000 + 1)}"},
                {"chars": 480, "amount_text": f"¥{mill_to_yuan(min_chars * price // 10000)}"},
            ],
            "free_rule": "新账号赠 5 页免费额度，不分字数与模式；失败页与空白页不消耗",
            "no_charge": ["双页拆分", "裁边", "行序还原", "格式优化", "框层", "全部导出格式"],
            "refund": "识别失败全额退回余额；余额不可提现、不可转让；平台停止服务时未使用余额全额退还",
        }
    )
