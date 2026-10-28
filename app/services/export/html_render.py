"""HTML 渲染 —— 预览与 PDF 的**唯一**来源（FR-EXP-06）。

关键决定：预览（浏览器）与竖排/校对版 PDF（Chromium 打印）用同一份 HTML + 同一份 CSS。
不做第二套渲染，「下载的和看到的不一样」是这类产品最难解释的客诉。

三种布局（同一模板，切 class）：
  .layout-horiz  横排阅读（默认）
  .layout-vert   竖排阅读（writing-mode: vertical-rl）
  .layout-proof  校对版（左原图带框 + 右文字带序号）
"""
from __future__ import annotations

import html
from pathlib import Path

from ...config import BASE_DIR, settings
from ...db import get_setting, q
from ...utils import fix_radicals
from ..ocr.normalize import to_simplified
from .fonts import css_font_stack, font_face_css

AI_LABEL = "本文件由 AI 识别生成，供参考。"
SIMPLIFIED_NOTE = "简体由模型自动转换，人名、地名、年号等专名可能存在歧义，引用请核对繁体原件。"

# 竖排标点（FR-EXP-02 ⑤）：CSS 决定字框位置，但有些标点需换成竖排专用字符
VERT_PUNCT_MAP = {
    "，": "︐", "、": "︑", "。": "︒", "：": "︓", "；": "︔",
    "！": "︕", "？": "︖",
}


def _esc(s: str) -> str:
    return html.escape(s or "", quote=True)


def _blocks(task_id: str, page_no: int) -> list[dict]:
    return q(
        "SELECT * FROM page_blocks WHERE task_id=:t AND page_no=:p ORDER BY block_order",
        t=task_id, p=page_no,
    )


def _doc_title(task: dict) -> str:
    """文件名去扩展名当文档标题；校对版/正文标题都用它，避免两处口径不一。"""
    return ((task or {}).get("filename") or "").rsplit(".", 1)[0].strip() or "识别结果"


def _pages(task_id: str) -> list[int]:
    rows = q(
        "SELECT page_no FROM task_pages WHERE task_id=:t AND status IN ('done','empty') ORDER BY page_no",
        t=task_id,
    )
    return [int(r["page_no"]) for r in rows]


def _text_for(block: dict, task: dict, lang: str) -> str:
    text = (block["text"] or "").strip()
    # 历史数据兜底：早期入库的文本可能带康熙部首（⻩、⼆），那批数据不重跑
    # OCR 就还在库里，下载时必须修掉，否则用户复制出来是搜不到的怪字。
    text = fix_radicals(text)
    # 繁简只看 lang，不看 mode。
    # 之前写成 `lang == "simp" and task["mode"] == "ancient"`，导致：
    # 把繁体古籍选成「现代横排」的用户点「简体」下载，拿到的仍是繁体原文——
    # 而 lang 是用户在导出按钮上的明确选择，没有任何理由被识别模式否决。
    if lang == "simp":
        text = to_simplified(text)
    return text


