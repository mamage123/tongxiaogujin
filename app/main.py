"""FastAPI 装配。

启动顺序（重要）：
1. init_db() 执行 schema.sql（幂等）
2. 若无管理员则按 ADMIN_INIT_PWD 创建（**禁止自助注册管理员**）
3. 可选：内嵌启动 worker 线程（RUN_WORKER_INPROC=1，开发/单机用）
"""
from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import text

from .config import BASE_DIR, settings
from .db import init_db, q1, tx
from .deps import ApiError, error_response, get_current_user_optional
from .routers import admin, auth, billing, export, meta, recharge, task, upload
from .security import hash_password
from .utils import new_id, now_iso

logging.basicConfig(
    level=logging.INFO if settings.app_env != "dev" else logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("app")

app = FastAPI(
    title="通晓古今 · 古籍文字识别与排版转换",
    version="1.0.0-m1a",
    docs_url="/api/docs" if settings.app_env != "prod" else None,
    redoc_url=None,
)

templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))


def _asset_version() -> str:
    """静态资源版本号 = 文件 mtime。

    为什么不能用固定值：改了 app.css/app.js 而不加版本号，浏览器会继续用
    缓存的旧文件，用户看到的是半新半旧的页面（2026-10-28 真实踩过：
    新增的页码状态样式不生效，被当成「功能没做」报了上来）。
    mtime 变了 URL 就变，浏览器自动拉新版；没改则命中缓存。
    """
    parts = []
    for name in ("app.css", "app.js"):
        p = static_dir / name
        try:
            parts.append(str(int(p.stat().st_mtime)))
        except OSError:
            parts.append("0")
    return ".".join(parts)


templates.env.globals["asset_v"] = _asset_version

# ---------------------------------------------------------------- 目录与静态
settings.storage_dir.mkdir(parents=True, exist_ok=True)
(settings.storage_dir / "uploads").mkdir(parents=True, exist_ok=True)
(settings.storage_dir / "tasks").mkdir(parents=True, exist_ok=True)
settings.font_dir.mkdir(parents=True, exist_ok=True)

static_dir = BASE_DIR / "app" / "static"
static_dir.mkdir(parents=True, exist_ok=True)
(static_dir / "pay").mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# ---------------------------------------------------------------- 异常
@app.exception_handler(ApiError)
async def _api_error_handler(request: Request, exc: ApiError):
    return error_response(exc)


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    log.exception("unhandled error on %s", request.url.path)
    return error_response(ApiError("E_INTERNAL", "服务器开小差了，请稍后重试", 500))


# ---------------------------------------------------------------- 路由
app.include_router(meta.router)
app.include_router(auth.router)
app.include_router(upload.router)
app.include_router(task.router)
app.include_router(export.router)
app.include_router(billing.router)
app.include_router(recharge.router)
app.include_router(admin.router)


# ---------------------------------------------------------------- 页面
def _ctx(request: Request, **extra) -> dict:
    user = get_current_user_optional(request)
    base = {
        "request": request,
        "user": user,
        "site_name": "通晓古今",
        "tagline": "古籍文字识别与排版转换",
        "free_pages": settings.free_pages_default,
        "max_upload_mb": settings.max_upload_mb,
        "ttl_hours": settings.file_ttl_hours,
    }
    base.update(extra)
    return base


@app.get("/", response_class=HTMLResponse)
def page_index(request: Request):
    return templates.TemplateResponse(request, "index.html", _ctx(request))


@app.get("/login", response_class=HTMLResponse)
def page_login(request: Request):
    return templates.TemplateResponse(request, "login.html", _ctx(request))


@app.get("/task/{task_id}", response_class=HTMLResponse)
def page_task(request: Request, task_id: str):
    """任务页必须登录——未登录时页面里除了任务号什么都没有，
    直接跳登录比让用户对着一堆空白猜要好。"""
    from fastapi.responses import RedirectResponse

    if not get_current_user_optional(request):
        return RedirectResponse(f"/login?next=/task/{task_id}", status_code=302)
    return templates.TemplateResponse(request, "task.html", _ctx(request, task_id=task_id))


