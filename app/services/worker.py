"""任务队列与识别执行（开发规格 §2.1 / §4.1）。

进程模型：单个 worker 进程轮询 DB 队列（不引 Redis/Celery）。
抢占用「条件 UPDATE」，天然互斥：只有一个 worker 能把 queued 改成 processing。

执行链：
  queued → processing
    └ 逐页：渲染 → recognize_page（含重试/降级/行序自检）→ 写 blocks+chars
    ├ 熔断：累计字数 > 预估 × 1.5 → partial
    ├ 全部失败 → failed（结算里退款）
    ├ 用户取消 → cancelled（已识别页仍结算）
    └ 正常完成 → settling → settle()
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

from sqlalchemy import text

from ..config import settings
from ..db import q, q1, setting_float, setting_int, tx
from ..enums import ALLOWED, assert_transition
from ..utils import hours_later, new_id, now_iso
from .billing import bill_mill, settle
from .ocr import recognize_page
from .ocr.base import OcrProviderError
from .ocr.base import INSERT_BLOCK_SQL
from .preprocess import (
    PreprocessError,
    detect_spread,
    image_size,
    load_image_page,
    parse_page_range,
    probe,
    render_pdf,
    split_spread,
)

log = logging.getLogger("worker")

# 进程入口收到 SIGTERM / SIGINT 时置 True。
# 任务不再「跑完才肯退出」（115 页的书要 90 分钟），而是在**页边界**收尾：
# 不结算、不动页状态，直接把任务放回队列，重启后断点续跑。
STOP_REQUESTED = False

# ---------------------------------------------------------------- 单实例锁
_worker_lock_fh = None


def acquire_worker_lock() -> tuple[bool, str]:
    """抢占「同一套库只能有一个 worker」的进程锁。返回 (是否拿到, 说明)。

    为什么必须有这把锁：队列靠「条件 UPDATE 抢占」实现互斥，那只保证**同一任务**
    不被两个 worker 同时处理，**不保证**只有一个 worker。多开 worker 时任务会被
    随机分给任意一个，如果两个 worker 的 OCR 配置不同（一个 OCR_MOCK=1、一个真通道），
    用户拿到的结果就会时真时假，而任务记录、状态、计费全都正常，事后极难发现。

    2026-10-27 真实踩过：一个遗留的 mock worker 与真实 worker 并存，
    第二个任务被 mock worker 抢走，用户拿到的正文是内置样例文章。

    flock 随进程退出（含 SIGKILL）由内核释放，不会留下假锁。
    """
    global _worker_lock_fh
    if _worker_lock_fh is not None:
        return True, "已持有单实例锁"

    lock_path = settings.storage_dir.parent / "worker.lock"
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(lock_path, "a+")
    except OSError as e:  # 锁文件不可用不该挡住启动
        log.warning("无法创建 worker 锁文件（%s），跳过单实例校验：%s", lock_path, e)
        return True, "锁不可用，已跳过单实例校验"

    try:
        if os.name == "nt":  # pragma: no cover - 部署目标为 Linux
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        holder = ""
        try:
            fh.seek(0)
            holder = (fh.read() or "").strip()
        except OSError:
            pass
        fh.close()
        return False, f"已有 worker 在跑（pid {holder or '未知'}），本进程退出以免抢任务"

    fh.seek(0)
    fh.truncate()
    fh.write(f"{os.getpid()} {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    fh.flush()
    _worker_lock_fh = fh
    return True, f"单实例锁已获取（{lock_path.name}, pid {os.getpid()}）"


# ---------------------------------------------------------------- 建页
def init_pages(task_id: str) -> int:
    """登记页记录（只读尺寸，不渲染）。创建任务时同步调用，页数立即可见。"""
    task = q1("SELECT * FROM tasks WHERE id=:id", id=task_id)
    if not task:
        raise ValueError("任务不存在")
    src = task["source_path"]
    if not src or not Path(src).exists():
        raise PreprocessError("E_NO_FILE", "原文件不存在")

    ftype, total, sizes = probe(src)
    selected = _selected_pages(task, total)

    rows = []
    spread_rec: dict | None = None
    if ftype == "pdf":
        for i, pn in enumerate(selected, start=1):
            w, h = sizes[pn - 1] if pn - 1 < len(sizes) else (0, 0)
            rows.append({"pn": i, "sp": pn, "w": w, "h": h, "half": 0})
    else:
        w, h = sizes[0]
        # 双页合拍：一张图拆成两页。
        # 不拆的后果（2026-10 实测）：整幅图缩到模型输入尺寸后每页有效分辨率
        # 只剩一半，小字糊掉，1000×916 的样张只认出 283 字（实际约 570 字），
        # 漏字四成，且两页的列序会互相穿插。
        is_spread, conf, seam = detect_spread(src)
        if is_spread:
            # 古籍右起：右半是本页、左半是次页，所以右半排前面
            rows.append({"pn": 1, "sp": 1, "w": w // 2, "h": h, "half": 1})
            rows.append({"pn": 2, "sp": 1, "w": w - w // 2, "h": h, "half": 2})
        else:
            rows.append({"pn": 1, "sp": 1, "w": w, "h": h, "half": 0})
        # 把拆分决策留痕：M-11 抽检与误判复盘全靠这张表。
        # 此前表建好了却从没写过一条 —— 双页判错时（test8 那种纸弯阴影糊成一条带）
        # 事后完全无法复盘当时是哪个判据、置信度多少。
        spread_rec = {
            "action": "split" if is_spread else "no_split",
            "conf": float(conf) if conf is not None else None,
            "bbox": json.dumps([seam, 0, seam, h]) if (is_spread and seam is not None) else None,
        }

    with tx() as conn:
        # 先删块再删页：page_blocks 有外键指向 task_pages(task_id, page_no)，
        # 顺序反了会 IntegrityError（重建页时会撞上，比如双页判定改动后重跑同一任务）。
        conn.execute(text("DELETE FROM page_blocks WHERE task_id=:t"), {"t": task_id})
        conn.execute(text("DELETE FROM task_pages WHERE task_id=:t"), {"t": task_id})
        for r in rows:
            conn.execute(
                text(
                    "INSERT INTO task_pages(task_id,page_no,source_page_no,is_split_half,status,chars,"
                    "retry_count,order_check_passed,img_width,img_height,image_path)"
                    " VALUES(:t,:pn,:sp,:half,'pending',0,0,NULL,:w,:h,NULL)"
                ),
                {"t": task_id, "pn": r["pn"], "sp": r["sp"], "half": r["half"], "w": r["w"], "h": r["h"]},
            )
        conn.execute(text("UPDATE tasks SET pages_total=:n WHERE id=:t"), {"n": len(rows), "t": task_id})
        conn.execute(
            text("DELETE FROM preprocess_records WHERE task_id=:t"), {"t": task_id}
        )
        if spread_rec:
            conn.execute(
                text(
                    "INSERT INTO preprocess_records(task_id,source_page_no,action,confidence,bbox,"
                    "reverted,created_at) VALUES(:t,1,:a,:c,:b,0,:n)"
                ),
                {
                    "t": task_id, "a": spread_rec["action"], "c": spread_rec["conf"],
                    "b": spread_rec["bbox"], "n": now_iso(),
                },
            )
    return len(rows)


def _selected_pages(task: dict, total: int) -> list[int]:
    """按 `tasks.page_range` 解析要识别的原页号。

    走 `parse_page_range` 而不是自己 split 逗号：建任务时存的是压缩表达式
    （`1-113`），旧数据是展开的逗号列表，两种都要认。解析失败一律退回全部页 ——
    建页这一步出错会让整个任务失败，而少识别几页只是体验问题。
    """
    raw = (task["page_range"] or "all").strip()
    try:
        return parse_page_range(raw, total)
    except PreprocessError:
        return list(range(1, total + 1))


def render_pages(task_id: str) -> int:
    """把还没渲染的页渲染成 PNG 并回填 image_path。返回渲染页数。"""
    task = q1("SELECT * FROM tasks WHERE id=:id", id=task_id)
    if not task:
        raise ValueError("任务不存在")
    src = task["source_path"]
    if not src or not Path(src).exists():
        raise PreprocessError("E_NO_FILE", "原文件不存在（可能已过 72 小时保留期）")

    out_dir = settings.storage_dir / "tasks" / task_id / "pages"
    out_dir.mkdir(parents=True, exist_ok=True)

    pages = q("SELECT * FROM task_pages WHERE task_id=:t ORDER BY page_no", t=task_id)
    todo = [p for p in pages if not p["image_path"]]
    if not todo:
        return 0

    ftype, _total, _sizes = probe(src)
    if ftype == "pdf":
        wanted = [int(p["source_page_no"]) for p in todo]
        rendered = {r.page_no: r for r in render_pdf(src, out_dir, pages=wanted)}
        with tx() as conn:
            for p in todo:
                r = rendered.get(int(p["source_page_no"]))
                if not r:
                    continue
                conn.execute(
                    text(
                        "UPDATE task_pages SET image_path=:ip, img_width=:w, img_height=:h"
                        " WHERE task_id=:t AND page_no=:pn"
                    ),
                    {"ip": r.path, "w": r.width, "h": r.height, "t": task_id, "pn": p["page_no"]},
                )
    else:
        # 图片：可能是双页合拍，按 is_split_half 切出对应的一半。
        # 一张原图可能对应两条页记录（右半 / 左半），这里一次性切好，
        # 避免对同一条缝重复检测。
        halves = {int(p["is_split_half"] or 0) for p in todo}
        if halves - {0}:
            is_spread, _c, seam = detect_spread(src)
            if not is_spread or seam is None:
                seam = image_size(src)[0] // 2
            rp, lp = split_spread(src, seam, out_dir)
            mapping = {1: (rp, 1), 2: (lp, 2), 0: (str(src), 0)}
            with tx() as conn:
                for p in todo:
                    half = int(p["is_split_half"] or 0)
                    path, _ = mapping.get(half, (str(src), 0))
                    # 中缝那一列像素两半都不含，宽高按实际文件重新量
                    try:
                        pw, ph = image_size(path)
                    except PreprocessError:
                        pw, ph = p["img_width"], p["img_height"]
                    conn.execute(
                        text(
                            "UPDATE task_pages SET image_path=:ip, img_width=:w, img_height=:h"
                            " WHERE task_id=:t AND page_no=:pn"
                        ),
                        {"ip": path, "w": pw, "h": ph, "t": task_id, "pn": p["page_no"]},
                    )
            _keep_source_copy(src, out_dir)
        else:
            dst = out_dir / "page_1.png"
            if Path(src).resolve() != dst.resolve():
                import shutil

                shutil.copyfile(src, dst)
            with tx() as conn:
                conn.execute(
                    text("UPDATE task_pages SET image_path=:ip WHERE task_id=:t AND page_no=1"),
                    {"ip": str(dst), "t": task_id},
                )
    return len(todo)


def _keep_source_copy(src: str, out_dir: Path) -> None:
    """拆分后把原图另存一份，供「取消拆分/人工核对」时回看。"""
    try:
        dst = out_dir / "source_original.png"
        if not dst.exists():
            import shutil

            shutil.copyfile(src, dst)
    except OSError:
        pass


def transition(task_id: str, dst: str, **fields) -> None:
    """按 ALLOWED 迁移任务状态，附带字段更新。"""
    task = q1("SELECT status FROM tasks WHERE id=:id", id=task_id)
    if not task:
        raise ValueError("任务不存在")
    assert_transition(task["status"], dst)
    sets = ["status=:dst"]
    params = {"dst": dst, "id": task_id}
    for k, v in fields.items():
        sets.append(f"{k}=:{k}")
        params[k] = v
    with tx() as conn:
        conn.execute(text(f"UPDATE tasks SET {', '.join(sets)} WHERE id=:id"), params)


# ---------------------------------------------------------------- 队列
def claim_next() -> dict | None:
    """抢占一个 queued 任务。条件 UPDATE 保证不重复消费。"""
    candidates = q("SELECT id FROM tasks WHERE status='queued' ORDER BY created_at LIMIT 5")
    for c in candidates:
        # 注意：重读必须放在事务**提交之后**。写在 `with tx()` 里面时，
        # q1 走的是另一个连接，SQLite 读不到未提交的 processing，返回的行
        # 状态还是 queued —— 下游据此判断会误以为没抢到。
        claimed = False
        with tx() as conn:
            res = conn.execute(
                text("UPDATE tasks SET status='processing' WHERE id=:id AND status='queued'"),
                {"id": c["id"]},
            )
            claimed = res.rowcount == 1
        if claimed:
            return q1("SELECT * FROM tasks WHERE id=:id", id=c["id"])
    return None


def active_task_count(user_id: str) -> int:
    row = q1(
        "SELECT COUNT(*) AS n FROM tasks WHERE user_id=:u AND status IN "
        "('created','preprocessing','queued','processing','settling')",
        u=user_id,
    )
    return int(row["n"]) if row else 0


def recover_stale_tasks() -> int:
    """把「僵尸 processing」放回队列。只能在拿到单实例锁之后调用。

    为什么必须有：`claim_next()` 只认 `queued`。worker 被杀（重启、OOM、断电、
    部署）时，被抢占的任务就永远停在 `processing` —— 没有任何代码会再碰它，
    用户看到的是「识别中」一路到 72 小时文件过期。

    放回队列是安全的：页级进度（`task_pages.status='done'`）都在，
    `run_task` 会跳过已完成的页，只补跑没做完的（见循环里的断点续跑分支）。
    所以这里不需要退款、也不需要让用户重新上传。
    """
    with tx() as conn:
        res = conn.execute(
            text("UPDATE tasks SET status='queued' WHERE status='processing'")
        )
        n = res.rowcount or 0
        # 停在半截的页回到 pending，否则断点续跑会把它当成「正在处理」又跳过
        conn.execute(
            text("UPDATE task_pages SET status='pending' WHERE status='processing'")
        )
    if n:
        log.warning("恢复 %s 个中断的任务到队列（worker 上次没有正常退出）", n)
    return n


# ---------------------------------------------------------------- 执行
def run_task(task: dict) -> dict:
    """执行一个已抢占（processing）的任务。返回结算结果。"""
    task_id = task["id"]
    mode = task["mode"]
    optimize = bool(task["optimize"])

    # 确保处于 processing：run_forever 抢占后已是 processing（此处 no-op）；
    # CLI 自测/测试直接调用时任务还是 queued，这里补一次合法迁移，否则 settle 会撞非法迁移。
    _cur = q1("SELECT status FROM tasks WHERE id=:id", id=task_id)
    if _cur and _cur["status"] != "processing":
        for step in ("preprocessing", "queued", "processing"):
            _now = q1("SELECT status FROM tasks WHERE id=:id", id=task_id)["status"]
            if _now == step:
                continue
            if step in ALLOWED.get(_now, set()):
                transition(task_id, step)

    pages = q("SELECT * FROM task_pages WHERE task_id=:t ORDER BY page_no", t=task_id)
    try:
        if not pages:
            init_pages(task_id)
        if any(not p["image_path"] for p in pages) or not pages:
            render_pages(task_id)
        pages = q("SELECT * FROM task_pages WHERE task_id=:t ORDER BY page_no", t=task_id)
    except PreprocessError as e:
        fail_task(task_id, e.code)
        return {"status": "failed", "error": e.code}

    # 熔断阈值：预估字数 × 1.5（FR-BIL-04）
    ratio = setting_float("circuit_break_ratio", settings.circuit_break_ratio)
    est = int(task["estimated_chars"]) or len(pages) * 400
    break_limit = int(est * ratio)

    total_chars = 0
    done = 0
    failed = 0
    interrupted = False
    statuses: list[str] = []
    order_scores: list[float] = []
    providers_used: set[str] = set()

    for p in pages:
        # 用户取消（FR：processing 允许取消，已识别页仍结算）
        cur = q1("SELECT status FROM tasks WHERE id=:id", id=task_id)
        if cur and cur["status"] == "cancelled":
            log.info("task %s cancelled by user", task_id)
            break

        # 运维停机：在页边界收手，任务放回队列，下次启动从断点继续
        if STOP_REQUESTED:
            interrupted = True
            break

        pn = int(p["page_no"])

        # 断点续跑：worker 重启后任务被放回队列（recover_stale_tasks），
        # 已识别好的页必须跳过 —— 否则整本重跑，既慢又重复花 OCR 的钱。
        # 字数与页数要补进汇总，不然结算会少算（用户少付、账实不符）。
        if p["status"] in ("done", "empty", "skipped"):
            statuses.append(p["status"])
            if p["status"] == "done":
                done += 1
                total_chars += int(p["chars"] or 0)
                if mode == "ancient" and p["order_check_passed"] is not None:
                    order_scores.append(1.0 if int(p["order_check_passed"]) else 0.0)
            continue

        img = p["image_path"]
        if not img or not Path(img).exists():
            _mark_page(task_id, pn, "failed", "图片不存在")
            failed += 1
            statuses.append("failed")
            continue

        with tx() as conn:
            conn.execute(
                text("UPDATE task_pages SET status='processing', started_at=:t WHERE task_id=:tk AND page_no=:p"),
                {"t": now_iso(), "tk": task_id, "p": pn},
            )

        try:
            res, used_provider, attempts = recognize_page(
                # keep_traditional=True：入库必须保留原文。
                # 之前这里传 False，ancient 模式在入库前就被转成简体——
                # 后果是「繁体原字」导出永远拿不到真繁体（数据已经被改掉了，还原不回去）。
                # 繁简只在输出层按用户点的「简体 / 繁体原字」转，见 export/*。
                img, mode=mode, optimize=optimize, keep_traditional=True
            )
            chars = res.chars
            if chars == 0 or not res.plain_text.strip():
                _mark_page(task_id, pn, "empty", None, chars=0)
                statuses.append("empty")
                continue

            score = getattr(res, "_order_score", 1.0)
            order_ok = res.order_ok
            providers_used.add(used_provider)
            with tx() as conn:
                conn.execute(text("DELETE FROM page_blocks WHERE task_id=:t AND page_no=:p"),
                             {"t": task_id, "p": pn})
                for b in res.blocks:
                    row = b.to_row()
                    conn.execute(text(INSERT_BLOCK_SQL), {"t": task_id, "p": pn, **row})
                conn.execute(
                    text(
                        "UPDATE task_pages SET status='done', chars=:c, finished_at=:f, "
                        "order_check_passed=:ok, error_msg=NULL WHERE task_id=:t AND page_no=:p"
                    ),
                    {"c": chars, "f": now_iso(), "ok": 1 if order_ok else (0 if order_ok is False else None),
                     "t": task_id, "p": pn},
                )
                # 逐页覆盖写入（同一任务页间可能因降级而换通道，结算前会改成汇总值）
                conn.execute(
                    text("UPDATE tasks SET ocr_provider=:p WHERE id=:id"),
                    {"p": used_provider, "id": task_id},
                )

            total_chars += chars
            done += 1
            statuses.append("done")
            if mode == "ancient":
                order_scores.append(score)
            log.info("task=%s page=%s chars=%s provider=%s attempts=%s",
                     task_id, pn, chars, used_provider, ",".join(attempts))

        except Exception as e:  # noqa: BLE001 - 单页失败不能拖垮整个任务
            log.warning("task=%s page=%s failed: %s", task_id, pn, e)
            code = getattr(e, "code", "E_PAGE_FAILED")
            _mark_page(task_id, pn, "failed", f"{code}: {getattr(e, 'message', str(e))}")
            failed += 1
            statuses.append("failed")

        # 熔断（FR-BIL-04 / EX-11）
        if total_chars > break_limit:
            log.warning("task=%s circuit break: %s chars > %s", task_id, total_chars, break_limit)
            with tx() as conn:
                conn.execute(
                    text("UPDATE tasks SET error_code='E_CIRCUIT_BREAK' WHERE id=:id"), {"id": task_id}
                )
            break

    # 停机中断：不结算、不把剩余页标 skipped，原样放回队列等下次启动续跑。
    # 结算会扣掉已识别页的钱，可那本书其实还没做完 —— 用户拿到的会是半本。
    if interrupted:
        with tx() as conn:
            conn.execute(
                text("UPDATE tasks SET status='queued' WHERE id=:i AND status='processing'"),
                {"i": task_id},
            )
        log.info("任务 %s 已放回队列（收到停止信号，%s 页已完成）", task_id, done)
        return {"status": "requeued"}

    # 未处理的页标记 skipped
    with tx() as conn:
        conn.execute(
            text("UPDATE task_pages SET status='skipped' WHERE task_id=:t AND status IN ('pending','processing')"),
            {"t": task_id},
        )

    # 平均行序得分写入（M-10 机器初筛）+ 实际通道汇总（故障追溯依据）
    if order_scores or providers_used:
        with tx() as conn:
            if order_scores:
                avg = round(sum(order_scores) / len(order_scores), 4)
                conn.execute(text("UPDATE tasks SET order_check_pass_rate=:s WHERE id=:id"),
                             {"s": avg, "id": task_id})
            if providers_used:
                conn.execute(
                    text("UPDATE tasks SET ocr_provider=:p WHERE id=:id"),
                    {"p": ",".join(sorted(providers_used)), "id": task_id},
                )

    # ---- 状态判定
    cur = q1("SELECT status FROM tasks WHERE id=:id", id=task_id)
    cur_status = cur["status"] if cur else "processing"

    if cur_status == "cancelled":
        # 已识别页仍结算（FR：取消不返还已用额度）
        with tx() as conn:
            conn.execute(
                text("UPDATE tasks SET status='processing' WHERE id=:id AND status='cancelled'"),
                {"id": task_id},
            )
        if done > 0:
            _to_settle(task_id)
        else:
            _force_finish(task_id, "cancelled")
        return {"status": "cancelled"}

    if done == 0:
        fail_task(task_id, "E_ALL_PAGES_FAILED")
        return {"status": "failed"}

    if failed > 0 or any(s == "skipped" for s in statuses):
        # 部分失败 / 熔断 / 额度不足跳过 → partial（已成功页 ≥1）
        _to_settle(task_id, prefer_partial=True)
        return {"status": "partial"}

    _to_settle(task_id)
    return {"status": "settled"}


def _to_settle(task_id: str, prefer_partial: bool = False) -> None:
    """进入 settling 并结算。partial 是结算后的状态（settle 只产出 settled/locked）。"""
    cur = q1("SELECT status FROM tasks WHERE id=:id", id=task_id)
    if cur and cur["status"] == "processing":
        try:
            transition(task_id, "settling")
        except Exception:  # noqa: BLE001
            pass
    result = settle(task_id)
    if prefer_partial and result["status"] in ("settled", "locked"):
        # 结算完成后再标 partial（partial 允许下载成功页）
        with tx() as conn:
            conn.execute(text("UPDATE tasks SET status='partial' WHERE id=:id AND status=:s"),
                         {"id": task_id, "s": result["status"]})
            if result["status"] == "locked":
                # 余额不足时保持 locked（锁下载比 partial 更严格，安全优先）
                conn.execute(text("UPDATE tasks SET status='locked' WHERE id=:id"), {"id": task_id})


def _force_finish(task_id: str, status: str) -> None:
    with tx() as conn:
        conn.execute(
            text("UPDATE tasks SET status=:s WHERE id=:id AND status NOT IN ('settled','expired')"),
            {"s": status, "id": task_id},
        )


def fail_task(task_id: str, code: str) -> None:
    with tx() as conn:
        conn.execute(
            text("UPDATE tasks SET status='failed', error_code=:c WHERE id=:id"), {"c": code, "id": task_id}
        )


def _mark_page(task_id: str, page_no: int, status: str, err: str | None, chars: int = 0) -> None:
    with tx() as conn:
        conn.execute(
            text(
                "UPDATE task_pages SET status=:s, chars=:c, error_msg=:e, finished_at=:f"
                " WHERE task_id=:t AND page_no=:p"
            ),
            {"s": status, "c": chars, "e": err, "f": now_iso(), "t": task_id, "p": page_no},
        )


def retry_page(task_id: str, page_no: int) -> dict:
    """重试单页（FR-OCR-05 / 结果页「单独重试」）。"""
    page = q1("SELECT * FROM task_pages WHERE task_id=:t AND page_no=:p", t=task_id, p=page_no)
    if not page:
        raise ValueError("页不存在")
    if int(page["retry_count"]) >= 3:
        raise ValueError("该页重试次数过多，请联系管理员")

    task = q1("SELECT * FROM tasks WHERE id=:id", id=task_id)
    img = page["image_path"]
    if not img or not Path(img).exists():
        raise ValueError("该页图片已过期（文件保留 3 天）")

    with tx() as conn:
        conn.execute(
            text("UPDATE task_pages SET retry_count=retry_count+1, status='processing' WHERE task_id=:t AND page_no=:p"),
            {"t": task_id, "p": page_no},
        )

    try:
        res, provider, _ = recognize_page(img, mode=task["mode"], optimize=bool(task["optimize"]))
        chars = res.chars
        with tx() as conn:
            conn.execute(text("DELETE FROM page_blocks WHERE task_id=:t AND page_no=:p"),
                         {"t": task_id, "p": page_no})
            for b in res.blocks:
                row = b.to_row()
                conn.execute(text(INSERT_BLOCK_SQL), {"t": task_id, "p": page_no, **row})
            conn.execute(
                text(
                    "UPDATE task_pages SET status=:s, chars=:c, finished_at=:f, error_msg=NULL"
                    " WHERE task_id=:t AND page_no=:p"
                ),
                {"s": "done" if chars else "empty", "c": chars, "f": now_iso(), "t": task_id, "p": page_no},
            )
        return {"page_no": page_no, "status": "done" if chars else "empty", "chars": chars, "provider": provider}
    except OcrProviderError as e:
        # 重试失败不是服务器错误：如实把页标成 failed 并把原因带回给前端，
        # 不能让请求 500（「服务器开小差了」）—— 用户会以为网站坏了。
        _mark_page(task_id, page_no, "failed", str(e))
        log.warning("重试失败 task=%s page=%s: %s", task_id, page_no, e)
        return {"page_no": page_no, "status": "failed", "error": str(e)}
    except Exception as e:  # noqa: BLE001
        _mark_page(task_id, page_no, "failed", str(e))
        log.exception("重试异常 task=%s page=%s", task_id, page_no)
        return {"page_no": page_no, "status": "failed", "error": str(e)}


# ---------------------------------------------------------------- 主循环
def run_forever(poll_seconds: float = 2.0) -> None:
    ok, msg = acquire_worker_lock()
    if not ok:
        log.error("worker 未启动：%s", msg)
        return
    log.info("worker started (poll=%ss) %s", poll_seconds, msg)
    while True:
        try:
            task = claim_next()
            if not task:
                time.sleep(poll_seconds)
                continue
            log.info("claimed task %s", task["id"])
            run_task(task)
        except KeyboardInterrupt:
            log.info("worker stopped")
            return
        except Exception:  # noqa: BLE001
            log.exception("worker loop error")
            time.sleep(poll_seconds)


def run_once() -> bool:
    """跑一个任务就返回（测试与 cron 用）。"""
    task = claim_next()
    if not task:
        return False
    run_task(task)
    return True
