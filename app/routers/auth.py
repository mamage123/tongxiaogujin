"""账号：注册 / 登录 / 登出 / 找回密码 / 个人中心（FR-ACC 全组）。

三个真实约束（D25 决定不采集手机号，所以必须用别的手段顶上）：
1. 防刷：设备 24h ≤2、IP 24h ≤5，超限**仍可注册但不发免费额度**（不误伤共用网络）；
2. 找回：有邮箱自助，无邮箱走管理员人工核验（EX-35），登录页必须明示，不许死路；
3. 锁定：用户名 + IP 双维度，5 次错锁 15 分钟（只错用户名不锁 IP 会被爆破）。
"""
from __future__ import annotations

import re
from datetime import timedelta

from fastapi import APIRouter, Request, Response
from sqlalchemy import text

from ..config import settings
from ..db import q1, setting_int, tx
from ..deps import ApiError, client_ip, device_token, get_current_user_optional, ok, set_auth_cookies, clear_auth_cookies
from ..security import (
    device_hash,
    hash_password,
    password_problem,
    read_session,
    username_problem,
    verify_password,
)
from ..services.audit import log_access
from ..utils import iso, minutes_later, new_id, new_token, now, now_iso, sha256_text

router = APIRouter(prefix="/api/auth", tags=["auth"])

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")

LOCK_THRESHOLD = 5
LOCK_MINUTES = 15

# 本机回环地址（含 IPv6 与 0.0.0.0）：防刷统计一律不认它们，见 _should_withhold_free。
# 故意不含空字符串——取不到 IP 时不能当回环放行，否则防刷整体失效。
_LOOPBACK_IPS = frozenset({"127.0.0.1", "::1", "0.0.0.0", "localhost"})


def _check_email(email: str | None) -> str | None:
    if not email:
        return None
    email = email.strip()
    if not EMAIL_RE.match(email):
        raise ApiError("E_BAD_EMAIL", "邮箱格式不正确")
    if len(email) > 120:
        raise ApiError("E_BAD_EMAIL", "邮箱过长")
    return email


# ---------------------------------------------------------------- 锁定
def _scope_status(scope: str) -> dict | None:
    return q1("SELECT * FROM login_attempts WHERE scope=:s", s=scope)


def _assert_not_locked(username: str, ip: str) -> None:
    ts = now_iso()
    for scope in (f"user:{username.lower()}", f"ip:{ip}"):
        row = _scope_status(scope)
        if row and row["locked_until"] and row["locked_until"] > ts:
            raise ApiError("E_LOCKED", f"密码错误次数过多，请 {LOCK_MINUTES} 分钟后重试", 429)


def _bump_fail(username: str, ip: str) -> None:
    with tx() as conn:
        for scope in (f"user:{username.lower()}", f"ip:{ip}"):
            row = conn.execute(
                text("SELECT fail_count FROM login_attempts WHERE scope=:s"), {"s": scope}
            ).mappings().first()
            cnt = (int(row["fail_count"]) if row else 0) + 1
            locked = minutes_later(LOCK_MINUTES) if cnt >= LOCK_THRESHOLD else None
            conn.execute(
                text(
                    "INSERT INTO login_attempts(scope,fail_count,locked_until,updated_at)"
                    " VALUES(:s,:c,:l,:t)"
                    " ON CONFLICT(scope) DO UPDATE SET fail_count=:c, locked_until=:l, updated_at=:t"
                ),
                {"s": scope, "c": cnt, "l": locked, "t": now_iso()},
            )


def _clear_fail(username: str, ip: str) -> None:
    with tx() as conn:
        for scope in (f"user:{username.lower()}", f"ip:{ip}"):
            conn.execute(text("DELETE FROM login_attempts WHERE scope=:s"), {"s": scope})


# ---------------------------------------------------------------- 防刷
def _register_marks(device: str | None, ip: str, user_id: str) -> None:
    with tx() as conn:
        conn.execute(
            text("INSERT INTO register_marks(device_hash,ip,user_id,created_at) VALUES(:d,:i,:u,:c)"),
            {"d": device, "i": ip, "u": user_id, "c": now_iso()},
        )


