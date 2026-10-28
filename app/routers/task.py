"""任务：创建、查询、取消、重试失败页、页图、块结构。

创建任务是资金与体验的交汇点，六道校验顺序不能乱：
1. 上传会话有效（文件真的传完了）
2. 文件可解析（加密/损坏在这里最后一次兜）
3. 选页合法 + 页数上限（余额 ≤¥1 时 10 页）
4. 并发限制（同用户仅 1 个进行中任务，FR-OCR-08）
5. 60 秒幂等去重（EX-14）
6. 余额/额度准入（余额 0 且额度用尽 → 开跑前拦截，EX-09）
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse
from sqlalchemy import text

from ..config import settings
from ..db import q, q1, setting_int, tx
from ..deps import ApiError, client_ip, err, get_current_user, ok
from ..enums import MODE, can_cancel, can_download, can_retry_page, can_view_text
from ..services import audit
from ..services.billing import estimate as billing_estimate
from ..services.preprocess import PreprocessError, compact_page_range, parse_page_range, probe
from ..services.worker import active_task_count, init_pages, retry_page
from ..utils import hours_later, new_id, now_iso

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


def _task_or_404(task_id: str, user: dict, admin: bool | None = None) -> dict:
    """取任务并校验归属。admin 默认按角色判定。

    后台「任务」列表里每个任务号都是 `/task/{id}` 链接（admin.html 里 `target=_blank`），
    管理员点进去要能看到别人任务的详情、原图和块坐标 —— 那是客服排查的唯一入口。
    这里曾经写死 `admin=False`，结果后台点任务号开出来是页面 200、数据全 403 的空白页。

    写操作（cancel / retry）**故意不走** admin 豁免：管理员误取消用户任务、
    或替用户重试导致重复扣费，都是不可撤销的。
    """
    if admin is None:
        admin = user.get("role") == "admin"
    task = q1("SELECT * FROM tasks WHERE id=:id", id=task_id)
    if not task:
        raise ApiError("E_NOT_FOUND", "任务不存在", 404)
    if not admin and task["user_id"] != user["id"]:
        raise ApiError("E_FORBIDDEN", "无权访问该任务", 403)
    return task


def task_public(task: dict) -> dict:
    return {
        "id": task["id"],
        "short_id": task["id"][:8],
        "filename": task["filename"],
        "file_type": task["file_type"],
        "pages_total": int(task["pages_total"]),
        "mode": task["mode"],
        "optimize": bool(task["optimize"]),
        "status": task["status"],
        "chars": int(task["chars"]),
        "amount_mill": int(task["amount_mill"]),
        "amount_text": f"¥{int(task['amount_mill']) / 1000:.2f}",
        "pending_mill": int(task["pending_mill"]),
        "pending_text": f"¥{int(task['pending_mill']) / 1000:.2f}",
        "free_pages_applied": int(task["free_pages_applied"]),
        "is_rerun": bool(task["is_rerun"]),
        "rerun_free": bool(task["rerun_free"]),
        "order_check_pass_rate": task["order_check_pass_rate"],
        "error_code": task["error_code"],
        "created_at": task["created_at"],
        "expire_at": task["expire_at"],
        "can_view_text": can_view_text(task["status"]),
        "can_download": can_download(task["status"]),
        "can_cancel": can_cancel(task["status"]),
    }


# ---------------------------------------------------------------- 创建
@router.post("")
def create_task(request: Request, payload: dict):
    user = get_current_user(request)
    upload_id = payload.get("upload_id") or ""
    mode = payload.get("mode") or "ancient"
    optimize = 1 if payload.get("optimize", True) else 0
    page_range = (payload.get("page_range") or "all").strip()

    if mode not in MODE:
        raise ApiError("E_BAD_MODE", "识别模式不正确")

    sess = q1("SELECT * FROM upload_sessions WHERE id=:id AND user_id=:u", id=upload_id, u=user["id"])
    if not sess:
        raise ApiError("E_NO_SESSION", "请重新上传文件")
    if not sess["done"]:
        raise ApiError("E_INCOMPLETE", "文件尚未上传完成")
    src = Path(sess["dir_path"]) / f"merged{Path(sess['filename']).suffix.lower()}"
    if not src.exists():
        raise ApiError("E_NO_FILE", "上传的文件已丢失，请重新上传")

    # ---- 文件可解析
    try:
        ftype, pages, sizes = probe(str(src))
        selected = parse_page_range(page_range, pages)
    except PreprocessError as e:
        raise ApiError(e.code, e.message) from e

    # ---- 页数上限（余额 ≤¥1 时 10 页）
    low = int(user["balance_mill"]) <= setting_int("low_balance_mill", settings.low_balance_mill)
    limit = setting_int(
        "max_pages_low_balance" if low else "max_pages_normal",
        settings.max_pages_low_balance if low else settings.max_pages,
    )
    if len(selected) > limit:
        msg = (
            f"余额较少时单次最多识别 {limit} 页，请减少页数或先充值"
            if low
            else f"单次最多 {limit} 页，可选页码范围（如 1-{limit}）分几次识别"
        )
        raise err("E_PAGE_LIMIT", msg, 400, limit=limit, selected=len(selected))

    # ---- 并发限制
    if active_task_count(user["id"]) > 0:
        raise ApiError("E_CONCURRENT", "你有任务正在处理中，请等待完成后再提交", 409)

    # ---- 60 秒幂等去重
    file_hash = sha256_cached(str(src), sess)
    bucket = str(int(time.time() // settings.dup_window_seconds))
    dup = q1(
        "SELECT id FROM tasks WHERE user_id=:u AND file_hash=:h AND dedup_bucket=:b",
        u=user["id"], h=file_hash, b=bucket,
    )
    if dup:
        raise err("E_DUP", "该文件已提交，请勿重复上传", 409, task_id=dup["id"])

    # ---- 余额/额度准入
    free_left = max(0, int(user["free_pages_total"]) - int(user["free_pages_used"]))
    prior = q1(
        "SELECT COUNT(*) AS n FROM tasks WHERE user_id=:u AND file_hash=:h", u=user["id"], h=file_hash
    )
    is_rerun = bool(prior and int(prior["n"]) > 0)
    rerun_free = 0
    rerun_hint = ""
    if is_rerun:
        rec = q1("SELECT * FROM rerun_records WHERE user_id=:u AND file_hash=:h", u=user["id"], h=file_hash)
        if not rec or not int(rec["free_rerun_used"]):
            rerun_free = 1
            rerun_hint = "本次为该文件的首次重跑，免费"
        else:
            rerun_hint = "该文件已使用过免费重跑，本次将按 ¥1/万字正常计费"

    if not rerun_free and len(selected) > free_left and int(user["balance_mill"]) <= 0:
        raise err(
            "E_NO_BALANCE",
            "免费额度已用完且余额为 0，请先充值（¥10 起）",
            402,
            free_left=free_left,
        )

    est_chars = max(0, len(selected) - (len(selected) if rerun_free else min(free_left, len(selected)))) * 400

    # ---- 建任务
    task_id = new_id()
    task_dir = settings.storage_dir / "tasks" / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    dest = task_dir / f"source{src.suffix.lower()}"
    shutil.move(str(src), str(dest))
    shutil.rmtree(Path(sess["dir_path"]), ignore_errors=True)

    with tx() as conn:
        conn.execute(
            text(
                "INSERT INTO tasks(id,user_id,filename,file_hash,file_type,pages_total,page_range,mode,"
                "optimize,status,chars,estimated_chars,amount_mill,pending_mill,free_pages_applied,"
                "is_rerun,rerun_free,source_path,created_at,expire_at,dedup_bucket)"
                " VALUES(:id,:u,:fn,:fh,:ft,:pt,:pr,:m,:opt,'created',0,:ec,0,0,0,:ir,:rf,:sp,:c,:e,:db)"
            ),
            {
                "id": task_id,
                "u": user["id"],
                "fn": sess["filename"],
                "fh": file_hash,
                "ft": ftype,
                "pt": len(selected),
                "pr": compact_page_range(selected)[:200],
                "m": mode,
                "opt": optimize,
                "ec": est_chars,
                "ir": 1 if is_rerun else 0,
                "rf": rerun_free,
                "sp": str(dest),
                "c": now_iso(),
                "e": hours_later(settings.file_ttl_hours),
                "db": bucket,
            },
        )
        if rerun_free:
            conn.execute(
                text(
                    "INSERT INTO rerun_records(user_id,file_hash,free_rerun_used,created_at)"
                    " VALUES(:u,:h,1,:c) ON CONFLICT(user_id,file_hash) DO UPDATE SET free_rerun_used=1"
                ),
                {"u": user["id"], "h": file_hash, "c": now_iso()},
            )

    # ---- 建页（只读尺寸，不渲染；渲染在 worker）
    try:
        n_pages = init_pages(task_id)
    except PreprocessError as e:
        with tx() as conn:
            conn.execute(text("UPDATE tasks SET status='failed', error_code=:c WHERE id=:id"),
                         {"c": e.code, "id": task_id})
        raise ApiError(e.code, e.message) from e

    # 双页合拍会让实际页数翻倍（1 张图 → 2 页），预估字数必须跟着重算。
    # 不重算的后果：est 按 1 页算 400 字，熔断线 = 400×1.5 = 600 字，
    # 而拆开后两页正常能出 700+ 字 → 每张双页图都会误触熔断变成 partial。
    if n_pages != len(selected):
        paying = 0 if rerun_free else n_pages - min(free_left, n_pages)
        with tx() as conn:
            conn.execute(
                text("UPDATE tasks SET estimated_chars=:ec WHERE id=:id"),
                {"ec": max(0, paying) * 400, "id": task_id},
            )

    # ---- created → preprocessing → queued
    with tx() as conn:
        conn.execute(text("UPDATE tasks SET status='queued' WHERE id=:id AND status='created'"),
                     {"id": task_id})

    audit.log_access("task_create", user["id"], client_ip(request), task_id,
                     f"pages={len(selected)},mode={mode},rerun_free={rerun_free}")
    audit.event("task_create", user["id"], task_id, mode=mode, pages=len(selected),
                rerun_free=rerun_free)

    task = q1("SELECT * FROM tasks WHERE id=:id", id=task_id)
    data = task_public(task)
    # 用 n_pages 而不是 len(selected)：双页合拍时 1 张图会变成 2 页，
    # 按原页数报预估会让用户以为只扣 1 页额度，结算时页面却显示 2 页。
    est = billing_estimate(user, n_pages, mode)
    data["estimate"] = est
    data["rerun_hint"] = rerun_hint
    return ok(data)


def sha256_cached(path: str, sess: dict) -> str:
    """合并后的文件 hash（complete 时算过一次，这里复用避免重复读大文件）。"""
    from ..utils import sha256_file

    cached = sess.get("file_hash")
    if cached:
        return cached
    return sha256_file(path)


# ---------------------------------------------------------------- 查询
@router.get("/{task_id}")
def get_task(request: Request, task_id: str):
    user = get_current_user(request)
    task = _task_or_404(task_id, user)
    data = task_public(task)

    pages = q(
        "SELECT page_no,source_page_no,is_split_half,status,chars,retry_count,order_check_passed,"
        "img_width,img_height,error_msg FROM task_pages WHERE task_id=:t ORDER BY page_no",
        t=task_id,
    )
    data["pages"] = [
        {
            "page_no": int(p["page_no"]),
            "source_page_no": int(p["source_page_no"]),
            "split_half": int(p["is_split_half"] or 0),
            "status": p["status"],
            "chars": int(p["chars"]),
            "retry_count": int(p["retry_count"]),
            "order_check_passed": p["order_check_passed"],
            "width": int(p["img_width"]),
            "height": int(p["img_height"]),
            "error": p["error_msg"],
        }
        for p in pages
    ]
    # 双页合拍已自动拆分时告诉前端，否则用户会以为「明明传了 1 张图，怎么收 2 页额度」
    data["spread_split"] = sum(1 for p in pages if int(p["is_split_half"] or 0) > 0) > 0
    data["progress"] = {
        "done": sum(1 for p in pages if p["status"] in ("done", "empty")),
        "failed": sum(1 for p in pages if p["status"] == "failed"),
        "total": len(pages),
    }
    # 文本（未结算也可见，FR-RES-04）
    if can_view_text(task["status"]):
        from ..services.export.markdown import build_plain_text

        data["text"] = build_plain_text(task_id, lang="simp")
        data["text_trad"] = build_plain_text(task_id, lang="trad")
    else:
        data["text"] = ""
        data["text_trad"] = ""
    return ok(data)


@router.get("")
def list_tasks(request: Request, limit: int = 20, offset: int = 0):
    user = get_current_user(request)
    rows = q(
        "SELECT * FROM tasks WHERE user_id=:u ORDER BY created_at DESC LIMIT :l OFFSET :o",
        u=user["id"], l=min(limit, 100), o=offset,
    )
    total = q1("SELECT COUNT(*) AS n FROM tasks WHERE user_id=:u", u=user["id"])
    return ok({"items": [task_public(t) for t in rows], "total": int(total["n"]) if total else 0})


# ---------------------------------------------------------------- 操作
@router.post("/{task_id}/cancel")
def cancel_task(request: Request, task_id: str):
    user = get_current_user(request)
    # 显式 admin=False：取消不可撤销，只能本人操作
    task = _task_or_404(task_id, user, admin=False)
    if not can_cancel(task["status"]):
        raise ApiError("E_BAD_STATE", "当前状态不允许取消")
    with tx() as conn:
        conn.execute(
            text("UPDATE tasks SET status='cancelled' WHERE id=:id AND status IN "
                 "('created','preprocessing','queued','processing')"),
            {"id": task_id},
        )
    audit.log_access("task_cancel", user["id"], client_ip(request), task_id)
    return ok({"status": "cancelled", "note": "已使用的免费额度不返还"})


@router.post("/{task_id}/pages/{page_no}/retry")
def retry_page_api(request: Request, task_id: str, page_no: int):
    user = get_current_user(request)
    # 显式 admin=False：重试会重新调用 OCR 并可能再次扣费，只能本人操作
    task = _task_or_404(task_id, user, admin=False)
    if not can_retry_page(task["status"]):
        raise ApiError("E_BAD_STATE", "当前状态不允许重试")
    try:
        res = retry_page(task_id, page_no)
    except ValueError as e:
        raise ApiError("E_RETRY_FAILED", str(e)) from e
    audit.log_access("page_retry", user["id"], client_ip(request), task_id, f"page={page_no}")
    return ok(res)


@router.get("/{task_id}/pages/{page_no}/image")
def page_image(request: Request, task_id: str, page_no: int):
    user = get_current_user(request)
    task = _task_or_404(task_id, user)
    if task["status"] == "expired":
        raise ApiError("E_EXPIRED", "文件已过期（保留 3 天），可免费重跑一次", 410)
    row = q1(
        "SELECT image_path FROM task_pages WHERE task_id=:t AND page_no=:p", t=task_id, p=page_no
    )
    if not row or not row["image_path"]:
        raise ApiError("E_NOT_FOUND", "该页图片不存在", 404)
    p = Path(row["image_path"])
    if not p.exists():
        raise ApiError("E_EXPIRED", "文件已过期（保留 3 天）", 410)
    return FileResponse(str(p), media_type="image/png", headers={"Cache-Control": "private, max-age=600"})


@router.get("/{task_id}/pages/{page_no}/blocks")
def page_blocks(request: Request, task_id: str, page_no: int):
    user = get_current_user(request)
    task = _task_or_404(task_id, user)
    row = q1(
        "SELECT img_width,img_height,status,order_check_passed FROM task_pages WHERE task_id=:t AND page_no=:p",
        t=task_id, p=page_no,
    )
    if not row:
        raise ApiError("E_NOT_FOUND", "页不存在", 404)
    blocks = q(
        "SELECT block_id,block_label,block_order,text,bbox_x1,bbox_y1,bbox_x2,bbox_y2,polygon_json,group_id "
        "FROM page_blocks WHERE task_id=:t AND page_no=:p ORDER BY block_order",
        t=task_id, p=page_no,
    )
    w, h = int(row["img_width"]), int(row["img_height"])

    # FR-RES-11：坐标异常判定（前后端都要能识别到，前端据此隐藏框层）
    bbox_ok = w > 0 and h > 0 and bool(blocks)
    if bbox_ok:
        for b in blocks:
            x1, y1, x2, y2 = (int(b[k]) for k in ("bbox_x1", "bbox_y1", "bbox_x2", "bbox_y2"))
            if x2 <= x1 or y2 <= y1 or x1 < 0 or y1 < 0 or x2 > w or y2 > h:
                bbox_ok = False
                break

    from ..db import get_setting

    return ok(
        {
            "page_no": page_no,
            "width": w,
            "height": h,
            "bbox_ok": bbox_ok,
            "bbox_enabled": get_setting("bbox_global_enabled", "1") == "1",
            "order_check_passed": row["order_check_passed"],
            "blocks": [
                {
                    "id": b["block_id"],
                    "label": b["block_label"],
                    "order": int(b["block_order"]),
                    "text": b["text"],
                    "bbox": [int(b["bbox_x1"]), int(b["bbox_y1"]), int(b["bbox_x2"]), int(b["bbox_y2"])],
                    "group_id": b["group_id"],
                }
                for b in blocks
            ],
        }
    )


@router.get("/{task_id}/exports")
def list_exports(request: Request, task_id: str):
    """已生成的文件列表（含是否可用），供下载面板渲染。"""
    user = get_current_user(request)
    task = _task_or_404(task_id, user)
    rows = q(
        "SELECT id,kind,lang,size,cleaned,expire_at FROM files WHERE task_id=:t AND cleaned=0 ORDER BY created_at",
        t=task_id,
    )
    return ok(
        {
            "can_download": can_download(task["status"]),
            "status": task["status"],
            "pending_mill": int(task["pending_mill"]),
            "items": [
                {
                    "file_id": r["id"], "kind": r["kind"], "lang": r["lang"],
                    "size": int(r["size"]), "expire_at": r["expire_at"],
                }
                for r in rows
            ],
        }
    )
