"""归一化四步（开发规格 §5.4）。顺序不能变。

1) 去前言：剥离「以下是识别结果」「图片中文字如下」等
2) 结构清洗：合并段内换行、去多余空行、全半角统一
3) 繁简：ancient 模式且未要求保留繁体时转简；trad 导出走块的原文，不依赖本步
4) 字数：len(re.sub(r'\\s', '', plain_text))  ← 只在这里统计一次

铁律：任何模块再写一次 `len(text)` 用于计费都是缺陷（FR-BIL-12）。
"""
from __future__ import annotations

import re

from ...utils import fix_radicals
from .base import OcrBlock, OcrPageResult

try:
    import zhconv
except ImportError:  # pragma: no cover
    zhconv = None


# 模型爱加的前言（既算 token 成本，又会被算进用户账单，双重损失）
_PREAMBLE_PATTERNS = [
    # 「以下是图片中的文字：」「以下为识别出的文本：」——最常见的一类
    r"^\s*以下(是|为)?[^\n]{0,20}(识别|提取|OCR|文字|文本|内容)[^\n]{0,20}[：:]\s*\n?",
    r"^\s*图片?[中的]*[^\n]{0,15}(文字|文本|内容)[^\n]{0,10}[：:]\s*\n?",
    r"^\s*(识别|提取)结果[^\n]{0,10}[：:]\s*\n?",
    r"^\s*OCR\s*(result|输出)?[^\n]{0,10}[：:]\s*\n?",
    r"^\s*好的[，,]\s*以下[^\n]{0,30}[：:]\s*\n?",
    r"^\s*```(markdown|md|text)?\s*\n",  # 代码块包裹
]
_PREAMBLE_RE = [re.compile(p, re.IGNORECASE) for p in _PREAMBLE_PATTERNS]

# Markdown 标记（不能算进字数，否则用户为符号付费）
_MD_MARKS = re.compile(r"(^|\n)\s{0,3}(#{1,6}\s+|[-*+]\s+|\d+\.\s+|>\s?|\|?[-: ]+\|?\s*$)")
# 注意：这里**不能**用 `\\` 通吃反斜杠。PaddleOCR-VL 会把公式返回成
# `$\lambda$` / `$$\mathrm{Q}=2\pi\lambda$$`，一律删反斜杠会得到
# `lambda` / `mathrm{Q}` —— 公式语义被破坏且用户会当成识别错误。
_MD_INLINE = re.compile(r"(\*\*|__|`{1,3}|<[^>\n]{1,40}>)")
_TABLE_PIPE = re.compile(r"^\s*\|.*\|\s*$", re.MULTILINE)
# 行内/块级公式整体保护（见 strip_markdown）
_MATH_RE = re.compile(r"\$\$.+?\$\$|\$[^$\n]{1,300}?\$", re.DOTALL)


def _to_halfwidth(s: str) -> str:
    """全角 → 半角，**只转数字与拉丁字母**，中文标点保持全角。

    早期实现把 U+FF01~U+FF5E 整段折半，结果 `，；：？！（）` 全被转成
    `,;:?!()`，古籍交付稿里出现半角逗号（真实缺陷，2026-10-27 实测发现）。
    全角中文标点是排版的一部分，不是「全角数字」那种该归一的东西。
    """
    out = []
    for ch in s:
        code = ord(ch)
        if code == 0x3000:                       # 全角空格
            out.append(" ")
        elif 0xFF10 <= code <= 0xFF19:           # ０-９
            out.append(chr(code - 0xFEE0))
        elif 0xFF21 <= code <= 0xFF3A:           # Ａ-Ｚ
            out.append(chr(code - 0xFEE0))
        elif 0xFF41 <= code <= 0xFF5A:           # ａ-ｚ
            out.append(chr(code - 0xFEE0))
        else:
            out.append(ch)
    return "".join(out)


def _fix_radicals(s: str) -> str:
    """把 CJK 部首字符还原成正常汉字（实现在 app.utils.fix_radicals）。

    这里保留一层薄封装，是为了让 OCR 侧读代码时不必跳文件；
    导出层也用同一个实现，历史数据（库里已存了部首）才能在下载时被修正。
    """
    return fix_radicals(s)


def strip_preamble(text: str) -> str:
    t = text or ""
    # 去掉整体代码块包裹
    t = re.sub(r"^\s*```[a-zA-Z]*\n", "", t)
    t = re.sub(r"\n```\s*$", "", t)
    for _ in range(3):  # 允许连续多层前言
        before = t
        for r in _PREAMBLE_RE:
            t = r.sub("", t, count=1)
        if t == before:
            break
    return t.strip()


