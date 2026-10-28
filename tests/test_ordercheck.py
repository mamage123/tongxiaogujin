"""竖排行序自检三信号（开发规格 §5.5 / 用例 P0-07）。

真实场景：古籍一页多列，识别服务常把「第 2 列的最后一句」接到「第 1 列中间」，
读者会看到语义断裂却很难察觉。这里用三个信号兜住：
  行首重复率 / 句读断裂率 / 栏连续性，加权得分 < 0.98 判为可疑。
"""
from __future__ import annotations

from dataclasses import replace

from app.services.ocr.base import OcrBlock
from app.services.ocr.ordercheck import (
    PASS_THRESHOLD,
    apply_order_check,
    check_order,
    reorder_vertical,
)

# 一栏 = 一个窄而高的块（竖排的真实形态）
COL_W = 66
COL_H = 900


def vcols(columns: list[str], x_starts: list[int]) -> list[OcrBlock]:
    """按「一列一块」造竖排页。x_starts 从右到左（index 0 = 最右列）。"""
    blocks = []
    for i, (text, x1) in enumerate(zip(columns, x_starts)):
        blocks.append(
            OcrBlock(
                block_id=f"col{i}", label="text", order=i, text=text,
                bbox=(x1, 100, x1 + COL_W, 100 + COL_H),
                group_id=f"col{i}",
            )
        )
    return blocks


def resequence(blocks: list[OcrBlock], order: list[int]) -> list[OcrBlock]:
    """人为打乱阅读顺序（模拟供应商串栏）。"""
    return [replace(b, order=order[i]) for i, b in enumerate(blocks)]


def texts_in_order(blocks: list[OcrBlock]) -> list[str]:
    return [b.text for b in sorted(blocks, key=lambda b: b.order)]


RIGHT = "易曰觀乎天文以察時變。"
MID = "日月星辰麗乎天者也。"
LEFT = "聖人仰觀俯察而知其道。"


# ---------------------------------------------------------------- 正常页
def test_good_vertical_page_passes():
    blocks = vcols([RIGHT, MID, LEFT], [980, 820, 660])
    passed, score = check_order(blocks, "ancient")
    assert passed is True
    assert score == 1.0


def test_duplicate_line_start_detected():
    """连续多块都以「曰」开头 → 多半是错行拼接（横排页也会触发该信号）。"""
    blocks = [
        OcrBlock(block_id=f"l{i}", label="text", order=i, text=f"曰{chr(0x4E00 + i)}…",
                 bbox=(100, 100 + i * 80, 1100, 170 + i * 80))
        for i in range(4)
    ]
    passed, score = check_order(blocks, "ancient")
    assert score < 1.0
    assert passed is False


def test_short_page_skips_check():
    """块太少不做判断（避免误杀单行页）。"""
    blocks = [OcrBlock(block_id="a", label="text", order=0, text="僅一行")]
    passed, score = check_order(blocks, "ancient")
    assert passed is True and score == 1.0


def test_non_ancient_mode_always_passes():
    blocks = resequence(vcols([RIGHT, MID, LEFT], [980, 820, 660]), [0, 2, 1])
    passed, score = check_order(blocks, "modern")
    assert passed is True and score == 1.0


# ---------------------------------------------------------------- 串栏
def test_columns_interleaved_fails():
    """典型的串栏：右列 → 左列 → 又跳回右列。"""
    blocks = resequence(vcols([RIGHT, MID, LEFT], [980, 820, 660]), [0, 2, 1])
    passed, score = check_order(blocks, "ancient")
    assert passed is False
    assert score < PASS_THRESHOLD


def test_worst_interleave_scores_lower():
    """乱得越厉害分越低（保证信号有区分度，不是二值碰运气）。"""
    mild = resequence(vcols([RIGHT, MID, LEFT], [980, 820, 660]), [0, 2, 1])
    _, mild_score = check_order(mild, "ancient")

    cols = [RIGHT, MID, LEFT, "故天垂象見吉凶。"]
    worst = resequence(vcols(cols, [980, 820, 660, 500]), [0, 2, 1, 3])
    _, worst_score = check_order(worst, "ancient")

    assert worst_score <= mild_score


def test_reorder_fixes_interleaved():
    blocks = resequence(vcols([RIGHT, MID, LEFT], [980, 820, 660]), [0, 2, 1])
    fixed = reorder_vertical(blocks)
    assert texts_in_order(fixed) == [RIGHT, MID, LEFT]


