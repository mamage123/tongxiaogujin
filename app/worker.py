"""队列 worker 独立进程入口：`python -m app.worker`。"""
from __future__ import annotations

import logging
import os
import signal
import sys
import time

from .config import settings
from .db import init_db
from .main import ensure_admin, templates  # noqa: F401 - 复用初始化逻辑
from .services import worker as worker_svc
from .services.cleanup import expire_stale_running
from .services.worker import acquire_worker_lock, claim_next, recover_stale_tasks, run_task

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
)
log = logging.getLogger("worker-main")

_stop = False


def _handle(signum, _frame):  # noqa: ANN001
    global _stop
    log.info("收到信号 %s，当前页识别完就收手（任务放回队列，下次续跑）", signum)
    _stop = True
    # 让正在跑的任务在页边界退出：115 页的书要 90 分钟，等它跑完再停是不现实的
    worker_svc.STOP_REQUESTED = True


def main() -> int:
    init_db(seed=True)
    ensure_admin()

    ok, msg = acquire_worker_lock()
    if not ok:
        log.error(msg)
        log.error("一个库只允许一个 worker。确认已在跑的那个是不是你以为的那个：")
        log.error("  cat %s        # 里面是持有锁的 pid", settings.storage_dir.parent / "worker.lock")
        log.error("  pgrep -fl 'app.worker|uvicorn app.main'")
        return 2
    log.info(msg)

    # 拿到锁之后才能做恢复：多实例同时跑时，恢复会把正在处理的任务抢回来。
    recover_stale_tasks()

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)

    poll = float(os.environ.get("WORKER_POLL_SECONDS", "2"))
    # 维护轮间隔：只做「僵尸任务收容」这一件不碰文件的事。
    # 之所以放在 worker 里而不是 cron：单实例已经靠 flock 保证了唯一执行者，
    # 再加一个调度器只会多一个可能起不来的东西。
    maint_every = float(os.environ.get("WORKER_MAINT_SECONDS", "600"))
    next_maint = time.monotonic() + maint_every
    log.info("worker 启动 poll=%ss ocr_primary=%s mock=%s", poll, settings.ocr_primary, settings.ocr_mock)

    idle_logged = False
    while not _stop:
        try:
            task = claim_next()
            if not task:
                if not idle_logged:
                    log.info("队列空闲，等待任务…")
                    idle_logged = True
                # 排队空闲时顺手收尸：卡在 processing 的任务会一直占着槽位，
                # 而这个函数此前定义了却没人调用 —— 兜底等于没做。
                if time.monotonic() >= next_maint:
                    next_maint = time.monotonic() + maint_every
                    try:
                        killed = expire_stale_running()
                        if killed:
                            log.warning("收容 %s 个长时间无进展的任务", killed)
                    except Exception:  # noqa: BLE001
                        log.exception("僵尸任务收容失败（不影响主循环）")
                time.sleep(poll)
                continue
            idle_logged = False
            log.info("领取任务 %s", task["id"])
            res = run_task(task)
            log.info("任务 %s 结束：%s", task["id"], res)
        except Exception:  # noqa: BLE001
            log.exception("worker 循环异常")
            time.sleep(poll)

    log.info("worker 已停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())
