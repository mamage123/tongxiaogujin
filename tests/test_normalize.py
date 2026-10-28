"""归一化与字数口径（FR-OCR-06 / FR-BIL-12）。

核心铁律：字数只统计一次，且「预览、计费、导出」必须同一口径。
"""
from __future__ import annotations

import pytest

from app.services.ocr.base import OcrBlock, OcrPageResult
from app.services.ocr.normalize import (
    clean_structure,
    count_chars,
    join_blocks,
    normalize,
    strip_markdown,
    strip_preamble,
    to_simplified,
)


# ---------------------------------------------------------------- 去前言
@pytest.mark.parametrize(
    "raw",
    [
        "以下是图片中的文字：\n欽定四庫全書",
        "以下是识别结果：\n欽定四庫全書",
        "图片中的文字如下：欽定四庫全書",
        "识别结果如下：\n\n欽定四庫全書",
        "Here is the text from the image:\n欽定四庫全書",
    ],
)
def test_strip_preamble(raw):
    assert "欽定四庫全書" in strip_preamble(raw)
    assert "以下是" not in strip_preamble(raw)
    assert "识别结果" not in strip_preamble(raw)


def test_strip_preamble_keeps_normal_text():
    s = "欽定四庫全書卷一"
    assert strip_preamble(s) == s


# ---------------------------------------------------------------- 结构清洗
def test_strip_markdown_removes_fences_and_bold():
    s = "```markdown\n# 卷一\n**乾隆**御製\n```"
    out = strip_markdown(s)
    assert "```" not in out
    assert "**" not in out
    assert "卷一" in out


def test_clean_structure_merges_single_newlines():
    """古籍竖排被识别成一行一句，必须合并成段，但中文之间不能插空格。"""
    s = "易曰觀乎天文\n以察時變\n日月星辰麗乎天者"
    out = clean_structure(s)
    assert out == "易曰觀乎天文以察時變日月星辰麗乎天者"


def test_clean_structure_keeps_blank_line_as_paragraph():
    s = "第一段\n第一段續\n\n第二段"
    out = clean_structure(s)
    assert "第一段第一段續" in out
    assert "第二段" in out
    assert out.count("\n") == 2  # 段间保留一个空行（\n\n），不是把两段并成一段


# ---------------------------------------------------------------- 繁简
def test_to_simplified_basic():
    assert to_simplified("繁體字測試") == "繁体字测试"


def test_to_simplified_keeps_proper_noun_qianlong():
    """「乾隆」是年号，必须保持原样（词组表生效），否则会被当成「干隆」。"""
    assert to_simplified("乾隆御製") == "乾隆御制"


def test_to_simplified_idempotent():
    s = "已經完成的識別"
    once = to_simplified(s)
    assert to_simplified(once) == once


# ---------------------------------------------------------------- 字数
@pytest.mark.parametrize(
    "text,expected",
    [
        ("", 0),
        ("   ", 0),
        ("欽定四庫", 4),
        ("欽 定 四 庫", 4),          # 空白不计
        ("欽定\n四庫", 4),
        ("欽定\t四庫\n\n全書", 6),
        ("a b c", 3),
    ],
)
def test_count_chars_ignores_whitespace(text, expected):
    assert count_chars(text) == expected


def test_result_chars_same_as_count_chars():
    """OcrPageResult.chars 与 count_chars 必须完全一致（唯一口径）。"""
    text = "欽定四庫全書\n卷一　天文"
    r = OcrPageResult(page_no=1, width=100, height=100, plain_text=text)
    assert r.chars == count_chars(text)


# ---------------------------------------------------------------- join_blocks
def test_join_blocks_follows_order():
    blocks = [
        OcrBlock(block_id="b", label="text", order=1, text="第二"),
        OcrBlock(block_id="a", label="text", order=0, text="第一"),
    ]
    out = join_blocks(blocks)
    assert out.index("第一") < out.index("第二")


def test_join_blocks_skips_empty():
    blocks = [
        OcrBlock(block_id="a", label="text", order=0, text="有字"),
        OcrBlock(block_id="b", label="text", order=1, text="   "),
    ]
    assert join_blocks(blocks) == "有字"


