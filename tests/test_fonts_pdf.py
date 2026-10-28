"""字体与 PDF 文本层：这两条不测就会静默退化。

三段真实退化链（2026-10-27 实测，都在「看起来没问题」的情况下发生）：
  1. fonts/ 为空 → 退到系统 serif → PDF 文本层出现康熙部首（复制搜不到）
  2. 字体是 CID-keyed CFF（官方 OTF）→ Chromium 无法子集化 → 每页几十个 Type3
  3. 标题用伪粗体（只有一个 Regular 字重）→ 标题那几个字变成 Type3 图片字

所以这里不只测「函数返回值」，而是**真渲染一个 PDF 再读回来**。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.export.fonts import _font_format  # noqa: E402


def test_font_format_matches_container():
    """`format()` 提示写错，Chromium 会直接放弃这个 @font-face。

    历史 bug：.ttf 被标成 opentype、.otf 也被标成 opentype，只有 .woff2 猜对。
    因为白名单一直是 OTF，错误标签侥幸正确，所以一直没暴露。
    """
    assert _font_format("a.otf") == "opentype"
    assert _font_format("a.ttf") == "truetype"
    assert _font_format("a.ttc") == "truetype"
    assert _font_format("a.woff2") == "woff2"
    assert _font_format("a.woff") == "woff"


def _shipped_fonts() -> list[Path]:
    d = ROOT / "fonts"
    return sorted(p for p in d.glob("*.ttf")) + sorted(p for p in d.glob("*.otf"))


def test_whitelist_fonts_are_pdf_safe():
    """仓库里的字体文件必须是「已加工」的：无 glyph 名、无康熙部首码位。

    这两条是 PDF 文本层能正确复制的充要条件，详见 tools/build_fonts.py。
    """
    files = _shipped_fonts()
    if not files:
        pytest.skip("fonts/ 为空（尚未放入白名单字体）")

    from fontTools.ttLib import TTFont

    for p in files:
        f = TTFont(str(p), lazy=True)
        try:
            if "CFF " in f:
                pytest.fail(
                    f"{p.name} 是 CID-keyed CFF：Chromium 打印时会退化成每字一个 Type3，"
                    "请用 tools/build_fonts.py 转成 TTF"
                )
            assert f["post"].formatType == 3.0, f"{p.name} 还带 glyph 名（uni2EXX 会污染文本层）"
            n_rad = sum(
                1
                for t in f["cmap"].tables
                if t.isUnicode()
                for cp in t.cmap
                if 0x2E80 <= cp <= 0x2FDF
            )
            assert n_rad == 0, f"{p.name} cmap 里还有 {n_rad} 个康熙部首码位"
        finally:
            f.close()


@pytest.fixture()
def rendered_pdf(env, tmp_path):
    """造一个已结算任务，渲染成横排 PDF，返回 (pdf 路径, 期望文本)。"""
    from app.services.export.fonts import available_body_font

    if available_body_font() is None:
        pytest.skip("没有可用白名单字体，无法验证嵌入质量")
    try:
        from app.services.export.html_render import build_html
        from app.services.export.pdf import html_to_pdf, playwright_available
    except ImportError:  # pragma: no cover
        pytest.skip("导出依赖缺失")
    if not playwright_available():
        pytest.skip("未安装 Playwright")

    from app.services.billing import settle
    from tests.test_export import make_task, make_user

    uid = make_user("fontuser", balance_mill=10_000)
    tid = make_task(uid, chars=3000, status="processing")
    settle(tid)

    html = build_html(tid, lang="trad", layout="horiz", for_pdf=True)
    out = tmp_path / "check.pdf"
    html_to_pdf(html, str(out), width_px=794, height_px=1123)
    return out, "欽定四庫全書"


def test_pdf_text_layer_clean(rendered_pdf):
    """导出的 PDF：无 Type3、无康熙部首、正文可提取。"""
    from pypdf import PdfReader

    pdf, expect = rendered_pdf
    rd = PdfReader(str(pdf))
    text = "".join((p.extract_text() or "") for p in rd.pages)
    flat = "".join(text.split())

    radicals = [c for c in flat if 0x2E80 <= ord(c) <= 0x2FDF]
    assert not radicals, f"文本层混入康熙部首：{''.join(radicals)}（复制出来搜不到）"

    subs = {}
    for page in rd.pages:
        for _, ref in (page.get("/Resources", {}).get("/Font") or {}).items():
            st = str(ref.get_object().get("/Subtype"))
            subs[st] = subs.get(st, 0) + 1
    assert subs.get("/Type3", 0) == 0, f"出现 Type3 碎片字体：{subs}"
    assert subs.get("/Type0", 0) >= 1, f"没有嵌入 CID 字体：{subs}"

    assert expect in flat, f"正文提取异常：{flat[:40]}"


def test_pdf_size_within_redline(rendered_pdf):
    """M-13 体积红线：纯文本横排单页 ≤1MB（嵌入字体后应远小于此）。"""
    pdf, _ = rendered_pdf
    assert pdf.stat().st_size < 1024 * 1024