BASE_CSS = r"""
* { box-sizing: border-box; }
body { margin: 0; background: #f6f4ef; color: #1f1c17;
       font-family: %FONT%; font-size: 15px; line-height: 1.9; }
.page { background: #fff; margin: 0 auto 18px; padding: 28px 30px;
        box-shadow: 0 1px 4px rgba(0,0,0,.10); max-width: 820px; }
.pnum { font-size: 11px; color: #8b8271; text-align: center; margin: 6px 0 18px; }
p { margin: 0 0 0; text-indent: 2em; }
p + p { margin-top: 0; }
/* 标题一律不用粗体：白名单只放 Regular，浏览器会「伪粗体」（把字描粗一圈），
   而伪粗体在 PDF 里必然退化成 Type3 字体——标题那几个字就成了图片，
   复制不到、放大会糊。层级改由字号与字距承担，这也更接近古籍排印习惯。 */
h1, h2, h3, h4, h5, h6, b, strong, th { font-weight: 400; }
h1.doc { font-size: 24px; text-align: center; margin: 0 0 22px; text-indent: 0;
         letter-spacing: .06em; }
h3.bt { font-size: 18px; text-align: center; margin: 14px 0 10px; text-indent: 0; }
p.bh { text-align: center; text-indent: 0; font-size: 13px; color: #6b6355; }
p.empty { text-align: center; text-indent: 0; color: #a39a88; }

/* ---- 横排 ---- */
.layout-horiz .page { max-width: 820px; }
.layout-horiz p { text-indent: 2em; }

/* ---- 竖排（L1 竖排阅读版）---- */
.layout-vert { background: #efe9dc; }
.layout-vert .page {
  position: relative;
  writing-mode: vertical-rl; text-orientation: upright;
  max-width: none; width: 760px; height: 1120px;
  padding: 34px 30px; font-size: 15px; line-height: 2.0;
  column-count: 1; border: 0;
}
.layout-vert h1.doc { font-size: 22px; margin: 0 0 0 14px; letter-spacing: .1em; }
.layout-vert h3.bt { margin: 0 0 0 12px; font-size: 17px; }
.layout-vert p { text-indent: 1em; margin: 0 0 0 6px; }
.layout-vert .num { text-combine-upright: all; }   /* 阿拉伯数字整体旋转，禁止逐个正立 */
/* 注：公式不走 CSS。竖排里没有可用的混排列法（行内横排块会把正文切成碎列，
   整段横排又会挤死标题），所以竖排是在服务端把 LaTeX 降级成 λ、π、x₁ 这类
   普通字符（见 tex_to_plain），横排/校对版则原样保留，方便用户复制 LaTeX。 */
/* 页码必须脱离竖排流，否则会挤在版心右上角与正文抢位置 */
.layout-vert .pnum {
  writing-mode: horizontal-tb; position: absolute;
  left: 0; right: 0; bottom: 12px; margin: 0;
}

/* ---- 校对版 ---- */
.layout-proof .page { max-width: none; width: 1100px; }
.proof-head { font-size: 17px; text-align: center; letter-spacing: .06em; margin: 0 0 6px; }
.proof-row { display: grid; grid-template-columns: 1fr 1fr; gap: 22px; align-items: start; }
.proof-img { position: relative; border: 1px solid #ddd6c6; background: #faf8f2; }
.proof-img img { display: block; width: 100%; height: auto; }
.box { position: absolute; border: 1.5px solid rgba(37,99,235,.85);
       background: rgba(37,99,235,.07); border-radius: 2px; }
.box.t-title { border-color: rgba(194,65,12,.9); background: rgba(194,65,12,.07); }
.box.t-table { border-color: rgba(21,128,61,.9); background: rgba(21,128,61,.07); }
.box.t-formula { border-color: rgba(126,34,206,.9); background: rgba(126,34,206,.07); }
.box.t-figure { border-color: rgba(120,113,108,.9); background: rgba(120,113,108,.07); }
.box.active { outline: 2px solid #dc2626; }
.proof-txt .row { display: flex; gap: 8px; margin-bottom: 4px; line-height: 1.75; }
.proof-txt .no { flex: 0 0 26px; color: #8b8271; font-size: 12px; text-align: right;
                 padding-top: 3px; text-indent: 0; }
.proof-txt .tx { flex: 1; text-indent: 0; }
.proof-txt .row.active .tx { background: #fff3c4; }

footer.doc-foot { max-width: 820px; margin: 0 auto 30px; padding: 10px 4px;
                  font-size: 11px; color: #8b8271; line-height: 1.7; text-align: center; }

/* ---- 打印（PDF）----
   两条硬约束，缺一条都会毁掉成品：
   1) 一个源页 = 一张纸。不写 break-after 的话，第二页会从第一张纸的底部接着排，
      被腰斩成"上半页 + 下半页"两张纸。
   2) 校对版必须整页放进一张横向 A4。横向 A4 的可印区是 1122×793 CSS px，
      而竖排原图（1240×1754）按列宽缩放后高 717px，加上标题/页码就 852px ——
      超了 59px，Chromium 不会缩放，而是把整张图推到下一张纸，正文全被挤走。
      所以这里按纸面给死高度，并让图按 max-height 自适应。 */
@media print {
  @page { margin: 0; }
  body { background: #fff; }
  /* 竖排是"宣纸"观感：整张纸统一底色，版心不再是一个浮在色块上的白框。
     注意 `.layout-vert` 的优先级高于 `body`，只写 body{background:#fff} 是压不住的。 */
  body.layout-vert { background: #efe9dc; }
  .layout-vert .page { background: transparent; }
  .page { box-shadow: none; margin: 0; break-inside: avoid; page-break-inside: avoid; }
  .page:not(:last-of-type) { break-after: page; page-break-after: always; }

  /* AI 标识：position:fixed 在 Chromium 打印时会重复到每一张纸上，
     既满足"每页标注"，又不会因为页脚单独占一张纸而留下一张空白末页。 */
  footer.doc-foot {
    position: fixed; left: 0; right: 0; bottom: 5px; margin: 0;
    padding: 0 14px; font-size: 8.5px; line-height: 1.45; text-align: center;
    max-width: none;
  }

  .layout-proof .page { width: 1122px; height: 752px; padding: 16px 24px; overflow: hidden; }
  .layout-proof .proof-row { height: calc(100% - 46px); }
  .layout-proof .proof-img { width: fit-content; max-width: 100%; margin: 0 auto; }
  .layout-proof .proof-img img { max-height: 672px; width: auto; max-width: 100%; }
  .layout-proof .proof-txt { overflow: hidden; }

  /* 横排正文是流式排版，给最后一张纸留出页脚的高度 */
  .layout-horiz .page { padding-bottom: 46px; }
}
"""