@app.get("/recharge", response_class=HTMLResponse)
def page_recharge(request: Request):
    return templates.TemplateResponse(request, "recharge.html", _ctx(request))


@app.get("/account", response_class=HTMLResponse)
def page_account(request: Request):
    from fastapi.responses import RedirectResponse

    if not get_current_user_optional(request):
        return RedirectResponse("/login?next=/account", status_code=302)
    return templates.TemplateResponse(request, "account.html", _ctx(request))


@app.get("/terms", response_class=HTMLResponse)
def page_terms(request: Request):
    from .services.compliance import agreement_required

    return templates.TemplateResponse(request, "terms.html", _ctx(request, agreement=agreement_required()))


@app.get("/admin", response_class=HTMLResponse)
def page_admin(request: Request):
    """后台页面本身要拦住非管理员。

    数据接口（`/admin/api/*`）本来就有鉴权，但页面裸奔有两个坏处：
    ① 未登录用户能看到后台全貌（有哪些板块、字段怎么叫）；
    ② 普通用户误入后是一堆 401 的空壳，看着像坏了。
    未登录跳登录页（带 next 回来），非管理员直接踢回首页 —— 不告诉它这里有后台。
    """
    from fastapi.responses import RedirectResponse

    user = get_current_user_optional(request)
    if not user:
        return RedirectResponse("/login?next=/admin", status_code=302)
    if user.get("role") != "admin":
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(request, "admin.html", _ctx(request))


@app.get("/pay/qr/{method}")
def pay_qr(method: str):
    """收款码图片。二维码本身就是公开信息，不需登录；文件名固定，配合 ?v= 破缓存。"""
    from fastapi.responses import FileResponse

    if method not in ("wechat", "alipay"):
        raise ApiError("E_NOT_FOUND", "收款方式不存在", 404)
    pay_dir = settings.storage_dir / "pay"
    for ext in (".png", ".jpg", ".jpeg", ".webp"):
        p = pay_dir / f"{method}{ext}"
        if p.exists():
            return FileResponse(str(p), headers={"Cache-Control": "public, max-age=300"})
    raise ApiError("E_NOT_FOUND", "收款码尚未上传", 404)


# ---------------------------------------------------------------- 启动
def ensure_admin() -> None:
    """首次启动创建管理员。没有 ADMIN_INIT_PWD 就不创建（不设默认密码）。"""
    exists = q1("SELECT id FROM users WHERE role='admin' LIMIT 1")
    if exists:
        return
    if not settings.admin_init_pwd:
        log.warning("未配置 ADMIN_INIT_PWD，未创建管理员账号（后台不可登录）")
        return
    from .security import password_problem

    if (msg := password_problem(settings.admin_init_pwd)):
        log.error("ADMIN_INIT_PWD 不合规（%s），未创建管理员", msg)
        return
    with tx() as conn:
        conn.execute(
            text(
                "INSERT INTO users(id,username,pwd_hash,email,role,balance_mill,free_pages_total,"
                "free_pages_used,status,risk_flag,created_at)"
                " VALUES(:id,:u,:p,NULL,'admin',0,0,0,'active',0,:c)"
            ),
            {
                "id": new_id(),
                "u": settings.admin_init_user,
                "p": hash_password(settings.admin_init_pwd),
                "c": now_iso(),
            },
        )
    log.info("已创建管理员账号：%s（请立即登录后修改密码）", settings.admin_init_user)


def start_inline_worker() -> None:
    if os.environ.get("RUN_WORKER_INPROC", "0") != "1":
        return
    from .services.worker import run_forever

    t = threading.Thread(target=run_forever, kwargs={"poll_seconds": 2.0}, daemon=True, name="worker")
    t.start()
    log.info("内嵌 worker 已启动")


@app.on_event("startup")
def on_startup() -> None:
    init_db(seed=True)
    ensure_admin()
    start_inline_worker()
    log.info(
        "启动完成 env=%s db=%s ocr_primary=%s mock=%s",
        settings.app_env,
        settings.database_url,
        settings.ocr_primary,
        settings.ocr_mock,
    )
