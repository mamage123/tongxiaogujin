"""字体白名单、缺字回退、子集化（FR-EXP-07/12/13，M-13 体积红线）。

三条铁律：
1. 只允许白名单 3 个文件：京华老宋体 / 思源宋体 SC / 思源宋体 TC。
2. 显式禁用的字族出现在任何模板或代码里 → 直接报错（CI 也扫）。
3. 缺字逐字回退并记录，**禁止静默用系统字体兜底**。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from ...config import settings
from ...db import q

logger = logging.getLogger(__name__)

# FR-EXP-07 ④：显式禁用（版权归中易/方正等，服务端嵌入 PDF 分发属未授权复制发行）
FORBIDDEN_FAMILIES = [
    "SimSun", "宋体", "NSimSun", "新宋体",
    "Microsoft YaHei", "微软雅黑", "微软雅黑 Light",
    "FangSong", "仿宋", "FangSong_GB2312",
    "SimHei", "黑体",
    "KaiTi", "楷体", "KaiTi_GB2312",
    "PingFang SC", "PingFang TC", "苹方",
    "Songti SC", "Songti TC", "STSong", "华文宋体",
    "STKaiti", "华文楷体", "STHeiti", "华文黑体",
]

# 回退链（FR-EXP-12）：京华 → 思源 SC → 思源 TC
FALLBACK_CHAIN = ["KingHwaOldSong", "SourceHanSerifSC", "SourceHanSerifTC"]

# 渲染时用的 CSS 字族名（与 font_assets.name 一致，加引号防含空格）
CSS_FAMILY = {
    "KingHwaOldSong": "KingHwaOldSong",
    "SourceHanSerifSC": "SourceHanSerifSC",
    "SourceHanSerifTC": "SourceHanSerifTC",
}


class FontPolicyError(Exception):
    pass


@dataclass
class FontAsset:
    name: str
    filename: str
    role: str
    license_grade: str
    sha256: str
    path: Path
    exists: bool = False

    @property
    def css_name(self) -> str:
        return CSS_FAMILY.get(self.name, self.name)


@dataclass
class GlyphFallback:
    """缺字回退结果。"""

    missing: dict[str, list[str]] = field(default_factory=dict)  # font -> [chars]

    @property
    def total(self) -> int:
        return sum(len(v) for v in self.missing.values())

    def to_record(self) -> dict:
        return {k: {"font": k, "chars": v} for k, v in self.missing.items()}


def load_font_assets() -> list[FontAsset]:
    rows = q("SELECT * FROM font_assets WHERE enabled=1 ORDER BY CASE role WHEN 'body' THEN 0 ELSE 1 END")
    out: list[FontAsset] = []
    for r in rows:
        p = settings.font_dir / r["filename"]
        out.append(
            FontAsset(
                name=r["name"],
                filename=r["filename"],
                role=r["role"],
                license_grade=r["license_grade"],
                sha256=r["sha256"],
                path=p,
                exists=p.exists(),
            )
        )
    return out


def check_family_policy(css_text: str) -> None:
    """构建期/运行期扫描：出现禁用字族即报错（FR-EXP-13 ④）。"""
    for fam in FORBIDDEN_FAMILIES:
        # 精确匹配 font-family 值，避免 "宋体" 命中 "宋体风格" 这类注释
        if re.search(rf"font-family\s*:[^;]*{re.escape(fam)}", css_text, re.IGNORECASE):
            raise FontPolicyError(
                f"检测到禁用字体「{fam}」。仅允许白名单：{', '.join(FALLBACK_CHAIN)}。"
                "系统宋体/黑体/楷体版权归中易、方正等，服务端嵌入分发属未授权复制发行。"
            )


def available_body_font() -> str | None:
    """返回第一个可用的正文字体名（文件真实存在才算）。

    字体文件缺失时返回 None，上层降级为「不嵌入字体」（浏览器默认），
    但必须在导出日志里记 `font_missing`，不能静默假装成功。
    """
    for a in load_font_assets():
        if a.exists and a.role in ("body", "fallback"):
            return a.name
    return None


def _font_format(filename: str) -> str:
    """CSS `format()` 提示必须与真实容器格式一致，写错 Chromium 会直接放弃该 @font-face。

    历史 bug：原先 .ttf 被标成 opentype、.otf 也被标成 opentype，只有 .woff2 猜对了；
    一旦白名单字体是 OTF（思源宋体就是 OTF），标签侥幸正确，看似能跑，
    所以这个错一直没暴露。
    """
    suffix = Path(filename).suffix.lower()
    if suffix == ".otf":
        return "opentype"
    if suffix in (".ttf", ".ttc", ".otc"):
        return "truetype"
    if suffix == ".woff2":
        return "woff2"
    if suffix == ".woff":
        return "woff"
    return "truetype"


def _is_cid_cff(path: Path) -> bool:
    """是否 CID-keyed CFF（.otf 里最常见的那种中文字体）。

    Chromium/Skia 打印 PDF 时**无法子集化 CID-keyed CFF**，会退化成
    「每个字一个 Type3 字体」：体积虚高、放大发虚、跨设备渲染不一致。
    换成等价的 TrueType(glyf) 文件就正常嵌入为 Type0。实测对比：
        OTF（Source Han Serif） → Type3×16，37KB
        同字体转 TTF            → Type0×1， 8KB
    所以这里只提醒、不阻断——观感能接受，但要知道代价。
    """
    if path.suffix.lower() != ".otf":
        return False
    try:
        from fontTools.ttLib import TTFont

        f = TTFont(str(path), lazy=True)
        try:
            return "CFF " in f or "CFF2" in f
        finally:
            f.close()
    except Exception:  # noqa: BLE001
        return False


def font_face_css() -> str:
    """生成 @font-face（供 HTML 预览与 PDF 共用）。文件缺失则跳过该字体。"""
    parts = []
    for a in load_font_assets():
        if not a.exists:
            continue
        uri = a.path.resolve().as_uri()
        if _is_cid_cff(a.path):
            logger.warning(
                "font_cff: %s 是 CID-keyed CFF，Chromium 打印时无法子集化，"
                "PDF 会退化成每字一个 Type3 字体。请改用同款 TTF（otf2ttf 转换即可）。",
                a.filename,
            )
        parts.append(
            f"@font-face{{font-family:'{a.css_name}';src:url('{uri}') format('{_font_format(a.filename)}');"
            f"font-display:swap;}}"
        )
    return "\n".join(parts)


def css_font_stack() -> str:
    """CSS font-family 值：白名单优先，最后兜到 serif（不给系统字体具名，避免侵权指引）。"""
    names = [a.css_name for a in load_font_assets() if a.exists]
    names = list(dict.fromkeys(names))
    if not names:
        # 铁律 3：不许静默兜底。落到裸 serif 后 PDF 文本层会退化成康熙部首码位
        # （复制出来搜不到），必须在日志里喊出来，否则没人知道质量已经塌了。
        logger.warning(
            "font_missing: fonts/ 目录没有任何白名单字体，导出将退化为系统 serif。"
            "PDF 文本层的“可复制可搜索”承诺会失效（详见 tools/audit_pdf_text.py）。"
        )
        return "serif"
    return ", ".join(f"'{n}'" for n in names) + ", serif"


# ---------------------------------------------------------------- 缺字检测
_cmap_cache: dict[str, set[int]] = {}


def _font_cmap(path: Path) -> set[int]:
    key = str(path)
    if key in _cmap_cache:
        return _cmap_cache[key]
    try:
        from fontTools.ttLib import TTFont, TTCollection

        if path.suffix.lower() in (".ttc", ".otc"):
            coll = TTCollection(str(path))
            font = coll.fonts[0]
        else:
            font = TTFont(str(path), fontNumber=0, lazy=True)
        cps = set()
        for table in font["cmap"].tables:
            cps |= set(table.cmap.keys())
        _cmap_cache[key] = cps
        return cps
    except Exception:  # noqa: BLE001
        _cmap_cache[key] = set()
        return set()


def find_missing(text: str, font_name: str = "KingHwaOldSong") -> list[str]:
    """逐字查 cmap，返回该字体缺失的字符（去重、保序）。"""
    assets = {a.name: a for a in load_font_assets()}
    a = assets.get(font_name)
    if not a or not a.exists:
        return []
    cps = _font_cmap(a.path)
    if not cps:
        return []
    missing = []
    seen = set()
    for ch in text:
        if ch.isspace() or ch in seen:
            continue
        seen.add(ch)
        if ord(ch) not in cps:
            missing.append(ch)
    return missing


def resolve_fallback(text: str, primary: str = "KingHwaOldSong") -> tuple[str, GlyphFallback]:
    """确定每个缺字用哪款字体渲染，并返回记录。

    返回 (主字体名, GlyphFallback)。调用方把记录写进 export_logs.missing_glyphs。
    """
    fb = GlyphFallback()
    assets = {a.name: a for a in load_font_assets()}

    # 主字体不存在 → 直接用第一个可用字体，并记录
    if primary not in assets or not assets[primary].exists:
        avail = [n for n in FALLBACK_CHAIN if n in assets and assets[n].exists]
        if not avail:
            return primary, fb
        fb.missing[primary] = ["*font_file_missing*"]
        primary = avail[0]

    missing = find_missing(text, primary)
    if not missing:
        return primary, fb

    # 逐级回退：把主字体缺的字，分给下一款能覆盖的字体
    remaining = list(missing)
    for name in FALLBACK_CHAIN:
        if name == primary or not remaining:
            continue
        a = assets.get(name)
        if not a or not a.exists:
            continue
        cps = _font_cmap(a.path)
        still = []
        covered = []
        for ch in remaining:
            if ord(ch) in cps:
                covered.append(ch)
            else:
                still.append(ch)
        if covered:
            fb.missing[name] = covered
        remaining = still

    if remaining:
        # 全链都缺：记录但继续（浏览器会落到 serif），并告警
        fb.missing["__unresolved__"] = remaining
    return primary, fb


def subset_command(src: Path, dst: Path, chars: str) -> list[str]:
    """构造 pyftsubset 命令（导出时按实际用字裁字体，M-13 体积红线）。"""
    return [
        "pyftsubset",
        str(src),
        f"--text={chars}",
        f"--output-file={dst}",
        "--layout-features=*",
        "--flavor=woff2",
        "--no-hinting",
        "--desubroutinize",
    ]


def size_mill_is_ok(size_bytes: int, kind: str, pages: int = 1) -> bool:
    """M-13 体积红线分档：纯文本 1MB / 竖排 1.5MB / 校对版 250KB 每页。"""
    mb = 1024 * 1024
    if kind in ("markdown", "horiz_docx"):
        return size_bytes <= 1 * mb
    if kind.startswith("vert"):
        return size_bytes <= int(1.5 * mb)
    if kind == "proof_pdf":
        return size_bytes <= int(0.25 * mb) * max(1, pages)
    if kind == "horiz_pdf":
        return size_bytes <= 1 * mb
    return True
