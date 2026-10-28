"""整本重算行序自检：把某任务所有已识别页按**当前**判定逻辑重跑一遍，
对比库里存的旧结果。

用途：算法改动后，不重新识别（不花钱、不耗时）也能知道「如果现在跑，
通过率会是多少」，以及「哪些页是真的有问题」。

用法：
  .venv/bin/python tools/recheck_order.py <task_id>
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import q  # noqa: E402
from app.services.ocr.ordercheck import check_order  # noqa: E402

from tools.diagnose_order import load_blocks  # noqa: E402


def main() -> int:
    tid = sys.argv[1] if len(sys.argv) > 1 else ""
    if not tid:
        print("用法：recheck_order.py <task_id>")
        return 2

    pages = q(
        "SELECT page_no, status, chars, order_check_passed FROM task_pages"
        " WHERE task_id=:t AND status IN ('done','empty') ORDER BY page_no",
        t=tid,
    )
    if not pages:
        print("这个任务还没有识别完成的页")
        return 2

    old_ok = 0
    new_ok = 0
    changed = []
    rows = []
    for p in pages:
        blocks = load_blocks(tid, int(p["page_no"]))
        if not blocks:
            continue
        passed, score = check_order(blocks, "ancient")
        old = p["order_check_passed"]
        if old == 1:
            old_ok += 1
        if passed:
            new_ok += 1
        if (old == 1) != passed:
            changed.append((int(p["page_no"]), old, score))
        rows.append((int(p["page_no"]), len(blocks), int(p["chars"]), old, score, passed))

    n = len(rows)
    print(f"任务 {tid[:8]}  {n} 页")
    print(f"  旧判定通过：{old_ok}/{n} = {old_ok/n:.1%}")
    print(f"  新判定通过：{new_ok}/{n} = {new_ok/n:.1%}")
    print()

    if changed:
        print(f"判定发生变化的页（{len(changed)} 页）：")
        print(f"  {'页':>5}{'旧':>4}{'新分':>8}")
        for pno, old, score in changed[:40]:
            print(f"  {pno:>5}{old:>4}{score:>8}")
        if len(changed) > 40:
            print(f"  …还有 {len(changed)-40} 页")
        print()

    fails = [r for r in rows if not r[5]]
    if fails:
        print(f"按新逻辑仍不通过（{len(fails)} 页）—— 这些才需要人工核对：")
        for pno, nb, ch, _old, score, _p in fails[:20]:
            print(f"  第 {pno} 页  块={nb} 字={ch} score={score}")
    else:
        print("按新逻辑全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