def test_reorder_is_right_to_left():
    """右起阅读：x 最大的列 order 最小。"""
    blocks = vcols([RIGHT, MID, LEFT], [980, 820, 660])
    fixed = reorder_vertical(blocks)
    first = min(fixed, key=lambda b: b.order)
    assert first.text == RIGHT
    assert first.bbox[0] == max(b.bbox[0] for b in blocks)


def test_apply_order_check_repairs_and_reports():
    blocks = resequence(vcols([RIGHT, MID, LEFT], [980, 820, 660]), [0, 2, 1])
    out, passed, score = apply_order_check(blocks, "ancient")
    assert texts_in_order(out) == [RIGHT, MID, LEFT]
    assert passed is True
    assert score == 1.0


def test_apply_order_check_keeps_good_page_untouched():
    blocks = vcols([RIGHT, MID, LEFT], [980, 820, 660])
    out, passed, _ = apply_order_check(blocks, "ancient")
    assert passed is True
    assert texts_in_order(out) == [RIGHT, MID, LEFT]


# ---------------------------------------------------------------- 边界
def test_empty_blocks():
    passed, score = check_order([], "ancient")
    assert passed is True and score == 1.0
    assert reorder_vertical([]) == []


def test_score_never_out_of_range():
    blocks = resequence(vcols([RIGHT, MID, LEFT], [980, 820, 660]), [2, 0, 1])
    _, score = check_order(blocks, "ancient")
    assert 0.0 <= score <= 1.0


# ---------------------------------------------------------------- 版心 / 标点 / 回目
# 这一组来自 test6.pdf（113 页真实古籍）的实测：改之前通过率只有 8.5%，
# 也就是每页都提示「行序存疑」。逐页诊断发现三个信号里有两个在误伤。
def _colophon(text: str, x1: int, y1: int, order: int, label: str = "vertical_text"):
    """版心块：书名 / 撰者 / 卷次 / 页码，窄且短，贴在页面边缘。"""
    return OcrBlock(
        block_id=f"c{order}", label=label, order=order, text=text,
        bbox=(x1, y1, x1 + COL_W, y1 + 120),
    )


def test_colophon_short_blocks_do_not_break_column_score():
    """版心短块插在正文列之间，不能算串栏。

    真实版式：正文列 → 书名/撰者列 → 正文列。x 中心来回跳是版式本身，
    不是供应商串栏。实测第 10 页因此从 1.0 掉到 0.5437。
    """
    cols = vcols([RIGHT, MID, LEFT], [980, 820, 660])  # order 0,1,2
    # 在最右列之后插一块 4 字书名、一块 4 字撰者
    title = _colophon("三國演義", 980, 40, 1)
    author = _colophon("羅貫中撰", 660, 40, 3)
    blocks = [cols[0], title, replace(cols[1], order=2), author, replace(cols[2], order=4)]

    passed, score = check_order(blocks, "ancient")
    assert passed is True, f"版心短块把正确顺序判成了串栏（score={score}）"
    assert score == 1.0


def test_real_interleave_still_caught_with_colophon_present():
    """加了版心豁免之后，**真**串栏（正文列回头跳）仍然要被抓到。"""
    cols = vcols([RIGHT, MID, LEFT], [980, 820, 660])
    blocks = resequence(
        [cols[0], _colophon("三國演義", 980, 40, 99), cols[1], cols[2]],
        [0, 1, 3, 2],  # 中间列与左列换位 = 回头
    )
    blocks[1] = replace(blocks[1], order=1)
    passed, score = check_order(blocks, "ancient")
    assert passed is False, f"真串栏被放过了（score={score}）"


def test_page_number_block_excluded_from_column_check():
    """页码块（label=number）即使字数够也不算正文流。"""
    cols = vcols([RIGHT, MID, LEFT], [980, 820, 660])
    page_no = OcrBlock(
        block_id="pn", label="number", order=3, text="五四五 四三",
        bbox=(120, 1000, 300, 1080),
    )
    blocks = [cols[0], cols[1], cols[2], page_no]
    passed, score = check_order(blocks, "ancient")
    assert passed is True, f"页码块被当成串栏（score={score}）"


