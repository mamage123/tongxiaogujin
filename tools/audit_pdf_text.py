#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PDF 文本层体检：导出文件「能不能被复制、能不能被搜索」。

为什么单独做这个工具：PDF 看起来对，不代表文本层对。
实测过两种隐蔽劣化：

  1. 康熙部首污染 —— 「黄」被写成 U+2EE9 ⻩、「二」被写成 U+2F02 ⼆。
     字形一模一样，肉眼看不出来，但复制出来搜不到、粘贴到 Word 是怪字。
     成因是字体缺白名单、Chromium 退到系统宋体，而系统宋体的 glyph 名
     沿用了康熙部首的命名，ToUnicode 反查就落在部首码位。
  2. Type3 碎片字体 —— 每个字一张小图（或一小段 charproc），
     体积暴涨、放大发虚、跨设备渲染不一致。正常应只有 TrueType/Type0。

判据（任一不满足即非 0 退出）：
  · 康熙部首字符数 = 0
  · Type3 字体数 = 0
  · 每页至少有一条可提取文本（纯扫描件不会有，但我们的导出都应满足）

用法：
  python tools/audit_pdf_text.py out.pdf
  python tools/audit_pdf_text.py --dir data/files/tasks/*/exports
  python tools/audit_pdf_text.py a.pdf --expect 受漢禪
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    from pypdf import PdfReader
except ImportError:
    sys.exit("缺少依赖：pip install pypdf")

# CJK 部首区：2E80–2EFF 补充部首，2F00–2FDF 康熙部首。
# 这两段里的字符不该出现在正文文本层——正文要的是对应的「汉字」码位。
RADICAL_RANGES = ((0x2E80, 0x2EFF), (0x2F00, 0x2FDF))


def is_radical(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in RADICAL_RANGES)


def is_cjk(ch: str) -> bool:
    cp = ord(ch)
    return 0x3400 <= cp <= 0x4DBF or 0x4E00 <= cp <= 0x9FFF or 0xF900 <= cp <= 0xFAFF


def audit(path: Path, expect: list[str] | None = None) -> dict:
    r = PdfReader(str(path))
    text = "".join((p.extract_text() or "") for p in r.pages)
    flat = "".join(text.split())

    radicals = [c for c in flat if is_radical(c)]
    cjk = sum(1 for c in flat if is_cjk(c))

    fonts: dict[str, int] = {}
    type3_names: list[str] = []
    for page in r.pages:
        res = page.get("/Resources") or {}
        fd = res.get("/Font") or {}
        for name, ref in fd.items():
            fo = ref.get_object()
            st = str(fo.get("/Subtype") or "?")
            fonts[st] = fonts.get(st, 0) + 1
            if st == "/Type3":
                type3_names.append(str(fo.get("/Name") or name))

    empty_pages = sum(1 for p in r.pages if not "".join((p.extract_text() or "").split()))
    missing = [w for w in (expect or []) if w not in flat]

    return {
        "file": path.name,
        "size": path.stat().st_size,
        "pages": len(r.pages),
        "chars": len(flat),
        "cjk": cjk,
        "radicals": radicals,
        "radical_rate": (len(radicals) / cjk) if cjk else 0.0,
        "fonts": fonts,
        "type3": len(type3_names),
        "empty_pages": empty_pages,
        "missing_expected": missing,
        "text": flat,
    }


def verdict(r: dict) -> tuple[bool, list[str]]:
    bad = []
    if r["radicals"]:
        bad.append(f"康熙部首 {len(r['radicals'])} 个：{''.join(r['radicals'][:12])}")
    if r["type3"]:
        bad.append(f"Type3 碎片字体 {r['type3']} 个")
    if r["empty_pages"]:
        bad.append(f"{r['empty_pages']} 页无文本层")
    if r["missing_expected"]:
        bad.append(f"缺期望字：{''.join(r['missing_expected'])}")
    return (not bad), bad


def fmt(r: dict) -> str:
    fonts = " ".join(f"{k.lstrip('/')}×{v}" for k, v in sorted(r["fonts"].items()))
    return (
        f"{r['file'][:34]:<36}{r['pages']:>4}页{r['size'] / 1024:>8.1f}KB"
        f"{r['chars']:>7}字  部首{r['radicals'].__len__():>3}  {fonts or '—'}"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="*", help="PDF 路径")
    ap.add_argument("--dir", action="append", default=[], help="目录（递归找 .pdf）")
    ap.add_argument("--expect", default="", help="必须出现在文本里的字（逐字校验）")
    ap.add_argument("--quiet", action="store_true", help="只输出结论")
    args = ap.parse_args()

    paths = [Path(f) for f in args.files]
    for d in args.dir:
        base = Path(d)
        if base.is_dir():
            paths += sorted(base.rglob("*.pdf"))
        else:
            paths += sorted(p for p in base.parent.glob(base.name) if p.suffix.lower() == ".pdf")
    paths = [p for p in dict.fromkeys(paths) if p.exists()]
    if not paths:
        print("没有找到 PDF。用法：python tools/audit_pdf_text.py 文件.pdf 或 --dir 目录")
        return 2

    expect = list(dict.fromkeys(args.expect)) if args.expect else []
    bad = 0
    print(f"{'文件':<34}{'页数':>6}{'体积':>10}{'字数':>8}  部首/字体")
    print("-" * 96)
    for p in paths:
        try:
            r = audit(p, expect)
        except Exception as e:  # noqa: BLE001
            print(f"{p.name[:34]:<36}  读取失败：{e}")
            bad += 1
            continue
        ok, reasons = verdict(r)
        if not ok:
            bad += 1
        if not args.quiet:
            print(("✓ " if ok else "✗ ") + fmt(r))
        for msg in reasons:
            print(f"      · {msg}")

    print()
    if bad:
        print(f"✗ {bad}/{len(paths)} 个文件文本层不合格")
        print("  康熙部首 → 字体未走白名单，检查 fonts/ 与 app.cli fonts")
        print("  Type3    → Chromium 没拿到可子集化的字体文件，同上")
        return 1
    print(f"✓ {len(paths)} 个文件文本层全部合格（可复制、可搜索、无 Type3 碎片）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
