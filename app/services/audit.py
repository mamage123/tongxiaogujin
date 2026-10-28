"""审计与通知：访问日志、后台操作日志、站内通知、埋点。

铁律：埋点与日志**禁止写入用户原文内容**（不埋 PII）。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import text

from ..db import q, tx
from ..utils import dumps, now_iso


def log_access(
    action: str,
    user_id: str | None = None,
    ip: str | None = None,
    target_id: str | None = None,
    detail: str | None = None,
) -> None:
    """FR-SYS-02：登录/上传/识别/下载/充值/调整全留痕，留存 ≥6 个月。"""
    try:
        with tx() as conn:
            conn.execute(
                text(
                    "INSERT INTO access_logs(user_id,ip,action,target_id,detail,created_at)"
                    " VALUES(:u,:i,:a,:t,:d,:c)"
                ),
                {"u": user_id, "i": ip, "a": action, "t": target_id, "d": detail, "c": now_iso()},
            )
    except Exception:  # noqa: BLE001 - 日志失败不能影响主流程
        pass


def log_admin(
    admin_id: str,
    action: str,
    target_type: str,
    target_id: str | None = None,
    before: Any = None,
    after: Any = None,
) -> None:
    """FR-ADM-06：资金操作与开关变更必须留痕（操作人、时间、对象、前后值）。"""
    with tx() as conn:
        conn.execute(
            text(
                "INSERT INTO admin_logs(admin_id,action,target_type,target_id,before_value,"
                "after_value,created_at) VALUES(:a,:ac,:tt,:ti,:b,:af,:c)"
            ),
            {
                "a": admin_id,
                "ac": action,
                "tt": target_type,
                "ti": target_id,
                "b": dumps(before) if before is not None else None,
                "af": dumps(after) if after is not None else None,
                "c": now_iso(),
            },
        )


def notify(user_id: str, kind: str, title: str, body: str | None = None, link: str | None = None) -> None:
    with tx() as conn:
        conn.execute(
            text(
                "INSERT INTO notifications(user_id,kind,title,body,link,created_at)"
                " VALUES(:u,:k,:t,:b,:l,:c)"
            ),
            {"u": user_id, "k": kind, "t": title, "b": body, "l": link, "c": now_iso()},
        )


def list_notifications(user_id: str, limit: int = 20) -> list[dict]:
    return q(
        "SELECT * FROM notifications WHERE user_id=:u ORDER BY id DESC LIMIT :l", u=user_id, l=limit
    )


def mark_read(user_id: str, notif_id: int | None = None) -> int:
    with tx() as conn:
        if notif_id:
            res = conn.execute(
                text("UPDATE notifications SET read_at=:t WHERE id=:i AND user_id=:u AND read_at IS NULL"),
                {"t": now_iso(), "i": notif_id, "u": user_id},
            )
        else:
            res = conn.execute(
                text("UPDATE notifications SET read_at=:t WHERE user_id=:u AND read_at IS NULL"),
                {"t": now_iso(), "u": user_id},
            )
        return int(res.rowcount or 0)


def event(name: str, user_id: str | None = None, task_id: str | None = None, **props) -> None:
    """埋点（T12）。props 里禁止放原文、文件名、邮箱等 PII。"""
    try:
        with tx() as conn:
            conn.execute(
                text(
                    "INSERT INTO events(name,user_id,task_id,props,created_at)"
                    " VALUES(:n,:u,:t,:p,:c)"
                ),
                {
                    "n": name,
                    "u": user_id,
                    "t": task_id,
                    "p": dumps(props) if props else None,
                    "c": now_iso(),
                },
            )
    except Exception:  # noqa: BLE001
        pass
