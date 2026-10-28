"""导出统一入口：幂等、日志、体积校验、ZIP（FR-EXP 全组）。

三条硬约束：
1. `files(task_id, kind, lang)` 唯一 → 重复点击返回同一文件（cache_hit=1）。
2. 导出**不改任务状态、不扣费**（FR-EXP-08）。
3. 体积必须过 M-13 红线，超限记缺陷但不阻断（用户要能拿到文件）。
"""
from __future__ import annotations

import logging
import time
import zipfile
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from ...config import settings
from ...db import q, q1, tx
from ...enums import can_download
from ...utils import hours_later, new_id, now_iso
from . import docx as docx_mod
from . import fonts as fonts_mod
from . import markdown as md_mod
from .html_render import build_html
from .pdf import PdfEngineUnavailable, html_to_pdf

log = logging.getLogger("export")

# 渲染器版本。**改版必须 +1**：否则修好排版后，老用户再点下载拿到的还是
# 缓存里的旧坏文件（2026-10-27 校对版原图 404 就是这么被掩盖的）。
#   1 = 初版
#   2 = 校对版改 file:// 绝对路径 + 打印版面按纸面给死高度 + 每页 AI 标识
#   3 = 竖排里的 $...$ 公式降级成 λ、π、x₁ 这类普通字符（原先进竖排会被逐字拆开）
#   4 = 竖排纸张底色铺满整张纸（原先是"白框浮在色块上"，标题落在框外）
#   5 = 繁简只看用户选的 lang，不再被 task.mode 否决（modern 任务点「简体」拿到繁体）
#   6 = 白名单字体（思源宋体）到位
#   7 = 字体改加工版 TTF：去掉 glyph 名与康熙部首码位，PDF 文本层转 Type0
#       （6 那版虽嵌入了字体，但「黄」仍被写成 ⻩、「二」写成 ⼆，复制搜不到）
#   8 = 标题不再用伪粗体（伪粗体在 PDF 里必然退化成 Type3 图片字）
#   9 = 校对版原图打印时转 JPEG q88，单页体积落回 M-13 的 250KB 红线内
RENDER_VERSION = 9


class ExportError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


def _task(task_id: str) -> dict:
    row = q1("SELECT * FROM tasks WHERE id=:id", id=task_id)
    if not row:
        raise ExportError("E_NOT_FOUND", "任务不存在")
    return row


def _existing(task_id: str, kind: str, lang: str) -> dict | None:
    return q1(
        "SELECT * FROM files WHERE task_id=:t AND kind=:k AND lang=:l AND cleaned=0 AND expire_at > :now",
        t=task_id, k=kind, l=lang, now=now_iso(),
    )


def _is_fresh(row: dict) -> bool:
    """缓存是否由当前版本渲染器产出。老库没这列时按 0 处理 → 视为过期。"""
    try:
        return int(row.get("render_version") or 0) >= RENDER_VERSION
    except (TypeError, ValueError):
        return False


def _record(
    task_id: str,
    user_id: str,
    kind: str,
    success: bool,
    duration_ms: int,
    size: int | None = None,
    error_code: str | None = None,
    missing_glyphs: dict | None = None,
    cache_hit: bool = False,
) -> None:
    import json

    with tx() as conn:
        from sqlalchemy import text

        conn.execute(
            text(
                "INSERT INTO export_logs(task_id,user_id,kind,success,duration_ms,size_bytes,"
                "error_code,missing_glyphs,cache_hit,created_at)"
                " VALUES(:t,:u,:k,:s,:d,:b,:e,:m,:c,:ca)"
            ),
            {
                "t": task_id, "u": user_id, "k": kind, "s": 1 if success else 0,
                "d": duration_ms, "b": size, "e": error_code,
                "m": json.dumps(missing_glyphs, ensure_ascii=False) if missing_glyphs else None,
                "c": 1 if cache_hit else 0, "ca": now_iso(),
            },
        )