def build_html(
    task_id: str,
    lang: str = "simp",
    layout: str = "horiz",
    with_boxes: bool = False,
    for_pdf: bool = False,
    preview_url_prefix: str | None = None,
) -> str:
    """生成完整 HTML 文档字符串。preview/pdf/proof 共用此函数。"""
    task_rows = q("SELECT * FROM tasks WHERE id=:id", id=task_id)
    if not task_rows:
        raise ValueError("任务不存在")
    task = task_rows[0]

    title = _doc_title(task)
    cfg = get_setting("ai_label", AI_LABEL)

    css = BASE_CSS.replace("%FONT%", css_font_stack())
    pages_html: list[str] = []

    for pn in _pages(task_id):
        blocks = _blocks(task_id, pn)
        if layout == "proof" and with_boxes:
            pages_html.append(_proof_page(task, pn, blocks, lang, for_pdf=for_pdf))
            continue

        inner: list[str] = []
        if not blocks:
            inner.append('<p class="empty">未识别到文字</p>')
        for b in blocks:
            text = _text_for(b, task, lang)
            if not text:
                continue
            # 竖排必须先把公式降级：标题/页眉/正文都要过，不能只处理正文段落
            if layout == "vert":
                text = tex_to_plain(text)
            label = b["block_label"] or "text"
            if label == "title":
                inner.append(f'<h3 class="bt">{_esc(text)}</h3>')
            elif label == "header":
                inner.append(f'<p class="bh">{_esc(text)}</p>')
            else:
                for chunk in [c for c in text.split("\n\n") if c.strip()]:
                    inner.append(f"<p>{_wrap_numbers(_esc(chunk))}</p>")
        pnum = f'<div class="pnum">第 {pn} 页</div>'
        # 竖排版面高度写死（1120px），字多了一定会溢出到版心外；
        # 打上 data-fit 让打印前自动缩字号，宁可字小也不能让字出框。
        fit = ' data-fit="1"' if (for_pdf and layout == "vert") else ""
        pages_html.append(f'<section class="page"{fit}>{pnum}{"".join(inner)}</section>')

    head_title = _esc(title)
    doc_h1 = f'<h1 class="doc">{head_title}</h1>' if layout != "proof" else ""

    foot_parts = [cfg]
    if lang == "simp":
        foot_parts.append(SIMPLIFIED_NOTE)
    if task["mode"] == "ancient":
        foot_parts.append("竖排按右起直行排印；如行序异常可在站内提交反馈。")
    footer = '<footer class="doc-foot">' + "　·　".join(_esc(x) for x in foot_parts) + "</footer>"

    face = font_face_css()
    return f"""<!DOCTYPE html>
<html lang="zh-Hans">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{head_title}</title>
<style>
{face}
{css}
</style>
</head>
<body class="layout-{layout}">
{doc_h1}
{doc_h1 and '<div style="height:8px"></div>'}
{''.join(pages_html)}
{footer}
</body>
</html>"""


def _wrap_numbers(escaped: str) -> str:
    """把阿拉伯数字包进 .num，让竖排里整体旋转而不是逐个正立。"""
    import re

    return re.sub(r"(\d+(?:[.,]\d+)*%?)", r'<span class="num">\1</span>', escaped)


