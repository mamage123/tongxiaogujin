"""横排 Word 导出（FR-EXP-04，M1a 唯一必须交付的格式）。

约束：
- 正文 12pt、段首缩进 2 字、保留标题层级、可编辑、**必须横排**
- 禁止竖排 Word（改一个字整列重排，做出来会被骂）
- 必须含 AI 标识与简体免责说明（FR-EXP-09）
"""
from __future__ import annotations

from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Cm, Pt

from ...db import q
from ...utils import fix_radicals
from ..ocr.normalize import to_simplified

BODY_PT = 12          # 小四
AI_LABEL = "本文件由 AI 识别生成，供参考。"
SIMPLIFIED_NOTE = (
    "简体由模型自动转换，人名、地名、年号等专名可能存在歧义，引用请核对繁体原件。"
)


def _set_font(run, name: str, size_pt: float | None = None, bold: bool = False):
    run.font.name = name
    run.font.size = Pt(size_pt) if size_pt else None
    run.bold = bold
    rpr = run._element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = rpr.makeelement(qn("w:rFonts"), {})
        rpr.append(rfonts)
    rfonts.set(qn("w:ascii"), name)
    rfonts.set(qn("w:hAnsi"), name)
    rfonts.set(qn("w:eastAsia"), name)


def _add_page_footer(doc, text: str):
    section = doc.sections[0]
    footer = section.footer
    p = footer.paragraphs[0] if footer.paragraphs else footer.add_paragraph()
    p.text = ""
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run(text)
    _set_font(run, "SourceHanSerifSC", 9)


def build_docx(
    task_id: str,
    out_path: str,
    lang: str = "simp",
    font_name: str = "SourceHanSerifSC",
    title: str | None = None,
) -> dict:
    """从 page_blocks 生成横排 Word。返回 {path, size, chars}。"""
    task = q("SELECT * FROM tasks WHERE id=:id", id=task_id)
    if not task:
        raise ValueError("任务不存在")
    task = task[0]

    pages = q(
        "SELECT page_no FROM task_pages WHERE task_id=:t AND status IN ('done','empty') ORDER BY page_no",
        t=task_id,
    )

    doc = Document()

    # 页面：A4 + 常规留白
    section = doc.sections[0]
    section.page_width = Cm(21.0)
    section.page_height = Cm(29.7)
    section.top_margin = Cm(2.54)
    section.bottom_margin = Cm(2.54)
    section.left_margin = Cm(2.8)
    section.right_margin = Cm(2.8)

    # 默认样式
    style = doc.styles["Normal"]
    style.font.size = Pt(BODY_PT)
    style.font.name = font_name
    style.element.rPr.rFonts.set(qn("w:eastAsia"), font_name)
    style.paragraph_format.space_after = Pt(0)
    style.paragraph_format.line_spacing = 1.5

    # 标题：居中 15pt（开发规格 §6 版式约定）
    doc_title = title or (task["filename"].rsplit(".", 1)[0] or "识别结果")
    h = doc.add_paragraph()
    h.alignment = WD_ALIGN_PARAGRAPH.CENTER
    h.paragraph_format.space_before = Pt(0)
    h.paragraph_format.space_after = Pt(12)
    hr = h.add_run(doc_title)
    _set_font(hr, font_name, 15, bold=True)

    total_chars = 0
    for p in pages:
        pn = int(p["page_no"])
        blocks = q(
            "SELECT * FROM page_blocks WHERE task_id=:t AND page_no=:p ORDER BY block_order",
            t=task_id, p=pn,
        )
        if not blocks:
            continue

        # 多页时插页码小标题（单页不加，避免无意义空行）
        if len(pages) > 1:
            ph = doc.add_paragraph()
            ph.paragraph_format.space_before = Pt(12)
            phr = ph.add_run(f"第 {pn} 页")
            _set_font(phr, font_name, 10.5, bold=True)

        for b in blocks:
            text = fix_radicals((b["text"] or "").strip())
            if not text:
                continue
            # 繁简只看 lang（用户点的是「简体」还是「繁体原字」），不看 mode。
            if lang == "simp":
                text = to_simplified(text)
            total_chars += len("".join(text.split()))

            label = b["block_label"] or "text"
            if label in ("title",):
                para = doc.add_paragraph()
                para.alignment = WD_ALIGN_PARAGRAPH.CENTER
                para.paragraph_format.space_before = Pt(10)
                para.paragraph_format.space_after = Pt(4)
                run = para.add_run(text)
                _set_font(run, font_name, 15, bold=True)
            elif label in ("header",):
                para = doc.add_paragraph()
                run = para.add_run(text)
                _set_font(run, font_name, 10.5)
                para.alignment = WD_ALIGN_PARAGRAPH.CENTER
            else:
                # 正文：一段一块，段首缩进 2 字
                for chunk in [c.strip() for c in text.split("\n\n") if c.strip()]:
                    para = doc.add_paragraph()
                    para.paragraph_format.first_line_indent = Pt(BODY_PT * 2)
                    para.paragraph_format.space_after = Pt(0)
                    run = para.add_run(chunk)
                    _set_font(run, font_name, BODY_PT)

    # 页脚：AI 标识 + 简体免责
    footer_text = AI_LABEL
    if lang == "simp":
        footer_text += "　" + SIMPLIFIED_NOTE
    _add_page_footer(doc, footer_text)

    # 文档属性（FR-SYS-03 另一处标识）
    doc.core_properties.comments = AI_LABEL
    doc.core_properties.title = doc_title

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out))
    return {"path": str(out), "size": out.stat().st_size, "chars": total_chars}
