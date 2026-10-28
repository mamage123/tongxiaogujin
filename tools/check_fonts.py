#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
字体准入体检：判断一款中文字体能否用于「竖排 PDF 导出」。

检查 6 项：
  1. 授权与版本信息（name 表：家族名、版本、版权、授权描述、授权链接）
  2. fsType 嵌入许可（OS/2 表）—— 决定「能不能合法嵌进 PDF」
  3. 简体覆盖（GB2312 一二级汉字 6763 字）
  4. 繁体覆盖（Big5 常用 + 次常用）
  5. 古籍异体字 / 生僻字样本（繁简转换易丢字的那批）
  6. 竖排能力：GSUB 的 vert / vrt2 特性是否存在，vmtx / vhea 表是否存在

用法：
  python3 check_fonts.py 字体1.ttf 字体2.otf 字体3.ttc
  python3 check_fonts.py ~/Downloads/fonts/*.ttf --json out.json

依赖：fonttools  (pip install fonttools)
"""

import sys
import os
import json
import argparse

try:
    from fontTools.ttLib import TTFont, TTCollection
except ImportError:
    sys.exit("缺少依赖，请先执行：pip install fonttools")

# ---------------------------------------------------------------- 字表构造

def gb2312_chars():
    """GB2312 一二级汉字（约 6763 字），由编码区间解码生成，不依赖外部字表。"""
    out = []
    for hi in range(0xB0, 0xF8):
        for lo in range(0xA1, 0xFF):
            try:
                out.append(bytes([hi, lo]).decode("gb2312"))
            except UnicodeDecodeError:
                pass
    return "".join(out)


def big5_chars():
    """Big5 常用字（A440-C67E）+ 次常用字（C940-F9D5）。"""
    out = []
    for start, end in ((0xA440, 0xC67E), (0xC940, 0xF9D5)):
        for hi in range(start >> 8, (end >> 8) + 1):
            for lo in range(0x40, 0xFF):
                try:
                    out.append(bytes([hi, lo]).decode("big5"))
                except UnicodeDecodeError:
                    pass
    return "".join(out)


# 古籍场景最容易缺字的一批：异体字、繁简一对多、年号专名、生僻字
RARE_SAMPLE = (
    # 异体字（正字→异体）
    "敎旣槩峯甯喆堃邨竝羣囘爲說讀龢迺廼尙敍敘眞眞箇粧敎"
    # 繁简一对多（转换必错的那批）
    "乾干幹後后徵征於于雲云餘余穀谷髮发鬥斗儘尽"
    # 年号 / 专名常用
    "乾隆嘉慶光緒康熙雍正萬曆崇禎洪武永樂"
    # 古籍常见但简体字库常缺
    "曰兮矣焉哉歟耶邪夫蓋故若乃遂迺"
    # 生僻 / 扩展区
    "龘靐齉爨鬱籲麤龖𠀀㐀㐁"
)


# ---------------------------------------------------------------- 检查逻辑

FSTYPE_MAP = {
    0: ("可安装嵌入", "允许嵌入，无限制", True),
    1: ("保留", "保留值，视为不可嵌入", False),
    2: ("受限授权", "禁止嵌入（仅允许在授权系统内使用）", False),
    4: ("预览与打印", "允许只读嵌入（PDF 打印场景可用）", True),
    8: ("可编辑", "允许可编辑嵌入", True),
}


def pick_name(name_table, name_id):
    for plat, enc, lang in ((3, 1, 0x409), (3, 1, 0x804), (1, 0, 0)):
        rec = name_table.getDebugName(name_id)
        if rec:
            return rec
    return ""


def check_one(path, index=0):
    font = TTFont(path, fontNumber=index, lazy=True)
    info = {"file": os.path.basename(path), "index": index}

    # 1) 元信息
    nt = font["name"]
    info["family"] = pick_name(nt, 1) or pick_name(nt, 16) or "(未命名)"
    info["version"] = pick_name(nt, 5)
    info["copyright"] = pick_name(nt, 0)[:120]
    info["license"] = pick_name(nt, 13)[:200]
    info["license_url"] = pick_name(nt, 14)[:160]

    # 2) 嵌入许可
    fs = font["OS/2"].fsType if "OS/2" in font else None
    if fs is None:
        info["fsType"] = None
        info["fsType_label"] = "无 OS/2 表"
        info["embed_ok"] = False
    else:
        bits = fs & 0x000F
        if bits == 0 and fs & 0x0100:
            label, desc, ok = ("无子集化许可", "不允许子集嵌入，体积会很大", False)
        elif bits == 0 and fs & 0x0200:
            label, desc, ok = ("仅位图嵌入", "只能嵌点阵，PDF 会失真", False)
        else:
            label, desc, ok = FSTYPE_MAP.get(bits, (f"未知({bits})", "需人工确认", False))
        info["fsType"] = fs
        info["fsType_label"] = label
        info["fsType_desc"] = desc
        info["embed_ok"] = ok

    # 3~5) 字符覆盖
    cmap = set()
    for table in font["cmap"].tables:
        if table.isUnicode():
            cmap |= set(table.cmap.keys())
    info["glyph_count"] = len(cmap)

    def cover(sample, name):
        missing = [c for c in dict.fromkeys(sample) if ord(c) not in cmap]
        total = len(dict.fromkeys(sample))
        info[name] = {
            "total": total,
            "missing": len(missing),
            "rate": round(100.0 * (total - len(missing)) / total, 2) if total else 0.0,
            "missing_sample": "".join(missing[:30]),
        }

    cover(gb2312_chars(), "gb2312")
    cover(big5_chars(), "big5")
    cover(RARE_SAMPLE, "rare")

    # 6) 竖排能力
    feats = set()
    if "GSUB" in font:
        try:
            feats = {fr.FeatureTag for fr in font["GSUB"].table.FeatureList.FeatureRecord}
        except Exception:
            feats = set()
    info["has_vert"] = "vert" in feats
    info["has_vrt2"] = "vrt2" in feats
    info["has_vmtx"] = "vmtx" in font
    info["has_vhea"] = "vhea" in font
    info["has_palt"] = "palt" in feats   # 比例宽度，竖排会用到
    # 竖排能力分级：
    #   A = 有 vert/vrt2 且有 vmtx/vhea  → 标点自动落位、字距正常，可直接做竖排正文
    #   B = 有 vmtx 无 vert            → 字距正常，但句读/引号需 CSS 或替换表兜底
    #   C = 两者皆无                    → 不建议用于竖排正文，只做标题/牌记装饰
    vert_feat = info["has_vert"] or info["has_vrt2"]
    vert_metric = info["has_vmtx"] or info["has_vhea"]
    if vert_feat and vert_metric:
        info["vertical_grade"] = "A"
    elif vert_metric:
        info["vertical_grade"] = "B"
    else:
        info["vertical_grade"] = "C"
    info["vertical_ok"] = info["vertical_grade"] in ("A", "B")

    font.close()
    return info


def fmt_table(results):
    lines = []
    hdr = (f"{'字体':<22}{'字库':>7}{'GB2312':>9}{'Big5':>8}{'异体':>7}"
           f"{'vert':>6}{'vmtx':>6}{'fsType':>8}{'竖排':>6}")
    lines.append(hdr)
    lines.append("-" * 84)
    for r in results:
        if "error" in r:
            lines.append(f"{r['file'][:20]:<22}  读取失败：{r['error']}")
            continue
        v = lambda b: "有" if b else "—"
        lines.append(
            f"{r['family'][:20]:<22}{r['glyph_count']:>7}"
            f"{r['gb2312']['rate']:>8.1f}%{r['big5']['rate']:>7.1f}%{r['rare']['rate']:>6.1f}%"
            f"{v(r['has_vert']):>6}{v(r['has_vmtx']):>6}{str(r['fsType']):>8}"
            f"{r['vertical_grade']:>6}"
        )
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("fonts", nargs="+", help="字体文件路径，支持 ttf / otf / ttc")
    ap.add_argument("--json", help="同时输出 JSON 报告到指定路径")
    args = ap.parse_args()

    results = []
    for p in args.fonts:
        if not os.path.exists(p):
            results.append({"file": os.path.basename(p), "error": "文件不存在"})
            continue
        try:
            if p.lower().endswith((".ttc", ".otc")):
                coll = TTCollection(p)
                for i in range(len(coll.fonts)):
                    coll.close()
                    results.append(check_one(p, i))
            else:
                results.append(check_one(p, 0))
        except Exception as e:
            results.append({"file": os.path.basename(p), "error": str(e)})

    print(fmt_table(results))

    print("\n判定口径")
    print("  GB2312 ≥99%  简体正文可用")
    print("  Big5   ≥98%  繁体原字（D15 出口）可用")
    print("  异体   ≥90%  古籍异体字、繁简一对多、年号专名不易缺字")
    print("  fsType 必须为 0/4/8 才允许嵌入 PDF；为 2（受限授权）时禁止嵌入")
    print("  竖排 A=有 vert+vmtx，可直接做竖排正文")
    print("        B=有 vmtx 无 vert，字距正常但句读/引号需 CSS 兜底")
    print("        C=两者皆无，只做标题/牌记装饰，不进竖排正文")

    bad = [r for r in results if "error" not in r and r["vertical_grade"] == "C"]
    if bad:
        print("\n不可用于竖排正文（C 级）：" + "、".join(r["family"] for r in bad))
    noembed = [r for r in results if "error" not in r and not r["embed_ok"]]
    if noembed:
        print("禁止嵌入 PDF（fsType 受限）：" + "、".join(r["family"] for r in noembed))

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        print(f"\nJSON 报告已写入 {args.json}")


if __name__ == "__main__":
    main()