def _should_withhold_free(device: str | None, ip: str) -> tuple[bool, str]:
    """返回 (是否扣发免费额度, 原因)。"""
    # 回环地址豁免：本机请求只可能来自开发/自测，不是真实用户。
    # 不豁免的后果是实测过的真实事故——本机跑了 30 次注册用例后，IP 配额(5/24h)永久打满，
    # 之后**真人在本机注册也被判超限、免费额度为 0**，而前端压根没解释（用户只看到"剩 0 页"）。
    if settings.anti_abuse_skip_loopback and ip in _LOOPBACK_IPS:
        return False, ""

    since = iso(now() - timedelta(hours=24))
    dev_limit = settings.device_account_limit
    ip_limit = settings.ip_account_limit
    if device:
        row = q1(
            "SELECT COUNT(*) AS n FROM register_marks WHERE device_hash=:d AND created_at>=:s",
            d=device, s=since,
        )
        if row and int(row["n"]) >= dev_limit:
            return True, "device"
    if ip:
        row = q1(
            "SELECT COUNT(*) AS n FROM register_marks WHERE ip=:i AND created_at>=:s", i=ip, s=since
        )
        if row and int(row["n"]) >= ip_limit:
            return True, "ip"
    return False, ""


# ---------------------------------------------------------------- 注册
@router.post("/register")
def register(request: Request, payload: dict, response: Response):
    username = (payload.get("username") or "").strip()
    password = payload.get("password") or ""
    email = payload.get("email")
    agree = bool(payload.get("agree"))

    if not agree:
        raise ApiError("E_NEED_AGREE", "请先阅读并同意《用户协议》与《版权声明》")

    if (msg := username_problem(username)):
        raise ApiError("E_BAD_USERNAME", msg)
    if (msg := password_problem(password)):
        raise ApiError("E_WEAK_PWD", msg)
    email = _check_email(email)

    if q1("SELECT id FROM users WHERE username=:u", u=username):
        raise ApiError("E_USERNAME_TAKEN", "该用户名已被使用")
    if email and q1("SELECT id FROM users WHERE email=:e", e=email):
        raise ApiError("E_EMAIL_TAKEN", "该邮箱已被使用")

    ip = client_ip(request)
    dev = device_hash(device_token(request), request.headers.get("user-agent"))
    withhold, reason = _should_withhold_free(dev, ip)

    uid = new_id()
    free_total = 0 if withhold else setting_int("free_pages_default", settings.free_pages_default)

    with tx() as conn:
        conn.execute(
            text(
                "INSERT INTO users(id,username,pwd_hash,email,role,balance_mill,free_pages_total,"
                "free_pages_used,status,risk_flag,device_hash,reg_ip,created_at)"
                " VALUES(:id,:u,:p,:e,'user',0,:ft,0,'active',:rf,:d,:ip,:c)"
            ),
            {
                "id": uid,
                "u": username,
                "p": hash_password(password),
                "e": email,
                "ft": free_total,
                "rf": 1 if withhold else 0,
                "d": dev,
                "ip": ip,
                "c": now_iso(),
            },
        )
    _register_marks(dev, ip, uid)
    log_access("register", uid, ip, detail=f"free={free_total},withhold={reason or '-'}")

    set_auth_cookies(response, uid)
    user = q1("SELECT * FROM users WHERE id=:id", id=uid)
    return ok(_public_user(user, extra={"free_withheld": withhold, "withhold_reason": reason}))


# ---------------------------------------------------------------- 登录
@router.post("/login")
def login(request: Request, payload: dict, response: Response):
    username = (payload.get("username") or "").strip()
    password = payload.get("password") or ""
    ip = client_ip(request)

    _assert_not_locked(username, ip)

    user = q1("SELECT * FROM users WHERE username=:u", u=username)
    if not user or not verify_password(user["pwd_hash"], password):
        _bump_fail(username, ip)
        log_access("login_fail", user["id"] if user else None, ip)
        raise ApiError("E_BAD_CRED", "用户名或密码不正确", 401)

    if user["status"] != "active":
        raise ApiError("E_DISABLED", "账号已被停用，请联系管理员", 403)

    _clear_fail(username, ip)
    set_auth_cookies(response, user["id"])
    log_access("login", user["id"], ip)
    return ok(_public_user(user))


@router.post("/logout")
def logout(response: Response):
    clear_auth_cookies(response)
    return ok({"logged_out": True})


