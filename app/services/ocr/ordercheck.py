"""行序自检（FR-OCR-06 ④，卖点 1 的兜底，开发规格 §6）。

三个信号：
1) 行首重复率：相邻块的行首字符重复出现 → 疑似跨栏串行（右栏第一行接左栏第一行）
2) 句读断裂率：块尾无标点却接下一块的中段 → 顺序被打乱
3) 栏连续性：同列（x 中心接近）的块必须连续出现；被别的列插队即为串栏

综合得分 = 1 - 加权异常率；< 0.98 判不通过 → 按「x 中心降序（右起）+ y 升序」重排。

禁止在没有 block_order 的情况下自己按 y 坐标瞎排（竖排双栏准确率难超 90%）。
"""
from __future__ import annotations

import statistics
from dataclasses import replace

from .base import OcrBlock

# 中文句读（句末 + 停顿）
SENT_END = "。！？!?…"
PAUSE = "，、；：,;:"
QUOTES = "」』”’）)》〉】"
ALL_PUNCT = SENT_END + PAUSE + QUOTES + "「『“‘（(《〈【"

PASS_THRESHOLD = 0.98

# 标题类块不参与文本信号：标题后面不跟句号是正常的，行首与正文重复也正常
# （书名/篇名常在正文首行再出现一次）。把它们算进去会系统性误报。
TITLE_LABELS = {
    "paragraph_title", "doc_title", "title", "table_title", "figure_title",
    "chart_title", "abstract_title", "number", "header", "footer",
}

# 竖排页与横排页的信号权重必须不同：
# - 竖排（窄高块）→ 栏连续性是硬判据（坐标直接体现阅读顺序），文本信号在
#   「一块=一整列」时噪声极大（跨列接续天然句读断裂）→ 只给很小的辅助权重。
# - 横排（宽扁块）→ 没有栏可判，只能靠文本信号，权重回到以文本为主。
# 曾把两者混用同一套 0.45/0.30/0.25，导致真实竖排页（块序完全正确）被判 0.9192 失败。
WEIGHTS_VERTICAL = {"column": 0.85, "dup": 0.10, "break": 0.05}
WEIGHTS_HORIZONTAL = {"column": 0.45, "dup": 0.30, "break": 0.25}

# 「正文块」的门槛：版心题名、撰者、卷次、页码、图，这些都不是正文流的一部分。
# 它们通常只有 2~8 个字，却散落在页面边缘的窄列里 —— 一旦让它们参与栏连续性判据，
# 阅读顺序会在「正文列 → 版心列 → 正文列」之间来回跳，被计成串栏。
# 实测（test6.pdf 第 10/12/20/30 页）：只看正文块时列步进全是 [0,1,1,1]，
# 断裂 0/4~0/5；把短块算进来后断裂率飙到 0.25~0.50，把本来正确的顺序判成 0.54~0.88。
MIN_BODY_CHARS = 6

# 这些 label 明确不是正文流，即使字数够也不参与栏连续性
NON_BODY_LABELS = {
    "number", "image", "figure", "table", "header", "footer",
    "page_number", "seal", "stamp",
}

# 标点密度低于这个值时，「句读断裂」信号失效 —— 无标点古籍里
# 「块尾没标点」恒为真，break_rate 常年 0.6~0.75，这一项反而成了主要扣分器。
MIN_PUNCT_DENSITY = 0.005


def _x_center(b: OcrBlock) -> float:
    return (b.bbox[0] + b.bbox[2]) / 2


def _y_center(b: OcrBlock) -> float:
    return (b.bbox[1] + b.bbox[3]) / 2


def _is_vertical_block(b: OcrBlock) -> bool:
    """竖排的块应该是竖长条。横条说明模型没真懂竖排（FR-RES-08 ②）。"""
    w = b.bbox[2] - b.bbox[0]
    h = b.bbox[3] - b.bbox[1]
    return h >= w


def _looks_vertical_layout(blocks: list[OcrBlock]) -> bool:
    """判断这页是不是竖排：多数块是竖长条 + 块宽度小于页面宽的 1/3。"""
    if not blocks:
        return False
    vertical = sum(1 for b in blocks if _is_vertical_block(b))
    return vertical / len(blocks) >= 0.5


