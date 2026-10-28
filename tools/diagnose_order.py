"""行序自检诊断：把某一页的三个信号分量单独算出来，看分数为什么掉。

用法：
  .venv/bin/python tools/diagnose_order.py <task_id> [page_no]
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.ocr.base import OcrBlock  # noqa: E402
from app.services.ocr.ordercheck import (  # noqa: E402
    ALL_PUNCT,
    WEIGHTS_HORIZONTAL,
    WEIGHTS_VERTICAL,
    _looks_vertical_layout,
    check_order,
)
from app.db import q  # noqa: E402


def load_blocks(task_id: str, page_no: int) -> list[OcrBlock]:
    rows = q(
        "SELECT * FROM page_blocks WHERE task_id=:t AND page_no=:p ORDER BY block_order",
        t=task_id, p=page_no,
    )
    out = []
    for r in rows:
        out.append(
            OcrBlock(
                block_id=r["block_id"], label=r["block_label"], order=int(r["block_order"]),
                text=r["text"] or "",
                bbox=(float(r["bbox_x1"]), float(r["bbox_y1"]),
                      float(r["bbox_x2"]), float(r["bbox_y2"])),
            )
        )
    return out


def main() -> int:
    tid = sys.argv[1] if len(sys.argv) > 1 else ""
    pno = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    if not tid:
        print("需要 task_id")
        return 2

    blocks = load_blocks(tid, pno)
    if not blocks:
        print("这页没有块")
        return 2

    vertical = _looks_vertical_layout(sorted(blocks, key=lambda b: b.order))
    passed, score = check_order(blocks, "ancient")

    total_chars = sum(len((b.text or "")) for b in blocks)
    punct = sum(1 for b in blocks for ch in (b.text or "") if ch in ALL_PUNCT)
    density = punct / total_chars if total_chars else 0

    # 复算两个文本信号
    dup = pairs = 0
    broken = 0
    for prev, cur in zip(blocks, blocks[1:]):
        pt = (prev.text or "").strip()
        ct = (cur.text or "").strip()
        if not pt or not ct:
            continue
        pairs += 1
        if pt[0] == ct[0]:
            dup += 1
        if pt[-1] not in ALL_PUNCT and ct[0] not in ALL_PUNCT:
            broken += 1
    dup_rate = dup / pairs if pairs else 0
    break_rate = broken / (len(blocks) - 1) if len(blocks) > 1 else 0

    w = WEIGHTS_VERTICAL if vertical else WEIGHTS_HORIZONTAL
    print(f"任务 {tid[:8]} 第 {pno} 页")
    print(f"  块数={len(blocks)}  字数={total_chars}  标点={punct}（密度 {density:.3%}）")
    print(f"  判定布局：{'竖排' if vertical else '横排'}  权重={w}")
    print(f"  行首重复率 dup   = {dup_rate:.3f}  (贡献 {w['dup']*dup_rate:.3f})")
    print(f"  句读断裂率 break = {break_rate:.3f}  (贡献 {w['break']*break_rate:.3f})")
    print(f"  => score={score}  passed={passed}")

    # 试算：把「版心短块」（书名/撰者/页码/图）剔掉后，栏连续性还剩多少断裂
    if vertical:
        long_blocks = [b for b in blocks if len((b.text or "").strip()) >= 12]
        if len(long_blocks) >= 3:
            centers = [(b.bbox[0] + b.bbox[2]) / 2 for b in long_blocks]
            widths = [max(1, b.bbox[2] - b.bbox[0]) for b in long_blocks]
            import statistics as st

            tol = max(st.median(widths) * 0.5, 1)
            clusters: list[float] = []
            for c in sorted(centers, reverse=True):
                if not clusters or abs(clusters[-1] - c) > tol:
                    clusters.append(c)
            ranks = [min(range(len(clusters)), key=lambda i: abs(clusters[i] - c)) for c in centers]
            steps = [(a, b) for a, b in zip(ranks, ranks[1:])]
            bad = sum(1 for a, b in steps if (b - a) not in (0, 1))
            print()
            print(f"  [试算] 只看正文块（≥12 字）：{len(long_blocks)} 块，{len(clusters)} 列")
            print(f"         列 rank 序列 = {ranks}")
            print(f"         步进         = {[b - a for a, b in steps]}")
            print(f"         断裂 {bad}/{len(steps)} = {bad/len(steps):.3f}")
            alt = 1 - (w["column"] * (bad / len(steps)) + w["dup"] * dup_rate + w["break"] * break_rate)
            print(f"         试算 score   = {max(0, round(alt, 4))}")
    print()
    print("  前 8 块：")
    for b in blocks[:8]:
        label = (b.label or "").ljust(14)
        txt = (b.text or "").replace("\n", " ")[:34]
        print(f"    #{b.order:<3} {label} {txt}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