def test_no_punctuation_page_not_penalized():
    """整页无标点的古籍：句读断裂信号必须失效，不能天天扣分。

    旧逻辑下 break_rate 恒为 1.0，0.05×1 = 0.05，直接把 0.95 压在阈值 0.98 之下。
    """
    a = "卻說蔡瑁張允歸見劉琮具言曹操許保奏將軍永鎭荆襄"
    b = "次日與母蔡夫人商議欲將荆州獻與曹操蔡夫人曰此計大妙"
    c = "劉備若據此地急難動搖操曰孤當親提大軍速取荆州"
    blocks = vcols([a, b, c], [980, 820, 660])
    # 顺序正确，且三块首尾都没有标点
    assert all(b.text[-1] not in "。，、；：" for b in blocks)

    passed, score = check_order(blocks, "ancient")
    assert passed is True, f"无标点古籍被误判（score={score}）"
    assert score == 1.0


def test_vertical_ignores_sentence_break_even_with_punctuation():
    """竖排即使有标点也不靠句读判序：列与列是同一段话的续排，换列处本就不加标点。"""
    a = "孫策之知太史慈不以新降而疑其詐玄德之信子龍不以臨難而疑"
    b = "文有伏線之妙玄德之取長沙魏延之救黃忠尚隔數回而此處襄陽"
    c = "徐氏以不死報夫仇糜氏以一死全夫嗣皆賢妻也吳夫人臨死"
    blocks = vcols([a + "。", b, c], [980, 820, 660])  # 只有第一块有句号

    passed, score = check_order(blocks, "ancient")
    assert passed is True, f"竖排被句读信号误伤（score={score}）"


def test_heading_and_subheading_same_start_not_dup():
    """回目 + 副标题同首字是版式，不是错行拼接。

    `孔明用智激周瑜` 是 `孔明用智激周瑜 孫權決計破曹操` 的前缀。
    实测第 47 页因此 dup=0.167，score 0.9737 判不通过。
    """
    head = "孔明用智激周瑜"
    sub = "孔明用智激周瑜 孫權決計破曹操"
    body1 = "為江東之患不如殺之周郎欲殺孔明乃是孔明知己乃令人連夜請魯肅入帳"
    body2 = "調撥軍馬主公心有疑否權曰但憂曹操兵多寡不敵衆耳他無所疑臥龍先生"
    blocks = vcols([body2, head, sub, body1], [980, 820, 660, 500])

    passed, score = check_order(blocks, "ancient")
    assert passed is True, f"回目与副标题被当成行首重复（score={score}）"


def test_single_char_prefix_not_exempted():
    """单字前缀不能豁免，否则「曰…」「一…」这种真错行全被放过。"""
    blocks = [
        OcrBlock(block_id=f"l{i}", label="text", order=i, text=f"曰{chr(0x4E00 + i)}某句話在此處相接",
                 bbox=(100, 100 + i * 80, 1100, 170 + i * 80))
        for i in range(4)
    ]
    passed, _ = check_order(blocks, "ancient")
    assert passed is False


def test_narrative_openers_not_counted_as_duplicate():
    """连续两段都以「卻說」「話說」起头，是章回体的叙述习惯，不是错行。

    实测 test6.pdf 第 6 页：两段正文都以「卻說」开头 → dup=0.2 → 0.9737 不过。
    """
    a = "卻說張飛因關公放了上流水遂引軍從下流殺將來截住曹仁混殺忽遇許褚便與交鋒"
    b = "卻說曹仁收拾殘軍就新野屯住使曹洪去見曹操具言失利之事操大怒曰諸葛村夫安敢如此"
    c = "也操從其言便問誰可為使劉曄曰徐庶與劉備至厚今現在軍中何不命他一往操從之"
    blocks = vcols([a, b, c], [980, 820, 660])

    passed, score = check_order(blocks, "ancient")
    assert passed is True, f"发语词被当成行首重复（score={score}）"


def test_real_duplicate_beyond_openers_still_caught():
    """不是发语词的重复首字仍然要抓 —— 豁免只针对固定词表。"""
    a = "曹操大怒曰諸葛村夫安敢如此吾誓必擒之"
    b = "曹操遂引大軍南下欲取荆州劉備聞之與孔明商議"
    c = "孫權亦遣魯肅過江探聽虛實兩家遂有同盟之議"
    blocks = vcols([a, b, c], [980, 820, 660])
    passed, score = check_order(blocks, "ancient")
    assert passed is False, f"真行首重复被放过（score={score}）"