def strip_markdown(text: str) -> str:
    """把 Markdown 还原成纯文本。用于：纯文本导出、字数统计、Word 渲染。

    公式 `$...$` / `$$...$$` 先整体摘出来再放回：清洗规则会吃掉 LaTeX 的
    反斜杠（`\\lambda` → `lambda`），一旦发生，导出的公式就是错的。
    """
    t = text or ""

    saved: list[str] = []

    def _stash(m: re.Match[str]) -> str:
        saved.append(m.group(0))
        return f"\u0001M{len(saved) - 1}\u0001"

    t = _MATH_RE.sub(_stash, t)

    # 表格分隔行整行丢弃
    t = _TABLE_PIPE.sub(lambda m: re.sub(r"[|\-: ]", "", m.group(0)), t)
    t = _MD_MARKS.sub(lambda m: m.group(1) if m.group(1) else "", t)
    t = _MD_INLINE.sub("", t)

    for i, s in enumerate(saved):
        t = t.replace(f"\u0001M{i}\u0001", s)
    return t


def clean_structure(text: str, keep_linebreak: bool = False) -> str:
    """结构清洗：合并段内换行、去多余空行、行首行尾空白。

    关键点（FR-OCR-02 / EX 段落糊成一块）：
    OCR 的单换行往往表示「原文此行结束」，不是段落分隔。
    keep_linebreak=False 时：把单个换行并成空（中文不加空格），双换行保留为段落。
    keep_linebreak=True 时：保留原始换行（关闭格式优化）。
    """
    if not text:
        return ""
    t = text.replace("\r\n", "\n").replace("\r", "\n")
    t = re.sub(r"[ \t\u00a0]+\n", "\n", t)  # 行尾空白
    t = re.sub(r"[ \t]{2,}", " ", t)        # 多余空格

    if keep_linebreak:
        return t.strip()

    # 段落 = 2 个以上换行；单个换行视作同段换行 → 中文直接拼接
    paragraphs = re.split(r"\n{2,}", t)
    out = []
    for p in paragraphs:
        p = p.strip()
        if not p:
            continue
        merged = re.sub(r"\n+", "", p)  # 中文不加空格
        # 英文单词间的换行要补空格
        merged = re.sub(r"([A-Za-z0-9,.;:])\n([A-Za-z0-9])", r"\1 \2", merged)
        out.append(merged)
    return "\n\n".join(out)


def to_simplified(text: str) -> str:
    if not text:
        return ""
    if zhconv is None:
        return text
    # zh-hans 走词组表，"乾隆" 不会被转成 "干隆"
    return zhconv.convert(text, "zh-hans")


def count_chars(text: str) -> int:
    """计费字数：去掉所有空白后的长度。唯一口径。"""
    return len("".join((text or "").split()))


def join_blocks(blocks: list[OcrBlock], keep_linebreak: bool = False) -> str:
    """按 block_order 拼接块文本。竖排行序的正确性在这里体现。"""
    ordered = sorted(blocks, key=lambda b: b.order)
    parts = []
    for b in ordered:
        txt = (b.text or "").strip()
        if txt:
            parts.append(txt)
    return "\n\n".join(parts)


def normalize(
    result: OcrPageResult,
    mode: str = "ancient",
    optimize: bool = True,
    keep_traditional: bool = True,
) -> OcrPageResult:
    """归一化主入口。返回新的 OcrPageResult（不就地改供应商原始数据）。

    keep_traditional 默认 True：归一化**不**改字形。需要简体请在下游显式
    `to_simplified()`，且只在输出层（导出/展示）做。
    """
    # 块文本本身也要清洗（去前言/去 MD），否则框层里带 `##`
    clean_blocks: list[OcrBlock] = []
    for b in result.blocks:
        txt = strip_markdown(strip_preamble(b.text or ""))
        txt = _fix_radicals(txt)
        clean_blocks.append(
            OcrBlock(
                block_id=b.block_id,
                label=b.label,
                order=b.order,
                text=txt,
                bbox=b.bbox,
                polygon=b.polygon,
                group_id=b.group_id,
            )
        )

    # 步骤 1+2：以块顺序拼接，再清洗结构
    raw_joined = join_blocks(clean_blocks, keep_linebreak=not optimize)
    base = raw_joined if raw_joined.strip() else strip_markdown(strip_preamble(result.raw_text or ""))
    text = clean_structure(base, keep_linebreak=not optimize)
    text = _to_halfwidth(text)
    text = _fix_radicals(text)

    # 步骤 3：繁简。**默认保留原文**（keep_traditional 默认 True）。
    # 古籍的繁体字形是核心资产：一旦入库前转简，就再也还原不回「繁体原字」版，
    # 而这恰恰是七个导出格式里的一个。所以繁简只在输出层做，
    # 依据用户点的「简体 / 繁体原字」，与识别 mode 无关。
    if mode == "ancient" and not keep_traditional:
        text = to_simplified(text)

    # 步骤 4：字数在这里一次算清（下游只读 result.plain_text）
    result.blocks = clean_blocks
    result.plain_text = text
    if not result.raw_text:
        result.raw_text = raw_joined
    return result
