"""密码、会话、设备指纹。

会话用签名 Cookie（HttpOnly + SameSite=Lax），无服务端 session 表：
- M1a 单机单实例，签名 Cookie 足够；
- 登出 = 清 Cookie；
- 7 天有效期由签名时间戳校验（SESSION_DAYS）。
"""
from __future__ import annotations

import hmac
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from .config import settings
from .utils import sha256_text

SESSION_COOKIE = "txgj_session"
DEVICE_COOKIE = "txgj_device"

_ph = PasswordHasher(time_cost=2, memory_cost=65536, parallelism=2)


# ---------------------------------------------------------------- 密码
def hash_password(pwd: str) -> str:
    return _ph.hash(pwd)


def verify_password(pwd_hash: str, pwd: str) -> bool:
    try:
        return _ph.verify(pwd_hash, pwd)
    except (VerifyMismatchError, VerificationError, Exception):  # noqa: BLE001
        return False


def password_problem(pwd: str) -> str | None:
    """FR-ACC-01：密码 ≥8 位。返回错误文案或 None。"""
    if pwd is None:
        return "请输入密码"
    if len(pwd) < 8:
        return "密码至少 8 位"
    if len(pwd) > 128:
        return "密码过长"
    if len(set(pwd)) == 1:
        return "密码过于简单，请更换"
    return None


def username_problem(name: str) -> str | None:
    if not name:
        return "请输入用户名"
    name = name.strip()
    if len(name) < 2:
        return "用户名至少 2 个字符"
    if len(name) > 32:
        return "用户名最多 32 个字符"
    if any(c.isspace() for c in name):
        return "用户名不能包含空格"
    return None


# ---------------------------------------------------------------- 会话
def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.app_secret, salt="txgj-session")


def make_session(user_id: str) -> str:
    return _serializer().dumps({"uid": user_id})


def read_session(token: str | None) -> str | None:
    """返回 user_id；过期或签名错误返回 None。"""
    if not token:
        return None
    try:
        data: dict[str, Any] = _serializer().loads(token, max_age=settings.session_days * 86400)
    except (BadSignature, SignatureExpired):
        return None
    uid = data.get("uid")
    return uid if isinstance(uid, str) and uid else None


# ---------------------------------------------------------------- 设备指纹
def device_hash(device_token: str | None, user_agent: str | None) -> str:
    """设备指纹 = 前端持久 device token + UA。仅用于风控计数，不存原始 UA。"""
    raw = f"{device_token or ''}|{user_agent or ''}"
    return sha256_text(raw)[:32]


def constant_time_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a, b)