# ---------------------------------------------------------------- normalize 整链
def test_normalize_ancient_to_simplified():
    blocks = [
        OcrBlock(block_id="a", label="title", order=0, text="欽定四庫全書", bbox=(0, 0, 10, 10)),
        OcrBlock(block_id="b", label="text", order=1, text="易曰觀乎天文以察時變", bbox=(0, 10, 10, 20)),
    ]
    res = OcrPageResult(
        page_no=1, width=100, height=100, blocks=blocks,
        raw_text="以下是识别结果：\n欽定四庫全書\n易曰觀乎天文以察時變",
        provider="mock",
    )
    out = normalize(res, mode="ancient", optimize=False, keep_traditional=False)
    assert "钦定四库全书" in out.plain_text
    assert "以下是" not in out.plain_text
    assert out.chars == count_chars(out.plain_text)
    assert out.chars > 0


def test_normalize_keep_traditional():
    blocks = [OcrBlock(block_id="a", label="text", order=0, text="繁體字")]
    res = OcrPageResult(page_no=1, width=10, height=10, blocks=blocks, raw_text="繁體字")
    out = normalize(res, mode="ancient", optimize=False, keep_traditional=True)
    assert "繁體字" in out.plain_text


def test_normalize_default_keeps_traditional():
    """归一化默认必须保留原文。

    真实事故：worker 传了 keep_traditional=False，ancient 模式在**入库前**就被转简，
    于是「繁体原字」导出拿到的也是简体——数据已被改写，还原不回去。
    繁简只允许在输出层按用户选择做。
    """
    blocks = [OcrBlock(block_id="a", label="text", order=0, text="受漢禪，以帝爲尚書")]
    res = OcrPageResult(
        page_no=1, width=10, height=10, blocks=blocks, raw_text="受漢禪，以帝爲尚書"
    )
    out = normalize(res, mode="ancient", optimize=False)  # 不传 keep_traditional
    assert "漢" in out.plain_text and "爲" in out.plain_text
    assert "汉" not in out.plain_text and "为" not in out.plain_text


def test_normalize_chars_counted_once():
    """归一化跑两遍字数不变（幂等），说明没有重复统计。"""
    blocks = [OcrBlock(block_id="a", label="text", order=0, text="欽定四庫全書")]
    res = OcrPageResult(page_no=1, width=10, height=10, blocks=blocks, raw_text="欽定四庫全書")
    one = normalize(res, mode="ancient", optimize=False)
    two = normalize(one, mode="ancient", optimize=False)
    assert one.chars == two.chars


def test_normalize_radicals_become_ideographs():
    """康熙部首必须还原成汉字，且不能顺手把全角标点折成半角。

    OCR 会吐 ⻩ / ⼆ / ⻚ 这类码位（U+2E80–U+2FDF）：字形一样，但搜不到、
    粘贴到 Word 是怪字。修法是**逐字 NFKC**，只作用于部首区。
    整串 NFKC 会把 `，；：` 一起折成半角——那是排版的一部分，绝不能动。
    """
    blocks = [OcrBlock(block_id="a", label="text", order=0, text="⻩初⼆年，督軍官罷；遷侍中。")]
    res = OcrPageResult(page_no=1, width=10, height=10, blocks=blocks, raw_text="")
    out = normalize(res, mode="ancient", optimize=False)
    assert "黄初二年" in out.plain_text
    assert "，督軍官罷；遷侍中。" in out.plain_text, "全角标点被改坏了"
    assert not any(0x2E80 <= ord(c) <= 0x2FDF for c in out.plain_text)


def test_normalize_radicals_in_blocks_too():
    """框层文本（校对版左侧序号对应的块）也要修，不能只修 plain_text。"""
    blocks = [OcrBlock(block_id="a", label="text", order=0, text="⽂⽣⾃⼈⽤")]
    res = OcrPageResult(page_no=1, width=10, height=10, blocks=blocks, raw_text="")
    out = normalize(res, mode="ancient", optimize=False)
    assert out.blocks[0].text == "文生自人用"