_TEX_GREEK = {
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε", "varepsilon": "ε",
    "zeta": "ζ", "eta": "η", "theta": "θ", "iota": "ι", "kappa": "κ", "lambda": "λ", "mu": "μ",
    "nu": "ν", "xi": "ξ", "pi": "π", "rho": "ρ", "sigma": "σ", "tau": "τ", "upsilon": "υ",
    "phi": "φ", "varphi": "φ", "chi": "χ", "psi": "ψ", "omega": "ω",
    "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ", "Pi": "Π",
    "Sigma": "Σ", "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω",
}
_TEX_SYMBOL = {
    "times": "×", "cdot": "·", "pm": "±", "mp": "∓", "le": "≤", "leq": "≤", "ge": "≥",
    "geq": "≥", "ne": "≠", "neq": "≠", "approx": "≈", "equiv": "≡", "infty": "∞",
    "partial": "∂", "nabla": "∇", "to": "→", "rightarrow": "→", "leftarrow": "←",
    "sqrt": "√", "sum": "Σ", "prod": "Π", "int": "∫", "angle": "∠", "ldots": "…",
    "dots": "…", "quad": " ", "qquad": "  ",
}
_SUB_DIGITS = "₀₁₂₃₄₅₆₇₈₉"
_SUP_DIGITS = "⁰¹²³⁴⁵⁶⁷⁸⁹"


def _tex_inline(expr: str) -> str:
    """把一段 LaTeX 降级成普通字符（λ、π、x₁、²、a/b）。"""
    import re as _re

    s = expr
    s = _re.sub(r"\\(?:left|right|big|Big|bigg|Bigg)\b", "", s)
    s = _re.sub(r"\\[dt]?frac\{([^{}]*)\}\{([^{}]*)\}", r"(\1)/(\2)", s)
    s = _re.sub(r"\\(?:mathrm|mathbf|mathit|text|textrm|operatorname)\{([^{}]*)\}", r"\1", s)
    s = _re.sub(r"\\[,;:! ]", " ", s)

    def cmd(m: "re.Match[str]") -> str:
        name = m.group(1)
        if name in _TEX_GREEK:
            return _TEX_GREEK[name]
        if name in _TEX_SYMBOL:
            return _TEX_SYMBOL[name]
        return name

    s = _re.sub(r"\\([A-Za-z]+)", cmd, s)

    # 上下标：短内容直接转 Unicode（x_1 → x₁、10^{-3} → 10⁻³），长内容退化成 _()
    def sub_sup(m: "re.Match[str]") -> str:
        is_sub = m.group(0)[0] == "_"
        body = m.group(1)
        digits = _SUB_DIGITS if is_sub else _SUP_DIGITS
        out = []
        for ch in body:
            if ch.isdigit():
                out.append(digits[int(ch)])
            elif ch == "-":
                out.append("₋" if is_sub else "⁻")
            elif ch == "+":
                out.append("₊" if is_sub else "⁺")
            else:
                return ("_" if is_sub else "^") + (body if len(body) <= 3 else f"({body})")
        return "".join(out)

    s = _re.sub(r"[_^]\{([+-]?\d{1,3})\}", sub_sup, s)
    s = _re.sub(r"([_^])\{([A-Za-z0-9]{1,3})\}", r"\1\2", s)
    s = _re.sub(r"([_^])\{([^{}]*)\}", r"\1(\2)", s)
    s = s.replace("\\", "")
    return s


_TEX_GROUP = None


def tex_to_plain(text: str) -> str:
    """竖排专用：把 $...$ / $$...$$ 公式降级成普通字符。

    竖排下没法把公式横着嵌进竖排行（writing-mode 混排没有可用的折中：
    行内横排块会把每个公式顶成一列、正文被切碎；整段改横排又会把标题挤成单字长条）。
    真实古籍里的算题本来也是"字就是字"，所以这里转成 λ、π、x₁、² 这类普通字符，
    竖排时整行连续不断，读得下去。代价是复杂分式会丢一些版式，但不会丢内容。
    """
    global _TEX_GROUP
    if _TEX_GROUP is None:
        import re as _re

        _TEX_GROUP = _re.compile(r"\$\$([^$]{1,600})\$\$|\$([^$]{1,300})\$")

    def repl(m: "re.Match[str]") -> str:
        inner = m.group(1) if m.group(1) is not None else (m.group(2) or "")
        return _tex_inline(inner)

    return _TEX_GROUP.sub(repl, text or "")


def _print_image(src: Path) -> Path:
    """校对版打印用图：转成 JPEG 缓存，把 PDF 体积压到 M-13 红线以内。

    Chromium 打印 PNG 是原样内嵌（不做有损压缩），一页 954×1346 的扫描页
    PNG 就 240KB，校对版单页直接顶到 250KB/页的红线上。转 JPEG q88 后
    约 160KB，字迹在 100% 缩放下无可辨差异——校对看的是「哪个框对应哪段字」，
    不是印刷级原图。
    """
    if src.suffix.lower() in (".jpg", ".jpeg"):
        return src
    cache = src.with_name(f"{src.stem}.print88.jpg")
    try:
        if cache.exists() and cache.stat().st_mtime >= src.stat().st_mtime:
            return cache
        from PIL import Image

        im = Image.open(src)
        im = im.convert("RGB")
        im.save(cache, "JPEG", quality=88, optimize=True, progressive=True)
        return cache
    except Exception:  # noqa: BLE001 - 转不了就用原图，宁可大也不能出错
        return src