# 章回小说的常见发语词。连续两段都以「卻說」「話說」起头是叙述习惯，不是错行拼接；
# 不豁免的话每碰上一次就白扣一次行首重复（实测 test6.pdf 第 6 页 dup=0.2 → 0.9737 不过）。
NARRATIVE_OPENERS = {
    "卻說", "却说", "話說", "话说", "且說", "且说", "卻道", "却道",
    "原來", "原来", "只見", "只见", "卻見", "却见", "再說", "再说",
    "單說", "单说", "卻早", "却早", "不在", "休題", "休题",
}


def _is_title_block(b: OcrBlock) -> bool:
    return (b.label or "").strip().lower() in TITLE_LABELS


def _both_narrative_openers(a: str, b: str) -> bool:
    """两块都以同一个常见发语词起头 → 是文风，不是串栏。"""
    return len(a) >= 2 and len(b) >= 2 and a[:2] == b[:2] and a[:2] in NARRATIVE_OPENERS


def _is_same_heading(a: str, b: str) -> bool:
    """回目与它的副标题：`孔明用智激周瑜` 是 `孔明用智激周瑜 孫權決計破曹操` 的前缀。

    古籍回目常拆成「大题 + 双句」两块，首字相同是版式的必然，不是错行拼接。
    不豁免的话每页都白扣一次行首重复（实测 test6.pdf 第 47 页 dup=0.167 → 0.9737 判不通过）。
    长度门槛 ≥4 字，避免「曰」「一」这种单字被当成前缀。
    """
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    return len(short) >= 4 and long_.startswith(short)


def check_order(blocks: list[OcrBlock], mode: str = "ancient") -> tuple[bool, float]:
    """返回 (是否通过, 连续性得分 0~1)。仅 ancient 模式启用。"""
    if mode != "ancient":
        return True, 1.0
    if len(blocks) < 3:
        return True, 1.0

    ordered = sorted(blocks, key=lambda b: b.order)
    vertical = _looks_vertical_layout(ordered)

    # ---- 信号一：行首重复
    dup = 0
    pairs = 0
    for prev, cur in zip(ordered, ordered[1:]):
        pt = (prev.text or "").strip()
        ct = (cur.text or "").strip()
        if not pt or not ct:
            continue
        if _is_title_block(prev) or _is_title_block(cur):
            continue  # 标题/书名与正文重复是常态，不是串栏
        if _is_same_heading(pt, ct):
            continue  # 回目 + 副标题，同首字是版式
        if _both_narrative_openers(pt, ct):
            continue  # 都以「卻說」「話說」起头，是章回体叙述习惯
        pairs += 1
        if pt[0] == ct[0]:
            dup += 1
    dup_rate = dup / pairs if pairs else 0.0

    # ---- 信号二：句读断裂
    # 块尾是句末标点 → 下一块正常开始；块尾没有标点且下一块不是同组 → 可疑
    broken = 0
    for prev, cur in zip(ordered, ordered[1:]):
        pt = (prev.text or "").strip()
        ct = (cur.text or "").strip()
        if not pt or not ct:
            continue
        if prev.group_id and prev.group_id == cur.group_id:
            continue  # 夹注同组，允许
        if _is_title_block(prev) or _is_title_block(cur):
            continue  # 标题后面本就不跟句号
        if pt[-1] not in ALL_PUNCT and ct[0] not in ALL_PUNCT:
            broken += 1
    break_rate = broken / (len(ordered) - 1) if len(ordered) > 1 else 0.0

    # ---- 信号三：栏连续性
    # 竖排的 x 中心成簇（每列一簇）。把中心从右到左排成 rank 后，
    # 正常阅读序列的相邻两块只应出现两种步进：0（同列换行）或 +1（换到左边一列）。
    # 出现 -1（回头跳回已读列）或 >1（跳过整列）都算串栏。
    # 只看「回头」会漏掉 0,2,1 这种轮换，所以这里按步进取值判断。
    #
    # **只统计正文块**：版心题名/撰者/页码是短块，散落在边缘窄列，
    # 让它们参与会让 x 中心来回跳，把正确顺序误判成串栏（见 MIN_BODY_CHARS 注释）。
    column_break = 0
    body = [
        b for b in ordered
        if len((b.text or "").strip()) >= MIN_BODY_CHARS
        and (b.label or "").strip().lower() not in NON_BODY_LABELS
    ]
    if vertical and len(body) >= 3:
        centers = [_x_center(b) for b in body]
        widths = [max(1, b.bbox[2] - b.bbox[0]) for b in body]
        tol = max(statistics.median(widths) * 0.5, 1)
        # 从右到左聚类
        clusters: list[float] = []
        for c in sorted(centers, reverse=True):
            if not clusters or abs(clusters[-1] - c) > tol:
                clusters.append(c)
        ranks = [
            min(range(len(clusters)), key=lambda i: abs(clusters[i] - c)) for c in centers
        ]
        for a, b in zip(ranks, ranks[1:]):
            if (b - a) not in (0, 1):
                column_break += 1
        column_break_rate = column_break / max(1, len(ranks) - 1)
    else:
        column_break_rate = 0.0

    # ---- 权重：判据失效时不能让它的权重继续压分
    total_chars = sum(len((b.text or "")) for b in ordered)
    punct_n = sum(1 for b in ordered for ch in (b.text or "") if ch in ALL_PUNCT)
    punct_density = punct_n / total_chars if total_chars else 0.0

    w = dict(WEIGHTS_VERTICAL if vertical else WEIGHTS_HORIZONTAL)
    if not vertical:
        # 横排没有「栏」可判，column 权重留着只会白白吃掉 0.45
        w["column"] = 0.0
    else:
        # 竖排的「块」通常就是一整列，列与列之间是同一段话的续排，本来就不带标点。
        # 用「块尾有没有标点」衡量竖排是否串行，是拿横排的规则套竖排 ——
        # 实测第 5 页（评点本，标点密度 2.1%）正文块列步进全 [1,1,1,1]、
        # 断裂 0/4，却因为 break=0.778 被压到 0.9611 判不通过。
        w["break"] = 0.0
    if punct_density < MIN_PUNCT_DENSITY:
        # 整页几乎没标点（古籍常态），横排也没有句读可依
        w["break"] = 0.0
    if (s := sum(w.values())) > 0:
        w = {k: v / s for k, v in w.items()}

    score = 1.0 - (
        # 竖排无正文块/横排时 column_break_rate 恒 0，乘不乘权重都一样
        w["column"] * column_break_rate + w["dup"] * dup_rate + w["break"] * break_rate
    )
    score = max(0.0, min(1.0, score))
    return score >= PASS_THRESHOLD, round(score, 4)