def _save_file(task_id: str, kind: str, lang: str, path: str, size: int,
               replace_id: str | None = None) -> dict:
    """写 files 行。`replace_id` 用于覆盖过期缓存 —— 不能直接 INSERT，
    因为 (task_id, kind, lang) 上有唯一索引，插入第二条会直接报约束冲突。

    并发下的第二次 INSERT 也要能活下来：用户双击下载、浏览器重试、
    或者 ZIP 打包时顺手把某个格式也生成了一遍，都会撞上唯一索引。
    撞上了就说明**别人已经生成好了**，直接复用那条记录即可，报 500 是错的。
    """
    fid = replace_id or new_id()
    if replace_id:
        with tx() as conn:
            conn.execute(
                text(
                    "UPDATE files SET path=:p, size=:s, expire_at=:e, cleaned=0, created_at=:c,"
                    " render_version=:v WHERE id=:old"
                ),
                {
                    "p": path, "s": size, "e": hours_later(settings.file_ttl_hours),
                    "c": now_iso(), "v": RENDER_VERSION, "old": replace_id,
                },
            )
    else:
        try:
            with tx() as conn:
                conn.execute(
                    text(
                        "INSERT INTO files(id,task_id,kind,lang,path,size,expire_at,cleaned,created_at,"
                        "render_version) VALUES(:id,:t,:k,:l,:p,:s,:e,0,:c,:v)"
                    ),
                    {
                        "id": fid, "t": task_id, "k": kind, "l": lang, "p": path,
                        "s": size, "e": hours_later(settings.file_ttl_hours), "c": now_iso(),
                        "v": RENDER_VERSION,
                    },
                )
        except IntegrityError:
            row = q1(
                "SELECT * FROM files WHERE task_id=:t AND kind=:k AND lang=:l",
                t=task_id, k=kind, l=lang,
            )
            if not row:
                raise
            log.info("导出并发：%s/%s/%s 已由另一请求写入，复用 %s",
                     task_id[:8], kind, lang, row["id"][:8])
            return {"file_id": row["id"], "kind": kind, "lang": lang,
                    "path": row["path"], "size": int(row["size"])}
    return {"file_id": fid, "kind": kind, "lang": lang, "path": path, "size": size}


# ---------------------------------------------------------------- 主入口
def export(task_id: str, kind: str, lang: str = "simp", user_id: str | None = None) -> dict:
    """生成或复用导出文件。返回 {file_id, kind, lang, size, cache_hit}。"""
    task = _task(task_id)
    uid = user_id or task["user_id"]

    if not can_download(task["status"]):
        if task["status"] == "locked":
            raise ExportError("E_LOCKED", f"还差 ¥{task['pending_mill'] / 1000:.2f}，充值后即可下载全部格式")
        raise ExportError("E_BAD_STATE", "任务尚未完成，暂不能下载")

    lang = "trad" if lang == "trad" else "simp"

    # ---- 幂等：已有文件直接返回（但只在渲染器版本相同的前提下）
    hit = _existing(task_id, kind, lang)
    stale_id: str | None = None
    if hit and _is_fresh(hit):
        _record(task_id, uid, kind, True, 0, int(hit["size"]), cache_hit=True)
        return {
            "file_id": hit["id"], "kind": kind, "lang": lang,
            "size": int(hit["size"]), "cache_hit": True,
        }
    if hit:
        # 旧版本渲染的缓存：重新生成并覆盖同一行
        stale_id = hit["id"]
        log.info("export cache stale: task=%s kind=%s ver=%s<%s",
                 task_id, kind, hit.get("render_version"), RENDER_VERSION)

    started = time.time()
    out_dir = settings.storage_dir / "tasks" / task_id / "exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    pages = q(
        "SELECT page_no FROM task_pages WHERE task_id=:t AND status IN ('done','empty')", t=task_id
    )
    page_count = max(1, len(pages))
    missing: dict = {}

    try:
        if kind == "markdown":
            text = md_mod.build_markdown(task_id, lang=lang)
            path = out_dir / f"task_{task_id[:8]}_{lang}.md"
            path.write_text(text, encoding="utf-8")
            size = path.stat().st_size

        elif kind == "horiz_docx":
            body_font, fb = fonts_mod.resolve_fallback(
                _all_text(task_id, lang), fonts_mod.FALLBACK_CHAIN[1]
            )
            missing = fb.missing
            path_str = out_dir / f"task_{task_id[:8]}_{lang}_horiz.docx"
            res = docx_mod.build_docx(task_id, str(path_str), lang=lang, font_name=body_font)
            path, size = Path(res["path"]), res["size"]

        elif kind in ("horiz_pdf", "vert_pdf", "vert_pdf_trad", "proof_pdf"):
            layout = "proof" if kind == "proof_pdf" else ("vert" if kind.startswith("vert") else "horiz")
            use_lang = "trad" if kind == "vert_pdf_trad" else lang
            body_font, fb = fonts_mod.resolve_fallback(_all_text(task_id, use_lang))
            missing = fb.missing
            html_str = build_html(
                task_id, lang=use_lang, layout=layout, with_boxes=(kind == "proof_pdf"), for_pdf=True
            )
            path_str = out_dir / f"task_{task_id[:8]}_{kind}_{use_lang}.pdf"
            if layout == "vert":
                res = html_to_pdf(html_str, str(path_str), width_px=850, height_px=1200)
            elif layout == "proof":
                res = html_to_pdf(html_str, str(path_str), landscape=True)
            else:
                res = html_to_pdf(html_str, str(path_str))
            path, size = Path(res["path"]), res["size"]
            if kind == "vert_pdf_trad":
                kind = "vert_pdf_trad"

        elif kind == "zip":
            return _build_zip(task_id, lang, uid)

        else:
            raise ExportError("E_BAD_KIND", f"不支持的导出类型：{kind}")

        # 体积红线（M-13）：只记录，不阻断
        if not fonts_mod.size_mill_is_ok(size, kind, page_count):
            log.warning("export size over M-13: task=%s kind=%s size=%s", task_id, kind, size)

        rec = _save_file(task_id, kind, lang, str(path), size, replace_id=stale_id)
        dur = int((time.time() - started) * 1000)
        _record(task_id, uid, kind, True, dur, size, missing_glyphs=missing)
        rec["cache_hit"] = False
        rec["duration_ms"] = dur
        rec["missing_glyphs"] = missing
        return rec

    except PdfEngineUnavailable as e:
        _record(task_id, uid, kind, False, int((time.time() - started) * 1000), error_code="E_NO_PDF_ENGINE")
        raise ExportError("E_NO_PDF_ENGINE", str(e)) from e
    except ExportError:
        raise
    except Exception as e:  # noqa: BLE001
        log.exception("export failed task=%s kind=%s", task_id, kind)
        _record(task_id, uid, kind, False, int((time.time() - started) * 1000), error_code="E_EXPORT_FAILED")
        raise ExportError("E_EXPORT_FAILED", f"生成失败，可重试：{e}") from e