def _proof_page(task: dict, page_no: int, blocks: list[dict], lang: str, for_pdf: bool = False) -> str:
    """校对版单页：左原图带框，右文字带序号。框坐标按原图尺寸等比（百分比）。

    for_pdf=True 时**不能**用 loading="lazy"：打印时视口外的懒加载图片可能还没请求，
    生成的校对版 PDF 会出现整页空白图。宁可慢一点，也不能交付空图。
    """
    row = q(
        "SELECT img_width,img_height,image_path FROM task_pages WHERE task_id=:t AND page_no=:p",
        t=task["id"], p=page_no,
    )
    w = int(row[0]["img_width"]) if row else 0
    h = int(row[0]["img_height"]) if row else 0

    # 图片地址必须分两条路径：
    # - 浏览器预览：走 HTTP 路由，页面本身就在站点上
    # - PDF 打印：Chromium 加载的是 file:///.../xxx.render.html，
    #   相对路径 /api/... 会被解析成 file:///api/... → 404 → 校对版 PDF 里
    #   原图变成破图 + alt 文本，框层还被压成一排（真实缺陷，2026-10-27 发现）。
    #   所以打印时必须给 file:// 绝对路径。
    img_src = f"/api/tasks/{task['id']}/pages/{page_no}/image"
    if for_pdf:
        raw_path = (row[0]["image_path"] if row else "") or ""
        p_img = Path(raw_path) if raw_path else None
        if p_img is not None and not p_img.is_absolute():
            p_img = BASE_DIR / raw_path
        if p_img is None or not p_img.exists():
            # 宁可不交付，也不交付一份「左边全是破图」的校对版
            raise ValueError(
                f"第 {page_no} 页原图不存在（{raw_path or '未登记'}），无法生成校对版"
            )
        # PNG 原样内嵌会把体积顶到 M-13 红线之外，打印图统一走 JPEG 缓存
        img_src = _print_image(p_img).resolve().as_uri()

    boxes = []
    texts = []
    for i, b in enumerate(blocks):
        text = _text_for(b, task, lang)
        if not text:
            continue
        label = b["block_label"] or "text"
        seq = i + 1
        cls = {"title": "t-title", "table": "t-table", "formula": "t-formula",
               "figure": "t-figure"}.get(label, "t-text")
        if w > 0 and h > 0:
            x1, y1, x2, y2 = (int(b[k]) for k in ("bbox_x1", "bbox_y1", "bbox_x2", "bbox_y2"))
            if x2 > x1 and y2 > y1:
                left = max(0.0, min(100.0, x1 / w * 100))
                top = max(0.0, min(100.0, y1 / h * 100))
                bw = max(0.4, min(100.0 - left, (x2 - x1) / w * 100))
                bh = max(0.4, min(100.0 - top, (y2 - y1) / h * 100))
                boxes.append(
                    f'<div class="box {cls}" data-seq="{seq}" '
                    f'style="left:{left:.3f}%;top:{top:.3f}%;width:{bw:.3f}%;height:{bh:.3f}%"></div>'
                )
        texts.append(
            f'<div class="row" data-seq="{seq}"><div class="no">{seq}</div>'
            f'<div class="tx">{_esc(text)}</div></div>'
        )

    lazy = "" if for_pdf else ' loading="lazy"'
    # 文字栏加 data-fit：打印前由 Chromium 量一次实际高度，装不下就逐级缩字号，
    # 避免 overflow:hidden 把长页的文字静默截掉（校对版截字比排版难看严重得多）。
    fit = ' data-fit="1"' if for_pdf else ""
    return f"""<section class="page">
<div class="proof-head">{_esc(_doc_title(task))}</div>
<div class="pnum">第 {page_no} 页　·　原图与文字逐块对应</div>
<div class="proof-row">
  <div class="proof-img"><img src="{img_src}" alt="page {page_no}"{lazy}>{''.join(boxes)}</div>
  <div class="proof-txt"{fit}>{''.join(texts) or '<p class="empty">未识别到文字</p>'}</div>
</div>
</section>"""


def write_html(path: str | Path, **kwargs) -> str:
    html_str = build_html(**kwargs)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(html_str, encoding="utf-8")
    return str(p)