def reorder_vertical(blocks: list[OcrBlock]) -> list[OcrBlock]:
    """竖排重排：右起（x 中心降序）分列 → 列内自上而下（y 升序）→ 重编号 order。

    仅在自检不通过时使用；这是兜底，不是主路径（主路径信 block_order）。
    """
    if not blocks:
        return []
    widths = [max(1, b.bbox[2] - b.bbox[0]) for b in blocks]
    col_gap = max(statistics.median(widths) * 0.6, 1)
    max_x = max(_x_center(b) for b in blocks)
    keyed = []
    for b in blocks:
        col = int((max_x - _x_center(b)) // col_gap)  # 0 = 最右列
        keyed.append((col, _y_center(b), b))
    keyed.sort(key=lambda t: (t[0], t[1]))
    out = []
    for i, (_, _, b) in enumerate(keyed):
        out.append(replace(b, order=i))
    return out


def apply_order_check(
    blocks: list[OcrBlock], mode: str, trusted: bool = False
) -> tuple[list[OcrBlock], bool, float]:
    """自检 + 必要时重排。返回 (块, 是否通过, 得分)。

    `trusted=True` 表示供应商明确给出了阅读顺序（星河实测 block_order 就是右起列序）。
    此时自检只用来**打分与标记**，不重排——用坐标猜出来的列序未必比供应商的准，
    重排反而会把正确的顺序弄坏。而是否要通过自检由调用方决定展示文案。

    `trusted=False`（mock/GLM 这类没有可信顺序的通道）保留重排兜底。
    """
    if mode != "ancient":
        return blocks, True, 1.0
    passed, score = check_order(blocks, mode)
    if passed:
        return blocks, True, score
    if trusted:
        # 顺序来自供应商本人，不推翻，只如实标记「存疑」
        return blocks, False, score
    reordered = reorder_vertical(blocks)
    passed2, score2 = check_order(reordered, mode)
    # 重排后即使分数上升，也标记为「需人工抽检」（passed2 决定是否可用）
    return reordered, passed2, score2