# ---------------------------------------------------------------- 当前用户
def _public_user(user: dict, extra: dict | None = None) -> dict:
    from ..services.billing import total_consumed

    data = {
        "id": user["id"],
        "username": user["username"],
        "email": user["email"],
        "role": user.get("role", "user"),
        "balance_mill": int(user["balance_mill"]),
        "balance_text": f"¥{int(user['balance_mill']) / 1000:.2f}",
        "free_pages_total": int(user["free_pages_total"]),
        "free_pages_used": int(user["free_pages_used"]),
        "free_pages_left": max(0, int(user["free_pages_total"]) - int(user["free_pages_used"])),
        "risk_flag": int(user.get("risk_flag") or 0),
        "created_at": user["created_at"],
        "consumed_mill": total_consumed(user["id"]),
    }
    data["consumed_text"] = f"¥{data['consumed_mill'] / 1000:.2f}"
    if extra:
        data.update(extra)
    return data


@router.get("/me")
def me(request: Request):
    user = get_current_user_optional(request)
    if not user:
        return ok(None)
    return ok(_public_user(user))


@router.post("/me/email")
def set_email(request: Request, payload: dict):
    user = get_current_user_optional(request)
    if not user:
        raise ApiError("E_UNAUTHORIZED", "请先登录", 401)
    email = _check_email(payload.get("email"))
    if not email:
        raise ApiError("E_BAD_EMAIL", "请输入邮箱")
    if q1("SELECT id FROM users WHERE email=:e AND id<>:u", e=email, u=user["id"]):
        raise ApiError("E_EMAIL_TAKEN", "该邮箱已被其他账号使用")
    with tx() as conn:
        conn.execute(text("UPDATE users SET email=:e WHERE id=:u"), {"e": email, "u": user["id"]})
    return ok({"email": email})


# ---------------------------------------------------------------- 找回密码
@router.post("/password/reset-request")
def reset_request(request: Request, payload: dict):
    """有邮箱：生成 30 分钟令牌（M1a 不接短信/邮件服务，令牌直接返回给管理员渠道）。

    说明：M1a 未接邮件服务，为了不留「死路」：
    - 有邮箱 → 生成令牌并**在站内展示**（用户自己用），同时记日志供管理员核实；
    - 无邮箱 → 返回人工核验指引（EX-35），不出现死路。
    """
    username = (payload.get("username") or "").strip()
    user = q1("SELECT * FROM users WHERE username=:u", u=username)
    ip = client_ip(request)

    if not user:
        # 不暴露用户是否存在
        return ok({"mode": "manual", "message": "如账号存在，请按页面指引找回"})

    if not user["email"]:
        log_access("reset_manual", user["id"], ip)
        return ok(
            {
                "mode": "manual",
                "message": "该账号未填邮箱，请联系管理员人工重置。请提供：注册时间、最近一个任务的页数与文件名、或任一充值单号，我们核实后处理。",
            }
        )

    token = new_token(24)
    with tx() as conn:
        conn.execute(
            text(
                "INSERT INTO password_resets(token_hash,user_id,expire_at,used,created_at)"
                " VALUES(:h,:u,:e,0,:c)"
            ),
            {"h": sha256_text(token), "u": user["id"], "e": minutes_later(30), "c": now_iso()},
        )
    log_access("reset_request", user["id"], ip)
    masked = _mask_email(user["email"])
    return ok(
        {
            "mode": "token",
            "masked_email": masked,
            "token": token,
            "expires_in_minutes": 30,
            "message": f"重置令牌已生成（30 分钟内有效）。因系统暂未接入邮件服务，请直接使用下方令牌。",
        }
    )


@router.post("/password/reset")
def reset_password(payload: dict, response: Response):
    token = payload.get("token") or ""
    new_pwd = payload.get("password") or ""
    if (msg := password_problem(new_pwd)):
        raise ApiError("E_WEAK_PWD", msg)

    row = q1("SELECT * FROM password_resets WHERE token_hash=:h", h=sha256_text(token))
    if not row or row["used"] or row["expire_at"] < now_iso():
        raise ApiError("E_BAD_TOKEN", "令牌无效或已过期，请重新申请")

    with tx() as conn:
        conn.execute(text("UPDATE password_resets SET used=1 WHERE token_hash=:h"), {"h": sha256_text(token)})
        conn.execute(
            text("UPDATE users SET pwd_hash=:p WHERE id=:u"), {"p": hash_password(new_pwd), "u": row["user_id"]}
        )
    set_auth_cookies(response, row["user_id"])
    return ok({"reset": True})


def _mask_email(email: str) -> str:
    if not email or "@" not in email:
        return ""
    name, _, domain = email.partition("@")
    keep = name[:2] if len(name) > 2 else name[:1]
    return f"{keep}***@{domain}"
