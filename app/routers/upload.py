"""上传：分片、断点续传、文件头校验（FR-UPL 全组）。

为什么分片而不是一次 POST：
- 500MB 上限下，单次请求会打满内存与 Nginx 缓冲；
- 断网要能续传（FR-UPL-05）；
- 伪装扩展名的文件必须靠文件头判断（EX-02），合并后再校验一次。

前端约定：chunk 序号从 0 开始，大小固定 chunk_size（最后一片可小）。
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from fastapi import APIRouter, File, Request, UploadFile
from sqlalchemy import text

from ..config import settings
from ..db import q1, tx
from ..deps import ApiError, client_ip, get_current_user
from ..services.audit import log_access
from ..services.preprocess import PreprocessError, pdf_page_count, detect_type, probe
from ..utils import dumps, loads, new_id, now_iso, sha256_file

router = APIRouter(prefix="/api/upload", tags=["upload"])

CHUNK_SIZE = 4 * 1024 * 1024  # 4MB


@router.post("/init")
def init_upload(request: Request, payload: dict):
    user = get_current_user(request)
    filename = (payload.get("filename") or "").strip()
    total_size = int(payload.get("size") or 0)

    if not filename:
        raise ApiError("E_BAD_NAME", "缺少文件名")
    # 文件名会进 tasks.filename、下载时的 Content-Disposition、后台列表。
    # 不设上限的话，前端误传长串会撑坏布局，还会把库撑大。255 是多数文件系统的单名上限。
    if len(filename) > 255:
        raise ApiError("E_BAD_NAME", "文件名过长，请重命名后再上传（≤255 字符）")
    if total_size <= 0:
        raise ApiError("E_BAD_SIZE", "文件为空")
    if total_size > settings.max_upload_mb * 1024 * 1024:
        raise ApiError("E_TOO_LARGE", f"单文件不能超过 {settings.max_upload_mb}MB", 413)

    ext = Path(filename).suffix.lower()
    if ext not in {".jpg", ".jpeg", ".png", ".bmp", ".pdf"}:
        raise ApiError("E_UNSUPPORTED", "仅支持 JPG / PNG / BMP / PDF")

    uid = new_id()
    total_chunks = (total_size + CHUNK_SIZE - 1) // CHUNK_SIZE
    dir_path = settings.storage_dir / "uploads" / uid
    dir_path.mkdir(parents=True, exist_ok=True)

    with tx() as conn:
        conn.execute(
            text(
                "INSERT INTO upload_sessions(id,user_id,filename,total_size,chunk_size,total_chunks,"
                "received,dir_path,done,created_at) VALUES(:id,:u,:f,:s,:cs,:tc,'[]',:d,0,:c)"
            ),
            {
                "id": uid, "u": user["id"], "f": filename, "s": total_size,
                "cs": CHUNK_SIZE, "tc": total_chunks, "d": str(dir_path), "c": now_iso(),
            },
        )
    log_access("upload_init", user["id"], client_ip(request), uid)
    return {"ok": True, "data": {"upload_id": uid, "chunk_size": CHUNK_SIZE, "total_chunks": total_chunks}}


@router.post("/chunk")
async def upload_chunk(request: Request, upload_id: str = "", index: int = -1, file: UploadFile = File(...)):
    user = get_current_user(request)
    sess = q1("SELECT * FROM upload_sessions WHERE id=:id AND user_id=:u", id=upload_id, u=user["id"])
    if not sess:
        raise ApiError("E_NO_SESSION", "上传会话不存在或已过期")
    if sess["done"]:
        raise ApiError("E_DONE", "该文件已上传完成")
    if index < 0 or index >= int(sess["total_chunks"]):
        raise ApiError("E_BAD_INDEX", "分片序号越界")

    dir_path = Path(sess["dir_path"])
    dir_path.mkdir(parents=True, exist_ok=True)
    target = dir_path / f"chunk_{index:05d}"

    # 流式写盘，绝不整块读进内存
    size = 0
    with target.open("wb") as f:
        while chunk := await file.read(1024 * 1024):
            f.write(chunk)
            size += len(chunk)

    received = set(loads(sess["received"], []) or [])
    received.add(index)
    with tx() as conn:
        conn.execute(
            text("UPDATE upload_sessions SET received=:r WHERE id=:id"),
            {"r": dumps(sorted(received)), "id": upload_id},
        )
    return {
        "ok": True,
        "data": {"index": index, "size": size, "received": len(received), "total": int(sess["total_chunks"])},
    }


@router.post("/complete")
def complete_upload(request: Request, payload: dict):
    user = get_current_user(request)
    upload_id = payload.get("upload_id") or ""
    sess = q1("SELECT * FROM upload_sessions WHERE id=:id AND user_id=:u", id=upload_id, u=user["id"])
    if not sess:
        raise ApiError("E_NO_SESSION", "上传会话不存在或已过期")
    if sess["done"]:
        return {"ok": True, "data": {"upload_id": upload_id, "already": True, "file_hash": sess.get("file_hash")}}

    dir_path = Path(sess["dir_path"])
    received = loads(sess["received"], []) or []
    total = int(sess["total_chunks"])
    missing = [i for i in range(total) if i not in received]
    if missing:
        raise ApiError("E_INCOMPLETE", f"还有 {len(missing)} 个分片未上传，请重试", detail={"missing": missing[:20]})

    ext = Path(sess["filename"]).suffix.lower()
    merged = dir_path / f"merged{ext}"
    with merged.open("wb") as out:
        for i in range(total):
            part = dir_path / f"chunk_{i:05d}"
            if not part.exists():
                raise ApiError("E_INCOMPLETE", f"分片 {i} 丢失，请重新上传")
            with part.open("rb") as f:
                shutil.copyfileobj(f, out, 1024 * 1024)
            part.unlink(missing_ok=True)

    # 文件头校验（EX-02：伪装扩展名）
    try:
        real_type = detect_type(str(merged))
    except PreprocessError as e:
        shutil.rmtree(dir_path, ignore_errors=True)
        raise ApiError(e.code, "文件已损坏或格式不支持") from e

    if ext == ".pdf" and real_type != "pdf":
        shutil.rmtree(dir_path, ignore_errors=True)
        raise ApiError("E_UNSUPPORTED", "文件内容不是有效的 PDF")
    if ext != ".pdf" and real_type != "image":
        shutil.rmtree(dir_path, ignore_errors=True)
        raise ApiError("E_UNSUPPORTED", "文件内容不是有效的图片")

    # 加密 PDF 拦截（EX-01）
    try:
        ftype, pages, sizes = probe(str(merged))
    except PreprocessError as e:
        shutil.rmtree(dir_path, ignore_errors=True)
        raise ApiError(e.code, e.message) from e

    file_hash = sha256_file(str(merged))
    with tx() as conn:
        conn.execute(
            text("UPDATE upload_sessions SET done=1 WHERE id=:id"), {"id": upload_id}
        )

    log_access("upload_complete", user["id"], client_ip(request), upload_id, f"pages={pages}")
    warn = None
    if sizes and min(sizes[0]) < 1000:
        warn = f"分辨率偏低（{sizes[0][0]}×{sizes[0][1]}），可能影响识别精度"
    return {
        "ok": True,
        "data": {
            "upload_id": upload_id,
            "path": str(merged),
            "file_type": ftype,
            "pages": pages,
            "file_hash": file_hash,
            "size": merged.stat().st_size,
            "warning": warn,
        },
    }


@router.get("/session/{upload_id}")
def session_status(request: Request, upload_id: str):
    """断点续传：前端据此跳过已上传分片。"""
    user = get_current_user(request)
    sess = q1("SELECT * FROM upload_sessions WHERE id=:id AND user_id=:u", id=upload_id, u=user["id"])
    if not sess:
        raise ApiError("E_NO_SESSION", "上传会话不存在")
    return {
        "ok": True,
        "data": {
            "received": loads(sess["received"], []) or [],
            "total_chunks": int(sess["total_chunks"]),
            "done": bool(sess["done"]),
        },
    }
