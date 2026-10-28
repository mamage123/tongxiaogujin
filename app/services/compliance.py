"""合规：关键词拦截 + AI 标识（FR-SYS-03/04/05）。

M1a 的定位要诚实：
- 关键词拦截是**最低门槛**，不是内容审查系统；命中只做「阻断 + 提示」，不自动申诉；
- 词表可热更新（存在 settings 表 `blocked_keywords`，逗号分隔），改词不用发版。

为什么不在这里做 AI 审核：成本与延迟都不划算，且用户上传前已承诺合法来源；
真正的兜底是「不明示鼓励、不留永久直链、72h 清理」。
"""
from __future__ import annotations

from ..db import get_setting

# 默认词表（保守，只放明确违规方向的高危词，避免误伤古籍用语）
DEFAULT_KEYWORDS = ",".join(
    [
        "代开发票", "办证刻章", "枪支弹药", "毒品交易", "赌博平台",
        "色情服务", "开奖网", "私彩", "洗钱", "裸聊",
    ]
)

AI_LABEL = "本文件由 AI 识别生成，供参考。"


def keyword_list() -> list[str]:
    raw = get_setting("blocked_keywords", DEFAULT_KEYWORDS)
    return [w.strip() for w in raw.split(",") if w.strip()]


def check_keywords(text: str, limit: int = 2000) -> tuple[bool, list[str]]:
    """检查文件名/文本片段。返回 (是否命中, 命中词)。

    只检查前 limit 个字符：拦截是为了防止明显滥用，不是为了全文审查（性能与误伤）。
    """
    if not text:
        return False, []
    sample = text[:limit]
    hits = [w for w in keyword_list() if w in sample]
    return bool(hits), hits


def ai_label() -> str:
    return get_setting("ai_label", AI_LABEL)


def agreement_required() -> dict:
    """协议版本（注册必须勾选，FR-SYS-04）。改协议文本必须升版本号。"""
    return {
        "version": get_setting("agreement_version", "2026-10-27"),
        "title": "用户协议与版权声明",
        "points": [
            "上传的内容须为本人拥有合法版权，或已取得权利人明确授权。",
            "识别结果仅用于个人学习、学术研究等合理用途，不得用于商业传播或侵权用途。",
            "不得上传涉及国家秘密、淫秽色情、暴力恐怖等违法违规内容。",
            "本平台不存储、不传播用户上传的原始文件内容；生成文件最多保留 72 小时，到期自动彻底删除。",
            "识别结果由 AI 生成，仅供参考；因识别误差导致的引用错误，请核对原件。",
            "余额仅用于本平台服务，不可提现、不可转让；平台停止服务时未使用余额全额退还。",
            "因用户违反本协议导致的一切法律纠纷与侵权投诉，由用户自行承担。",
        ],
    }
