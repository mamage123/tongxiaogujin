"""导出与下载 API（FR-EXP 全组）。

关键点：
- 导出是 settled 的**只读派生**，不改变任务状态、不扣费；
- `(task_id, kind, lang)` 唯一 → 重复点击返回同一文件；
- 下载前校验归属与过期（72h）。
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse

from ..db import q1
from ..deps import ApiError, client_ip, get_current_user, ok
from ..enums import EXPORT_KIND, EXPORT_KIND_LABEL
from ..services import audit
from ..services.export import ExportError, export as do_export, file_for_download
from ..services.export.html_render import build_html

router = APIRouter(prefix="/api", tags=["export"])


@router.get("/export/kinds")
def kinds():
    return ok([{"kind": k, "label": EXPORT_KIND_LABEL[k]} for k in EXPORT_KIND])


@router.post("/tasks/{task_id}/exports")
def create_export(request: Request, task_id: str, payload: dict):
    user = get_current_user(request)
    kind = payload.get("kind") or ""
    lang = payload.get("lang") or "simp"
    if kind not in EXPORT_KIND:
        raise ApiError("E_BAD_KIND", "不支持的导出类型")

    task = q1("SELECT * FROM tasks WHERE id=:id", id=task_id)
    # 管理员可替用户重出：后台「导出健康度」发现某人某格式坏了，得能当场重生成。
    # 导出本身不扣费（只读派生），所以这里开豁免没有资金风险。
    is_admin = user.get("role") == "admin"
    if not task or (task["user_id"] != user["id"] and not is_admin):
        raise ApiError("E_NOT_FOUND", "任务不存在", 404)

    try:
        rec = do_export(task_id, kind, lang, user["id"])
    except ExportError as e:
        code_map = {
            "E_LOCKED": 402,
            "E_BAD_STATE": 409,
            "E_EXPIRED": 410,
            "E_FORBIDDEN": 403,
        }
        raise ApiError(e.code, e.message, code_map.get(e.code, 400)) from e

    audit.log_access("export", user["id"], client_ip(request), task_id, f"{kind}/{lang}")
    audit.event("export", user["id"], task_id, kind=kind, lang=lang, cache_hit=rec.get("cache_hit"))
    return ok(
        {
            "file_id": rec["file_id"],
            "kind": rec["kind"],
            "lang": rec["lang"],
            "size": rec["size"],
            "cache_hit": rec.get("cache_hit", False),
            "missing_glyphs": rec.get("missing_glyphs") or {},
            "download_url": f"/api/exports/{rec['file_id']}/download",
            "note": "本任务已付费，全部格式可任意下载，不会再次扣费",
        }
    )


@router.get("/exports/{file_id}/download")
def download(request: Request, file_id: str):
    user = get_current_user(request)
    try:
        row = file_for_download(file_id, user["id"], is_admin=(user.get("role") == "admin"))
    except ExportError as e:
        raise ApiError(e.code, e.message, 410 if e.code == "E_EXPIRED" else 403) from e

    p = Path(row["path"])
    names = {
        "vert_pdf": "竖排阅读版.pdf",
        "vert_pdf_trad": "竖排阅读版-繁体.pdf",
        "horiz_docx": "横排阅读版.docx",
        "horiz_pdf": "横排阅读版.pdf",
        "proof_pdf": "校对版.pdf",
        "markdown": "识别结果.md",
        "zip": "全部文件.zip",
    }
    task = q1("SELECT id, filename FROM tasks WHERE id=:t", t=row["task_id"])
    base = (task["filename"].rsplit(".", 1)[0] if task else "task")[:40]
    # names 里的值本身已经带扩展名，不要再拼 p.suffix —— 曾经多拼了一次，
    # 结果得到「竖排阅读版.pdf.pdf」。
    fname = base + "-" + names.get(row["kind"], row["kind"])

    audit.log_access("download", user["id"], client_ip(request), row["task_id"], row["kind"])
    return FileResponse(
        str(p),
        filename=fname,
        media_type=_media_type(p.suffix),
        headers={"Cache-Control": "private, no-store"},
    )


def _media_type(suffix: str) -> str:
    return {
        ".pdf": "application/pdf",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".zip": "application/zip",
        ".md": "text/markdown; charset=utf-8",
    }.get(suffix.lower(), "application/octet-stream")


@router.get("/tasks/{task_id}/preview", response_class=HTMLResponse)
def preview(request: Request, task_id: str, lang: str = "simp", layout: str = "horiz"):
    """预览 HTML —— 与 PDF 导出同源（同一模板 + 同一 CSS）。"""
    user = get_current_user(request)
    task = q1("SELECT * FROM tasks WHERE id=:id", id=task_id)
    if not task:
        raise ApiError("E_NOT_FOUND", "任务不存在", 404)
    if task["user_id"] != user["id"] and user.get("role") != "admin":
        raise ApiError("E_FORBIDDEN", "无权访问", 403)

    lay = layout if layout in ("horiz", "vert", "proof") else "horiz"
    ln = "trad" if lang == "trad" else "simp"
    html_str = build_html(task_id, lang=ln, layout=lay, with_boxes=(lay == "proof"))
    return HTMLResponse(html_str)
