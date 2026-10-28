"""72 小时清理（FR-SYS-01 / FR-RES-06）。

双保险：
1. 存储侧：物理删除任务目录（原图、页图、导出文件）；
2. DB 侧：files.cleaned=1、tasks.status='expired'、page_blocks 保留（用于质量抽检）。
   —— blocks 是纯文本+坐标，不含用户隐私原文以外的信息，但为合规也不长期保留：
     90 天后由 purge_blocks 清除。

幂等：清理只按 expire_at + cleaned 标记推进，重跑无副作用。
"""
from __future__ import annotations

import logging
import shutil
from datetime import timedelta
from pathlib import Path

from sqlalchemy import text

from ..config import settings
from ..db import q, tx
from ..utils import iso, now, now_iso

log = logging.getLogger("cleanup")


def cleanup_expired(limit: int = 500) -> dict:
    """清理到期任务的文件。返回统计。"""
    ts = now_iso()
    # 必须排除还在跑的任务：expire_at 是「创建 + 72h」，正常任务跑不到这么久，
    # 但一个卡住的任务（worker 挂了没人管）到期时如果被这里删掉目录，
    # 正在读原图/页图的进程会一路 OCR 失败，还会把 status 直接改成 expired
    # 绕过状态机。先让 expire_stale_running 把僵尸任务收掉，再谈删文件。
    rows = q(
        "SELECT id, status, source_path FROM tasks WHERE expire_at <= :t "
        "AND status NOT IN ('expired','queued','processing','preprocessing','settling') "
        "ORDER BY expire_at LIMIT :l",
        t=ts, l=limit,
    )
    cleaned = 0
    failed = 0
    freed = 0

    for r in rows:
        task_dir = settings.storage_dir / "tasks" / r["id"]
        try:
            if task_dir.exists():
                freed += _dir_size(task_dir)
                shutil.rmtree(task_dir, ignore_errors=False)
        except Exception as e:  # noqa: BLE001
            log.warning("cleanup dir failed task=%s: %s", r["id"], e)
            failed += 1
            continue

        with tx() as conn:
            conn.execute(text("UPDATE files SET cleaned=1 WHERE task_id=:t"), {"t": r["id"]})
            conn.execute(
                text("UPDATE tasks SET status='expired', source_path=NULL WHERE id=:t"),
                {"t": r["id"]},
            )
        cleaned += 1

    # 清理孤立的导出文件记录
    with tx() as conn:
        conn.execute(
            text("UPDATE files SET cleaned=1 WHERE expire_at <= :t AND cleaned=0"), {"t": ts}
        )

    return {"cleaned": cleaned, "failed": failed, "freed_bytes": freed, "at": ts}


def purge_blocks(days: int = 90) -> int:
    """清除 90 天前的块结构与页记录（合规：不留长期原文）。"""
    cutoff = iso(now() - timedelta(days=days))
    with tx() as conn:
        res = conn.execute(
            text(
                "DELETE FROM page_blocks WHERE task_id IN "
                "(SELECT id FROM tasks WHERE created_at < :c AND status='expired')"
            ),
            {"c": cutoff},
        )
        n = int(res.rowcount or 0)
        conn.execute(
            text(
                "DELETE FROM task_pages WHERE task_id IN "
                "(SELECT id FROM tasks WHERE created_at < :c AND status='expired')"
            ),
            {"c": cutoff},
        )
    return n


def expire_stale_running(hours: int = 6) -> int:
    """卡死的任务标为 failed，避免永久占并发槽位。

    判据是**最后活动时间**（该任务所有页里最晚的 started_at/finished_at），
    不是 created_at —— 用创建时间算会把「跑得慢但一直在推进」的大任务误杀：
    实测 1.5 分钟/页，200 页要 5 小时，OCR 一慢就超过 6 小时固定阈值。

    阈值按页数放宽：max(hours, 页数 × 4 分钟 / 60)。200 页 → 13.3 小时，
    留了约 2.7 倍余量；真卡死的任务最后活动时间会停住，照样会被收掉。
    """
    rows = q(
        "SELECT t.id, t.pages_total, t.created_at,"
        "       MAX(COALESCE(p.finished_at, p.started_at)) AS last_act"
        "  FROM tasks t LEFT JOIN task_pages p ON p.task_id = t.id"
        " WHERE t.status IN ('queued','processing','preprocessing','settling')"
        " GROUP BY t.id"
    )
    n = 0
    for r in rows:
        last_act = r["last_act"] or r["created_at"]
        pages = int(r["pages_total"] or 0)
        budget_hours = max(hours, pages * 4 / 60)
        cutoff = iso(now() - timedelta(hours=budget_hours))
        if last_act >= cutoff:
            continue
        with tx() as conn:
            conn.execute(
                text("UPDATE tasks SET status='failed', error_code='E_STALE' WHERE id=:id"), {"id": r["id"]}
            )
        log.warning(
            "任务 %s 最后活动 %s，已超 %s 页对应的 %s 小时预算，标记 E_STALE",
            r["id"], last_act, pages, round(budget_hours, 1),
        )
        n += 1
    return n


def _dir_size(p: Path) -> int:
    total = 0
    for f in p.rglob("*"):
        if f.is_file():
            try:
                total += f.stat().st_size
            except OSError:
                pass
    return total


def cleanup_records(limit: int = 20) -> list[dict]:
    """清理记录查询（FR-ADM-08）。"""
    return q(
        "SELECT t.id, t.status, t.expire_at, COUNT(f.id) AS files, MAX(f.cleaned) AS all_cleaned "
        "FROM tasks t LEFT JOIN files f ON f.task_id=t.id "
        "WHERE t.status='expired' GROUP BY t.id ORDER BY t.expire_at DESC LIMIT :l",
        l=limit,
    )
