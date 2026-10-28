#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把官方思源宋体加工成「能正确嵌入 PDF」的字库，放进 fonts/。

为什么不能直接用官方 OTF / 原样 TTF：

1. **CID-keyed CFF 无法子集化**
   Chromium 打印 PDF 时对 .otf（CID-keyed CFF）会退化成「每个字一个 Type3
   字体」：体积翻 4 倍、放大发虚。同名 TTF 才能嵌入为 Type0。

2. **glyph 名沿用康熙部首，导致文本层被改写**
   思源宋体的字形是按部首命名/共享的，例如：
       黄 U+9EC4 → glyph uni2EE9
       ⻩ U+2EE9 → glyph uni2EE9
   Chromium 生成 ToUnicode 时优先用 glyph 名（uniXXXX 形式可信），于是
   复制出来的「黄」变成「⻩」、「二」变成「⼆」。字形一样，但搜不到、
   粘贴到 Word 是怪字。实测一页 190 字的文档会污染 13~14 个字。

加工方案（两步，都不改字形与度量）：
  a. 把 post 表降为 3.0（不保留 glyph 名），逼 Chromium 只能按 cmap 反查；
  b. 从所有 cmap 子表里删掉 U+2E80–U+2FDF（CJK 部首区）码位，
     这样反查只可能落到真正的汉字码位上。

授权：思源宋体为 SIL OFL 1.1（无保留字体名），允许修改与再分发，
修改件需保留版权与许可声明（fonts/OFL-SourceHanSerif.txt）。

用法：
  # 首次：把官方 OTF 转 TTF 并加工
  python tools/build_fonts.py --otf ~/Downloads/SourceHanSerifSC-Regular.otf --name SourceHanSerifSC
  # 已经有 TTF，只做加工
  python tools/build_fonts.py --ttf .tmp/SourceHanSerifSC-Regular.ttf --name SourceHanSerifSC
  # 检查现有文件是否已加工
  python tools/build_fonts.py --verify fonts/SourceHanSerifSC-Regular.ttf
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FONT_DIR = ROOT / "fonts"

RADICAL_RANGES = ((0x2E80, 0x2EFF), (0x2F00, 0x2FDF))


def is_radical_cp(cp: int) -> bool:
    return any(lo <= cp <= hi for lo, hi in RADICAL_RANGES)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def otf_to_ttf(src: Path, dst: Path) -> Path:
    if shutil.which("otf2ttf") is None and not (ROOT / ".venv/bin/otf2ttf").exists():
        sys.exit("缺少 otf2ttf：.venv/bin/pip install otf2ttf")
    exe = shutil.which("otf2ttf") or str(ROOT / ".venv/bin/otf2ttf")
    print(f"· OTF → TTF（约 40 秒，45k 字形）…")
    r = subprocess.run([exe, str(src), "-o", str(dst)], capture_output=True, text=True)
    if r.returncode != 0 or not dst.exists():
        sys.exit(f"转换失败：{r.stderr[-500:]}")
    return dst


def process(src: Path, dst: Path) -> dict:
    from fontTools.ttLib import TTFont

    f = TTFont(str(src))
    stats = {"src_glyphs": len(f.getGlyphOrder())}

    # a) 去掉 glyph 名：post 3.0
    if "post" in f:
        f["post"].formatType = 3.0
        if hasattr(f["post"], "glyphOrder"):
            f["post"].glyphOrder = None
        stats["post_dropped"] = True

    # b) 删掉部首区码位
    removed = 0
    for table in f["cmap"].tables:
        if not table.isUnicode():
            continue
        for cp in [c for c in table.cmap if is_radical_cp(c)]:
            del table.cmap[cp]
            removed += 1
    stats["cmap_radicals_removed"] = removed

    dst.parent.mkdir(parents=True, exist_ok=True)
    f.save(str(dst))
    f.close()

    stats["out_size"] = dst.stat().st_size
    stats["sha256"] = sha256(dst)
    return stats


def verify(path: Path) -> int:
    from fontTools.ttLib import TTFont

    f = TTFont(str(path), lazy=True)
    bad = 0
    post = f["post"].formatType if "post" in f else None
    if post != 3.0:
        print(f"✗ post 表为 {post}，glyph 名还在 → Chromium 会用 uni2EXX 名污染文本层")
        bad += 1
    else:
        print("✓ post 3.0（无 glyph 名）")

    n = sum(1 for t in f["cmap"].tables if t.isUnicode() for cp in t.cmap if is_radical_cp(cp))
    if n:
        print(f"✗ cmap 里还有 {n} 个部首码位")
        bad += 1
    else:
        print("✓ cmap 无 CJK 部首码位")

    cmap = {}
    for t in f["cmap"].tables:
        if t.isUnicode():
            cmap.update(t.cmap)
    sample = "黄二页文用曰非生自人"
    miss = [c for c in sample if ord(c) not in cmap]
    if miss:
        print(f"✗ 常用字缺字：{''.join(miss)}")
        bad += 1
    else:
        print("✓ 常用字覆盖正常")
    print(f"  码位数 {len(cmap)}｜文件 {path.stat().st_size / 1048576:.1f}MB")
    f.close()
    return bad


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--otf", help="官方 .otf 路径（会先转 TTF）")
    ap.add_argument("--ttf", help="已有 .ttf 路径")
    ap.add_argument("--name", help="字体名（如 SourceHanSerifSC）")
    ap.add_argument("--out", help="输出路径，默认 fonts/<name>-Regular.ttf")
    ap.add_argument("--verify", help="只检查某个字体文件是否已加工")
    args = ap.parse_args()

    if args.verify:
        return 1 if verify(Path(args.verify)) else 0

    if not args.name or not (args.otf or args.ttf):
        ap.error("需要 --name 以及 --otf/--ttf")

    src = Path(args.ttf) if args.ttf else Path(args.otf)
    if not src.exists():
        sys.exit(f"找不到 {src}")
    if args.otf:
        tmp = Path(".tmp") / f"{args.name}-raw.ttf"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        src = otf_to_ttf(src, tmp)

    out = Path(args.out) if args.out else FONT_DIR / f"{args.name}-Regular.ttf"
    stats = process(src, out)
    print(f"✓ 已生成 {out.relative_to(ROOT) if out.is_relative_to(ROOT) else out}")
    print(f"  原始字形 {stats['src_glyphs']}｜删除部首码位 {stats['cmap_radicals_removed']}｜"
          f"{stats['out_size'] / 1048576:.1f}MB")
    print(f"  SHA256 {stats['sha256']}")
    print()
    print("下一步：")
    print("  .venv/bin/python tools/check_fonts.py " + str(out))
    print("  .venv/bin/python -m app.cli fonts --register")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
