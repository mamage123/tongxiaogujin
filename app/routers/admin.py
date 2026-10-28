"""管理后台 API（FR-ADM 全组 /admin/api/*）。

三条纪律：
1. 独立登录入口，校验 role=admin，**禁止自助注册管理员**；
2. 一切资金操作必须留痕（操作人、对象、前后值）；
3. 确认到账必须幂等（重复点击只入账一次）。
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, File, Request, UploadFile
from sqlalchemy import text

from ..config import settings
from ..db import get_setting, q, q1, set_setting, tx
from ..deps import ApiError, client_ip, get_admin, ok
from ..enums import RCG_STATUS_LABEL, TASK_STATUS_LABEL
from ..services import audit
from ..services.billing import (
    _change_balance,
    _insert_ledger,
    check_ledger_integrity,
    daily_reconcile,
    expire_balance,
)
from ..services.cleanup import cleanup_expired, cleanup_records
from ..services.compliance import check_keywords
from ..services.payment.manual import AlreadyDone, ManualChannel
from ..utils import mill_to_yuan, now_iso

router = APIRouter(prefix="/admin/api", tags=["admin"])


def _day_range(date_str: str | None) -> tuple[str, str]:
    d = date_str or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return d, f"{d}T99"


# ---------------------------------------------------------------- 看板
@router.get("/dashboard")
def dashboard(request: Request):
    admin = get_admin(request)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    d1, d2 = _day_range(today)

    def _one(sql: str, **p):
        row = q1(sql, **p)
        return int(list(row.values())[0]) if row else 0

    new_users = _one("SELECT COUNT(*) FROM users WHERE created_at>=:d1", d1=d1)
    new_tasks = _one("SELECT COUNT(*) FROM tasks WHERE created_at>=:d1", d1=d1)
    new_chars = _one("SELECT COALESCE(SUM(chars),0) FROM tasks WHERE created_at>=:d1", d1=d1)
    recharge = _one(
        "SELECT COALESCE(SUM(amount_mill),0) FROM ledger WHERE type='recharge' AND created_at>=:d1", d1=d1
    )
    consume = _one(
        "SELECT COALESCE(SUM(-amount_mill),0) FROM ledger WHERE type='consume' AND created_at>=:d1", d1=d1
    )
    pending_orders = _one("SELECT COUNT(*) FROM recharge_orders WHERE status='submitted'")

    # 行序抽检通过率（M-10 机器初筛 + 人工标记）
    oc = q1("SELECT AVG(order_check_pass_rate) AS v FROM tasks WHERE order_check_pass_rate IS NOT NULL")
    order_avg = round(float(oc["v"]), 4) if oc and oc["v"] is not None else None
    samples = q1("SELECT COUNT(*) AS n, AVG(order_ok) AS o FROM quality_samples WHERE order_ok IS NOT NULL")
    manual_order = round(float(samples["o"]), 4) if samples and samples["o"] is not None else None

    split_hit = _one("SELECT COALESCE(SUM(split_pages),0) FROM tasks WHERE split_pages>0")

    export_dist = q(
        "SELECT kind, COUNT(*) AS n FROM files WHERE created_at>=:d1 GROUP BY kind ORDER BY n DESC", d1=d1
    )

    integrity = check_ledger_integrity()

    return ok(
        {
            "today": today,
            "new_users": new_users,
            "new_tasks": new_tasks,
            "new_chars": new_chars,
            "recharge_mill": recharge,
            "recharge_text": f"¥{mill_to_yuan(recharge)}",
            "consume_mill": consume,
            "consume_text": f"¥{mill_to_yuan(consume)}",
            "pending_orders": pending_orders,
            "order_check_avg": order_avg,
            "order_check_manual": manual_order,
            "split_pages": split_hit,
            "export_dist": [{"kind": r["kind"], "count": int(r["n"])} for r in export_dist],
            "ledger_integrity": {"ok": integrity["ok"], "checked": integrity["checked"],
                                 "mismatches": integrity["mismatches"][:5]},
        }
    )


# ---------------------------------------------------------------- 用户
@router.get("/users")
def list_users(request: Request, kw: str = "", limit: int = 50, offset: int = 0):
    get_admin(request)
    where = "WHERE 1=1"
    page_size = min(limit, 200)
    params: dict = {"l": page_size, "o": max(0, offset)}
    count_params: dict = {}
    if kw.strip():
        where += " AND (username LIKE :kw OR email LIKE :kw OR id LIKE :kw)"
        params["kw"] = f"%{kw.strip()}%"
        count_params["kw"] = params["kw"]

    # 必须回总数，否则前端无法判断"还有没有下一页"，
    # 结果就是用户超过 limit 后，第 51 个以后的人在后台彻底消失、查不到也管不了。
    total = int(q1(f"SELECT COUNT(*) AS n FROM users u {where}", **count_params)["n"])

    rows = q(
        f"SELECT u.*, (SELECT COUNT(*) FROM tasks t WHERE t.user_id=u.id) AS task_count,"
        f" (SELECT COALESCE(SUM(-l.amount_mill),0) FROM ledger l WHERE l.user_id=u.id AND l.type='consume') AS consumed"
        f" FROM users u {where} ORDER BY u.created_at DESC LIMIT :l OFFSET :o",
        **params,
    )
    return ok(
        {
            "total": total,
            "offset": max(0, offset),
            "limit": page_size,
            "items": [
                {
                    "id": r["id"],
                    "username": r["username"],
                    "email": r["email"],
                    "role": r["role"],
                    "balance_mill": int(r["balance_mill"]),
                    "balance_text": f"¥{mill_to_yuan(int(r['balance_mill']))}",
                    "consumed_mill": int(r["consumed"] or 0),
                    "consumed_text": f"¥{mill_to_yuan(int(r['consumed'] or 0))}",
                    "free_left": max(0, int(r["free_pages_total"]) - int(r["free_pages_used"])),
                    "task_count": int(r["task_count"] or 0),
                    "status": r["status"],
                    "risk_flag": int(r["risk_flag"] or 0),
                    "device_prefix": (r["device_hash"] or "")[:8],
                    # 排查刷号/误伤必须看来源：同一 IP/设备的账号会在列表里排在一起
                    "reg_ip": r["reg_ip"] or "",
                    "created_at": r["created_at"],
                    "last_recharge_at": r["last_recharge_at"],
                }
                for r in rows
            ],
        }
    )


@router.get("/users/{user_id}")
def user_detail(request: Request, user_id: str):
    get_admin(request)
    u = q1("SELECT * FROM users WHERE id=:id", id=user_id)
    if not u:
        raise ApiError("E_NOT_FOUND", "用户不存在", 404)
    ledger = q("SELECT * FROM ledger WHERE user_id=:u ORDER BY id DESC LIMIT 30", u=user_id)
    tasks = q("SELECT id,status,chars,amount_mill,created_at FROM tasks WHERE user_id=:u ORDER BY created_at DESC LIMIT 20", u=user_id)
    return ok(
        {
            "user": {k: u[k] for k in u if k != "pwd_hash"},
            "ledger": ledger,
            "tasks": [dict(t, status_label=TASK_STATUS_LABEL.get(t["status"], t["status"])) for t in tasks],
        }
    )


@router.post("/users/{user_id}/balance")
def adjust_balance(request: Request, user_id: str, payload: dict):
    """手工调整余额（FR-RCG-06）：必填原因与凭证号，记录前后值。"""
    admin = get_admin(request)
    delta_mill = int(payload.get("delta_mill") or 0)
    reason = (payload.get("reason") or "").strip()
    voucher = (payload.get("voucher") or "").strip()

    if delta_mill == 0:
        raise ApiError("E_BAD_AMOUNT", "调整金额不能为 0")
    if not reason:
        raise ApiError("E_NO_REASON", "必须填写调整原因")
    if not voucher:
        raise ApiError("E_NO_VOUCHER", "必须填写凭证号（充值单号/转账流水号）")

    u = q1("SELECT * FROM users WHERE id=:id", id=user_id)
    if not u:
        raise ApiError("E_NOT_FOUND", "用户不存在", 404)

    with tx() as conn:
        before, after = _change_balance(conn, user_id, delta_mill)
        _insert_ledger(
            conn, user_id, "adjust", delta_mill, before, after, "admin", admin["id"],
            f"{reason}（凭证 {voucher}）",
        )
    audit.log_admin(admin["id"], "balance_adjust", "user", user_id,
                    {"balance": u["balance_mill"]}, {"balance": after, "reason": reason, "voucher": voucher})
    audit.log_access("balance_adjust", admin["id"], client_ip(request), user_id, f"{delta_mill}")
    return ok({"balance_before_mill": before, "balance_after_mill": after,
               "balance_after_text": f"¥{mill_to_yuan(after)}"})


@router.post("/users/{user_id}/status")
def set_user_status(request: Request, user_id: str, payload: dict):
    admin = get_admin(request)
    status = payload.get("status")
    if status not in ("active", "disabled"):
        raise ApiError("E_BAD_STATUS", "状态不合法")
    u = q1("SELECT * FROM users WHERE id=:id", id=user_id)
    if not u:
        raise ApiError("E_NOT_FOUND", "用户不存在", 404)

    # 两条防呆，都来自真实可发生的锁死场景：
    # 1) 停用自己 —— 下一次请求起就 401，后台再也进不去；
    # 2) 停用最后一个可用的管理员 —— 全站没有能确认到账/对账的人了。
    if status == "disabled":
        if user_id == admin["id"]:
            raise ApiError("E_SELF_DISABLE", "不能停用当前登录的管理员账号")
        if u["role"] == "admin":
            others = q1(
                "SELECT COUNT(*) AS n FROM users WHERE role='admin' AND status='active' AND id<>:id",
                id=user_id,
            )
            if others and int(others["n"]) == 0:
                raise ApiError("E_LAST_ADMIN", "这是最后一个可用的管理员，不能停用")

    with tx() as conn:
        conn.execute(text("UPDATE users SET status=:s WHERE id=:id"), {"s": status, "id": user_id})
    audit.log_admin(admin["id"], "user_status", "user", user_id, {"status": u["status"]}, {"status": status})
    return ok({"status": status})


@router.post("/users/{user_id}/grant-free")
def grant_free(request: Request, user_id: str, payload: dict):
    """给用户补发免费额度（EX-20：超限未发额度时的人工开通）。"""
    admin = get_admin(request)
    pages = int(payload.get("pages") or 0)
    if pages <= 0 or pages > 50:
        raise ApiError("E_BAD_PAGES", "额度需在 1-50 页之间")
    u = q1("SELECT * FROM users WHERE id=:id", id=user_id)
    if not u:
        raise ApiError("E_NOT_FOUND", "用户不存在", 404)
    new_total = int(u["free_pages_total"]) + pages
    with tx() as conn:
        conn.execute(
            text("UPDATE users SET free_pages_total=:t, risk_flag=0 WHERE id=:id"),
            {"t": new_total, "id": user_id},
        )
    audit.log_admin(admin["id"], "grant_free", "user", user_id,
                    {"free_pages_total": u["free_pages_total"]}, {"free_pages_total": new_total})
    return ok({"free_pages_total": new_total})


# ---------------------------------------------------------------- 充值单
@router.get("/recharge")
def list_orders(request: Request, status: str = "submitted", limit: int = 50):
    get_admin(request)
    if status == "all":
        rows = q(
            "SELECT r.*, u.username FROM recharge_orders r JOIN users u ON u.id=r.user_id "
            "ORDER BY r.created_at DESC LIMIT :l", l=min(limit, 200),
        )
    else:
        rows = q(
            "SELECT r.*, u.username FROM recharge_orders r JOIN users u ON u.id=r.user_id "
            "WHERE r.status=:s ORDER BY r.created_at DESC LIMIT :l", s=status, l=min(limit, 200),
        )
    return ok(
        {
            "items": [
                {
                    "id": r["id"],
                    "order_no": r["order_no"],
                    "user_id": r["user_id"],
                    "username": r["username"],
                    "amount_mill": int(r["amount_mill"]),
                    "amount_text": f"¥{mill_to_yuan(int(r['amount_mill']))}",
                    "pay_method": r["pay_method"],
                    "payer_note": r["payer_note"],
                    "paid_at": r["paid_at"],
                    "proof_url": r["proof_url"],
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


@router.post("/recharge/{order_id}/confirm")
def confirm_order(request: Request, order_id: str, payload: dict | None = None):
    """确认到账。连点两次只入账一次（FR-RCG-03 / EX-18）。

    payload 必须可省略：前端确认到账不传 body，声明成必填 dict 时
    FastAPI 会直接 422，管理员点「确认到账」永远失败（2026-10-28 踩过）。
    """
    admin = get_admin(request)
    ch = ManualChannel()
    try:
        res = ch.handle_callback(
            {
                "order_id": order_id,
                "admin_id": admin["id"],
                "idempotency_key": f"manual|{admin['id']}|{order_id}",
            }
        )
    except AlreadyDone as e:
        raise ApiError("E_ALREADY_DONE", str(e), 409) from e
    except ValueError as e:
        raise ApiError("E_BAD_STATE", str(e)) from e

    audit.log_admin(admin["id"], "recharge_confirm", "recharge_order", order_id, None, res)
    audit.log_access("recharge_confirm", admin["id"], client_ip(request), order_id)
    return ok(res)


@router.post("/recharge/{order_id}/reject")
def reject_order(request: Request, order_id: str, payload: dict | None = None):
    admin = get_admin(request)
    reason = (payload or {}).get("reason") or ""
    ch = ManualChannel()
    try:
        res = ch.reject(order_id, admin["id"], reason)
    except AlreadyDone as e:
        raise ApiError("E_ALREADY_DONE", str(e), 409) from e
    except ValueError as e:
        raise ApiError("E_BAD_STATE", str(e)) from e
    except Exception as e:  # MissingReason
        raise ApiError("E_NO_REASON", str(e)) from e

    audit.log_admin(admin["id"], "recharge_reject", "recharge_order", order_id, None,
                    {"reason": reason})
    return ok(res)


# ---------------------------------------------------------------- 台账与对账
@router.get("/ledger")
def ledger_all(request: Request, date: str = "", user_id: str = "", type: str = "", limit: int = 100):
    get_admin(request)
    where = "WHERE 1=1"
    p: dict = {"l": min(limit, 500)}
    if date:
        where += " AND l.created_at>=:d1 AND l.created_at<:d2"
        p["d1"], p["d2"] = _day_range(date)
    if user_id:
        where += " AND l.user_id=:u"
        p["u"] = user_id
    if type:
        where += " AND l.type=:ty"
        p["ty"] = type
    rows = q(
        f"SELECT l.*, u.username FROM ledger l JOIN users u ON u.id=l.user_id {where} ORDER BY l.id DESC LIMIT :l",
        **p,
    )
    return ok({"items": rows})


@router.get("/ledger/daily")
def daily(request: Request, date: str = ""):
    """每日对账视图（FR-ADM-05）：期初 + 充值 − 消费 − 退款 + 调整 = 期末。"""
    get_admin(request)
    d = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rec = daily_reconcile(d)
    rec["consume_abs"] = -rec["consume"]
    rec["text"] = {
        "begin": f"¥{mill_to_yuan(rec['begin_total'])}",
        "recharge": f"¥{mill_to_yuan(rec['recharge'])}",
        "consume": f"¥{mill_to_yuan(-rec['consume'])}",
        "refund": f"¥{mill_to_yuan(rec['refund'])}",
        "adjust": f"¥{mill_to_yuan(rec['adjust'])}",
        "end": f"¥{mill_to_yuan(rec['end_total'])}",
    }
    return ok(rec)


@router.get("/integrity")
def integrity(request: Request):
    """资损监控：台账累加必须等于用户余额。"""
    get_admin(request)
    return ok(check_ledger_integrity())


# ---------------------------------------------------------------- 任务
@router.get("/tasks")
def list_tasks(request: Request, status: str = "", limit: int = 50):
    get_admin(request)
    if status:
        rows = q(
            "SELECT t.*, u.username FROM tasks t JOIN users u ON u.id=t.user_id "
            "WHERE t.status=:s ORDER BY t.created_at DESC LIMIT :l", s=status, l=min(limit, 200),
        )
    else:
        rows = q(
            "SELECT t.*, u.username FROM tasks t JOIN users u ON u.id=t.user_id "
            "ORDER BY t.created_at DESC LIMIT :l", l=min(limit, 200),
        )
    return ok(
        {
            "items": [
                {
                    "id": r["id"],
                    "short_id": r["id"][:8],
                    "username": r["username"],
                    "filename": r["filename"],
                    "pages": int(r["pages_total"]),
                    "mode": r["mode"],
                    "chars": int(r["chars"]),
                    "amount_text": f"¥{mill_to_yuan(int(r['amount_mill']))}",
                    "status": r["status"],
                    "status_label": TASK_STATUS_LABEL.get(r["status"], r["status"]),
                    "error_code": r["error_code"],
                    "split_pages": int(r["split_pages"]),
                    "order_check_pass_rate": r["order_check_pass_rate"],
                    "created_at": r["created_at"],
                }
                for r in rows
            ]
        }
    )


# ---------------------------------------------------------------- 质量抽检
@router.post("/quality/sample")
def quality_sample(request: Request, payload: dict):
    """质量抽检（FR-ADM-09）：人工标记行序/拆分/框定位，用于持续度量 M-10/11/12。"""
    admin = get_admin(request)
    task_id = payload.get("task_id") or ""
    page_no = int(payload.get("page_no") or 0)
    if not task_id or page_no < 1:
        raise ApiError("E_BAD_PARAM", "缺少任务或页号")
    if not q1("SELECT 1 AS x FROM task_pages WHERE task_id=:t AND page_no=:p", t=task_id, p=page_no):
        raise ApiError("E_NOT_FOUND", "该页不存在", 404)

    def _tri(v):
        return None if v is None else (1 if v else 0)

    with tx() as conn:
        conn.execute(
            text(
                "INSERT INTO quality_samples(task_id,page_no,checker,order_ok,split_ok,bbox_ok,remark,created_at)"
                " VALUES(:t,:p,:c,:o,:s,:b,:r,:ca)"
            ),
            {
                "t": task_id, "p": page_no, "c": admin["id"],
                "o": _tri(payload.get("order_ok")),
                "s": _tri(payload.get("split_ok")),
                "b": _tri(payload.get("bbox_ok")),
                "r": payload.get("remark"),
                "ca": now_iso(),
            },
        )
    return ok(_quality_stats())


@router.get("/quality/stats")
def quality_stats(request: Request):
    get_admin(request)
    return ok(_quality_stats())


def _quality_stats() -> dict:
    def _rate(col: str) -> dict:
        row = q1(f"SELECT COUNT(*) AS n, SUM({col}) AS ok FROM quality_samples WHERE {col} IS NOT NULL")
        n = int(row["n"] or 0) if row else 0
        ok = int(row["ok"] or 0) if row and row["ok"] is not None else 0
        return {"n": n, "ok": ok, "rate": round(ok / n, 4) if n else None}

    return {
        "M-10_order": {**_rate("order_ok"), "target": 0.98},
        "M-11_split": {**_rate("split_ok"), "target": 0.95},
        "M-12_bbox": {**_rate("bbox_ok"), "target": 0.99},
    }


# ---------------------------------------------------------------- 导出健康度
@router.get("/exports/health")
def export_health(request: Request):
    get_admin(request)
    rows = q(
        "SELECT kind, COUNT(*) AS n, SUM(success) AS ok, AVG(duration_ms) AS dur, "
        "AVG(size_bytes) AS size FROM export_logs GROUP BY kind"
    )
    out = []
    for r in rows:
        n = int(r["n"] or 0)
        ok_n = int(r["ok"] or 0)
        out.append(
            {
                "kind": r["kind"],
                "total": n,
                "success": ok_n,
                "success_rate": round(ok_n / n, 4) if n else None,
                "avg_ms": int(r["dur"] or 0),
                "avg_size": int(r["size"] or 0),
                "below_target": bool(n >= 5 and ok_n / n < 0.995),
            }
        )
    fails = q(
        "SELECT kind, error_code, COUNT(*) AS n FROM export_logs WHERE success=0 "
        "GROUP BY kind, error_code ORDER BY n DESC LIMIT 20"
    )
    return ok({"by_kind": out, "failures": fails})


# ---------------------------------------------------------------- 开关与设置
@router.post("/settings/ocr")
def set_ocr_settings(request: Request, payload: dict):
    """配置星河 OCR 的 token / endpoint / model（写入 settings 表，优先于 .env）。

    token 是敏感凭证：审计日志里只记「是否变更 + 前后尾 4 位」，不落明文。
    """
    admin = get_admin(request)

    def _mask(v: str) -> str:
        v = (v or "").strip()
        return ("…" + v[-4:]) if len(v) > 8 else ("(已设)" if v else "")

    changed = {}
    for key, field in (("aistudio_token", "token"), ("aistudio_endpoint", "endpoint"), ("aistudio_model", "model")):
        if field in payload:
            val = str(payload[field] or "").strip()
            before = get_setting(key, "")
            if val != before:
                set_setting(key, val)
                changed[key] = {"before": _mask(before), "after": _mask(val)}

    if changed:
        audit.log_admin(admin["id"], "ocr_setting", "settings", None, changed, None)
    return ok({
        "aistudio_token": _mask(get_setting("aistudio_token", "")),
        "aistudio_endpoint": get_setting("aistudio_endpoint", ""),
        "aistudio_model": get_setting("aistudio_model", ""),
    })


@router.post("/settings/bbox")
def toggle_bbox(request: Request, payload: dict):
    """框层全局开关与灰度（FR-RES-12 / FR-ADM-10）：30 秒内生效。"""
    admin = get_admin(request)
    before = {
        "bbox_global_enabled": get_setting("bbox_global_enabled", "1"),
        "bbox_gray_ratio": get_setting("bbox_gray_ratio", "1.0"),
    }
    if "enabled" in payload:
        set_setting("bbox_global_enabled", "1" if payload["enabled"] else "0")
    if "gray_ratio" in payload:
        v = float(payload["gray_ratio"])
        if not 0 <= v <= 1:
            raise ApiError("E_BAD_VALUE", "灰度比例需在 0~1")
        set_setting("bbox_gray_ratio", str(v))
    after = {
        "bbox_global_enabled": get_setting("bbox_global_enabled", "1"),
        "bbox_gray_ratio": get_setting("bbox_gray_ratio", "1.0"),
    }
    audit.log_admin(admin["id"], "bbox_toggle", "settings", None, before, after)
    return ok(after)


@router.post("/settings/pay-qr")
async def upload_pay_qr(request: Request, method: str = "wechat", file: UploadFile = File(...)):
    """上传收款码。人工充值没有二维码就收不到钱，所以这条必须能在后台自助完成。

    存到 storage_dir/pay/，通过公开路由 /pay/qr/{method} 读取（二维码本来就是公开信息）。
    """
    admin = get_admin(request)
    if method not in ("wechat", "alipay"):
        raise ApiError("E_BAD_METHOD", "收款方式不正确")
    ext = Path(file.filename or "qr.png").suffix.lower()
    if ext not in (".png", ".jpg", ".jpeg", ".webp"):
        raise ApiError("E_BAD_TYPE", "请上传 PNG / JPG / WebP 图片")

    data = await file.read()
    if len(data) > 2 * 1024 * 1024:
        raise ApiError("E_TOO_LARGE", "图片不能超过 2MB", 413)

    pay_dir = settings.storage_dir / "pay"
    pay_dir.mkdir(parents=True, exist_ok=True)
    for old in pay_dir.glob(f"{method}.*"):
        old.unlink(missing_ok=True)
    dest = pay_dir / f"{method}{ext}"
    dest.write_bytes(data)

    url = f"/pay/qr/{method}?v={int(time.time())}"  # 带版本号破缓存
    set_setting(f"pay_{method}_qr", url)
    audit.log_admin(admin["id"], "pay_qr_upload", "settings", f"pay_{method}_qr",
                    None, {"size": len(data), "ext": ext})
    return ok({"url": url, "size": len(data)})


@router.get("/settings")
def settings_list(request: Request):
    get_admin(request)
    return ok(q("SELECT key,value,updated_at FROM settings ORDER BY key"))


@router.post("/settings")
def settings_set(request: Request, payload: dict):
    admin = get_admin(request)
    key = (payload.get("key") or "").strip()
    value = str(payload.get("value") if payload.get("value") is not None else "")
    if not key:
        raise ApiError("E_BAD_KEY", "缺少配置项名称")
    before = get_setting(key, "")
    set_setting(key, value)
    audit.log_admin(admin["id"], "setting_set", "settings", key, {"value": before}, {"value": value})
    return ok({"key": key, "value": value})


# ---------------------------------------------------------------- 运维
@router.post("/cleanup/run")
def run_cleanup(request: Request):
    admin = get_admin(request)
    res = cleanup_expired()
    audit.log_admin(admin["id"], "cleanup", "system", None, None, res)
    return ok(res)


@router.get("/cleanup/records")
def cleanup_recs(request: Request):
    get_admin(request)
    return ok(cleanup_records())


@router.post("/expire-balance")
def run_expire_balance(request: Request):
    admin = get_admin(request)
    n = expire_balance()
    audit.log_admin(admin["id"], "expire_balance", "system", None, None, {"users": n})
    return ok({"users_cleared": n})


@router.get("/logs")
def logs(request: Request, kind: str = "admin", limit: int = 100):
    get_admin(request)
    if kind == "admin":
        return ok(q("SELECT * FROM admin_logs ORDER BY id DESC LIMIT :l", l=min(limit, 500)))
    return ok(q("SELECT * FROM access_logs ORDER BY id DESC LIMIT :l", l=min(limit, 500)))


@router.get("/events")
def events(request: Request, name: str = "", limit: int = 100):
    get_admin(request)
    if name:
        return ok(q("SELECT * FROM events WHERE name=:n ORDER BY id DESC LIMIT :l", n=name, l=limit))
    return ok(q("SELECT name, COUNT(*) AS n FROM events GROUP BY name ORDER BY n DESC LIMIT 50"))
