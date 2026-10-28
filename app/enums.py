"""全部枚举与状态迁移表（开发规格 §4）。

铁律：状态只能按 ALLOWED 迁移，禁止回跳。
"""
from __future__ import annotations

# ---------------------------------------------------------------- 任务状态
TASK_STATUS = [
    "created",
    "preprocessing",
    "queued",
    "processing",
    "settling",
    "settled",
    "partial",
    "failed",
    "cancelled",
    "locked",
    "expired",
]

PAGE_STATUS = ["pending", "processing", "done", "empty", "failed", "skipped"]

MODE = ["ancient", "formula", "modern"]

MODE_LABEL = {
    "ancient": "繁体竖排古籍",
    "formula": "现代公式 / 简体",
    "modern": "现代横排",
}

# 合法迁移表。与 PRD §9.1 对齐：
# - locked 在充值补足后可 → settled
# - settled / partial / locked 满 72h 均可 → expired
# 注：开发规格 §4.1 的 ALLOWED 表漏了 locked → expired，已按 PRD 为准修正（见 spec-conflicts.md）
# - processing → queued 只有一个合法触发点：**worker 崩溃恢复**。
#   worker 进程被杀/断电时，已抢占的任务会永远停在 processing（claim_next 只认
#   queued），用户看到「识别中」一直到 72h 过期。启动时把它放回队列是唯一出路。
#   这是「回跳」的唯一豁免，业务代码不得使用（只许 recover_stale_tasks 调用）。
ALLOWED: dict[str, set[str]] = {
    "created": {"preprocessing"},
    "preprocessing": {"queued", "created"},
    "queued": {"processing"},
    "processing": {"settling", "partial", "failed", "cancelled", "queued"},
    "settling": {"settled", "locked"},
    "locked": {"settled", "expired"},
    "settled": {"expired"},
    "partial": {"expired"},
    "failed": {"expired"},
    "cancelled": {"expired"},
    "expired": set(),
}


class IllegalTransition(Exception):
    """非法状态迁移。资金场景下回跳等于重复扣费，必须抛错。"""

    def __init__(self, src: str, dst: str):
        self.src = src
        self.dst = dst
        super().__init__(f"illegal task transition: {src} -> {dst}")


def assert_transition(src: str, dst: str) -> None:
    if dst not in ALLOWED.get(src, set()):
        raise IllegalTransition(src, dst)


# ---------------------------------------------------------------- 充值单状态
RCG_STATUS = ["submitted", "confirmed", "rejected", "closed"]
ALLOWED_RCG: dict[str, set[str]] = {
    "submitted": {"confirmed", "rejected", "closed"},
    "confirmed": set(),
    "rejected": set(),
    "closed": set(),
}
RCG_STATUS_LABEL = {
    "submitted": "待确认",
    "confirmed": "已到账",
    "rejected": "已驳回",
    "closed": "已撤销",
}

# ---------------------------------------------------------------- 导出类型
EXPORT_KIND = ["vert_pdf", "vert_pdf_trad", "horiz_docx", "horiz_pdf", "proof_pdf", "markdown", "zip"]

EXPORT_KIND_LABEL = {
    "vert_pdf": "竖排阅读版 PDF",
    "vert_pdf_trad": "竖排阅读版 PDF（繁体原字）",
    "horiz_docx": "横排阅读版 Word",
    "horiz_pdf": "横排阅读版 PDF",
    "proof_pdf": "校对版 PDF",
    "markdown": "Markdown",
    "zip": "全部下载 ZIP",
}

# ---------------------------------------------------------------- 账本类型
LEDGER_TYPE = ["recharge", "consume", "refund", "adjust", "expire"]
LEDGER_TYPE_LABEL = {
    "recharge": "充值",
    "consume": "识别扣费",
    "refund": "失败退款",
    "adjust": "人工调整",
    "expire": "过期清零",
}

# ---------------------------------------------------------------- 块类型（框层着色）
BLOCK_LABEL = ["text", "title", "table", "formula", "figure", "header"]

# ---------------------------------------------------------------- 充值档位（FR-RCG-01，禁止自定义金额）
RECHARGE_PRESETS_MILL = [10_000, 30_000, 100_000, 300_000]  # ¥10 / ¥30 / ¥100 / ¥300
PAY_METHOD = ["wechat", "alipay"]  # D23 双通道

# ---------------------------------------------------------------- 任务状态中文（模板直接用）
TASK_STATUS_LABEL = {
    "created": "已创建",
    "preprocessing": "预处理中",
    "queued": "排队中",
    "processing": "识别中",
    "settling": "结算中",
    "settled": "已完成",
    "partial": "部分完成",
    "failed": "识别失败",
    "cancelled": "已取消",
    "locked": "待补款解锁",
    "expired": "已过期",
}

PAGE_STATUS_LABEL = {
    "pending": "等待",
    "processing": "识别中",
    "done": "完成",
    "empty": "未识别到文字",
    "failed": "失败",
    "skipped": "已跳过",
}

# 各状态允许的操作（PRD §9.1）
def can_view_text(status: str) -> bool:
    return status in {"processing", "settling", "settled", "partial", "locked"}


def can_download(status: str) -> bool:
    return status in {"settled", "partial"}


def can_cancel(status: str) -> bool:
    return status in {"created", "preprocessing", "queued", "processing"}


def can_retry_page(status: str) -> bool:
    return status in {"processing", "settled", "partial"}