def _all_text(task_id: str, lang: str) -> str:
    return md_mod.build_plain_text(task_id, lang=lang)


# ---------------------------------------------------------------- ZIP
def _build_zip(task_id: str, lang: str, uid: str) -> dict:
    """全部下载：先确保各格式存在，再打包。"""
    kinds = ["vert_pdf", "horiz_docx", "horiz_pdf", "proof_pdf", "markdown"]
    files: list[dict] = []
    errors: list[str] = []
    for k in kinds:
        try:
            files.append(export(task_id, k, lang, uid))
        except ExportError as e:
            errors.append(f"{k}: {e.code}")

    if not files:
        raise ExportError("E_EXPORT_FAILED", "没有可打包的文件：" + "; ".join(errors))

    existing = _existing(task_id, "zip", lang)
    zip_stale: str | None = None
    if existing and _is_fresh(existing):
        _record(task_id, uid, "zip", True, 0, int(existing["size"]), cache_hit=True)
        return {"file_id": existing["id"], "kind": "zip", "lang": lang,
                "size": int(existing["size"]), "cache_hit": True}
    if existing:
        # 旧 ZIP 里装的是旧渲染器产出的 PDF，必须重新打包
        zip_stale = existing["id"]

    out_dir = settings.storage_dir / "tasks" / task_id / "exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / f"task_{task_id[:8]}_all_{lang}.zip"
    started = time.time()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            p = Path(f["path"]) if f.get("path") else None
            if p is None:
                row = q1("SELECT path FROM files WHERE id=:id", id=f["file_id"])
                p = Path(row["path"]) if row else None
            if p and p.exists():
                zf.write(p, arcname=p.name)

    size = zip_path.stat().st_size
    rec = _save_file(task_id, "zip", lang, str(zip_path), size, replace_id=zip_stale)
    _record(task_id, uid, "zip", True, int((time.time() - started) * 1000), size)
    rec["cache_hit"] = False
    return rec


def file_for_download(file_id: str, user_id: str, is_admin: bool = False) -> dict:
    """下载前校验：归属、是否清理、是否过期。"""
    row = q1("SELECT * FROM files WHERE id=:id", id=file_id)
    if not row:
        raise ExportError("E_NOT_FOUND", "文件不存在")
    if not is_admin and row["task_id"]:
        task = _task(row["task_id"])
        if task["user_id"] != user_id:
            raise ExportError("E_FORBIDDEN", "无权下载该文件")
    if row["cleaned"]:
        raise ExportError("E_EXPIRED", "文件已过期（保留 3 天），可免费重跑一次")
    if row["expire_at"] < now_iso():
        raise ExportError("E_EXPIRED", "文件已过期（保留 3 天），可免费重跑一次")
    p = Path(row["path"])
    if not p.exists():
        raise ExportError("E_EXPIRED", "文件已过期（保留 3 天），可免费重跑一次")
    return row
