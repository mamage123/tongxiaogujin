"""Markdown 与纯文本派生（展示层，不是数据层）。

铁律：Markdown 只能从 OcrPageResult 生成，不能当输入格式用。
"""
from __future__ import annotations

from ...db import q
from ...utils import fix_radicals
from ..ocr.normalize import to_simplified

AI_LABEL = "> 本文件由 AI 识别生成，供参考。"
SIMPLIFIED_NOTE = "> 简体由模型自动转换，人名、地名、年号等专名可能存在歧义，引用请核对繁体原件。"


def build_markdown(task_id: str, lang: str = "simp", with_meta: bool = True) -> str:
    task = q("SELECT * FROM tasks WHERE id=:id", id=task_id)
    if not task:
        raise ValueError("任务不存在")
    task = task[0]

    pages = q(
        "SELECT page_no FROM task_pages WHERE task_id=:t AND status IN ('done','empty') ORDER BY page_no",
        t=task_id,
    )

    lines: list[str] = []
    title = (task["filename"].rsplit(".", 1)[0] or "识别结果").strip()
    lines.append(f"# {title}")
    lines.append("")

    for p in pages:
        pn = int(p["page_no"])
        blocks = q(
            "SELECT * FROM page_blocks WHERE task_id=:t AND page_no=:p ORDER BY block_order",
            t=task_id, p=pn,
        )
        if not blocks:
            continue
        if len(pages) > 1:
            lines.append(f"## 第 {pn} 页")
            lines.append("")
        for b in blocks:
            text = fix_radicals((b["text"] or "").strip())
            if not text:
                continue
            # 繁简只看 lang（用户在导出/切换上的明确选择），不看 mode。
            if lang == "simp":
                text = to_simplified(text)
            label = b["block_label"] or "text"
            if label == "title":
                lines.append(f"### {text}")
            elif label == "formula":
                lines.append("```")
                lines.append(text)
                lines.append("```")
            elif label == "table":
                lines.append(text)  # 原样保留管道表
            else:
                lines.append(text)
            lines.append("")

    if with_meta:
        lines.append("---")
        lines.append("")
        lines.append(AI_LABEL)
        if lang == "simp":
            lines.append(SIMPLIFIED_NOTE)
        lines.append("")
        lines.append(f"任务号：`{task_id[:8]}`　模式：{task['mode']}　字数：{task['chars']}")
    return "\n".join(lines)


def build_plain_text(task_id: str, lang: str = "simp") -> str:
    """纯文本：预览右侧与「复制全文」用。段落之间空一行。"""
    task = q("SELECT * FROM tasks WHERE id=:id", id=task_id)
    if not task:
        raise ValueError("任务不存在")
    task = task[0]
    rows = q(
        "SELECT page_no, text, block_label FROM page_blocks WHERE task_id=:t ORDER BY page_no, block_order",
        t=task_id,
    )
    parts: list[str] = []
    for r in rows:
        text = fix_radicals((r["text"] or "").strip())
        if not text:
            continue
        if lang == "simp":
            text = to_simplified(text)
        parts.append(text)
    return "\n\n".join(parts)


def build_chinese_vertical_text(task_id: str, lang: str = "simp") -> str:
    """竖排版用：块之间用换行，保留段落。"""
    return build_plain_text(task_id, lang)
