"""FastAPI 依赖：当前用户、管理员、错误响应、限流。"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse

from .db import q1
from .security import DEVICE_COOKIE, SESSION_COOKIE, read_session


class ApiError(Exception):
    """统一业务错误。响应体：{ok:false, code, message, detail}。"""

    def __init__(self, code: str, message: str, http: int = 400, detail: dict | None = None):
        self.code = code
        self.message = message
        self.http = http
        self.detail = detail or {}
        super().__init__(message)


def err(code: str, message: str, http: int = 400, **detail: Any) -> ApiError:
    return ApiError(code, message, http, detail)


def error_response(exc: ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.http,
        content={"ok": False, "code": exc.code, "message": exc.message, "detail": exc.detail},
    )


def ok(data: Any = None) -> dict:
    return {"ok": True, "data": data}


# ---------------------------------------------------------------- 当前用户
def get_current_user_optional(request: Request) -> dict | None:
    uid = read_session(request.cookies.get(SESSION_COOKIE))
    if not uid:
        return None
    user = q1("SELECT * FROM users WHERE id=:id", id=uid)
    if not user or user["status"] != "active":
        return None
    return user


def get_current_user(request: Request) -> dict:
    user = get_current_user_optional(request)
    if not user:
        raise ApiError("E_UNAUTHORIZED", "请先登录", 401)
    return user


def get_admin(request: Request) -> dict:
    user = get_current_user_optional(request)
    if not user:
        raise ApiError("E_UNAUTHORIZED", "请先登录管理后台", 401)
    if user.get("role") != "admin":
        raise ApiError("E_FORBIDDEN", "无权限", 403)
    return user


CurrentUser = Annotated[dict, Depends(get_current_user)]
OptionalUser = Annotated[dict | None, Depends(get_current_user_optional)]
AdminUser = Annotated[dict, Depends(get_admin)]


# ---------------------------------------------------------------- 设备与 IP
def client_ip(request: Request) -> str:
    """取真实 IP。生产在 Nginx 后面，读 X-Forwarded-For 首段。"""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "0.0.0.0"


def device_token(request: Request) -> str | None:
    return request.cookies.get(DEVICE_COOKIE)


def set_auth_cookies(response, user_id: str) -> None:
    from .config import settings
    from .security import make_session

    response.set_cookie(
        SESSION_COOKIE,
        make_session(user_id),
        max_age=settings.session_days * 86400,
        httponly=True,
        samesite="lax",
        secure=settings.app_env == "prod",
        path="/",
    )


def clear_auth_cookies(response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")
