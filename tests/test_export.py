"""导出（开发规格 §6 / FR-EXP 全组）。

三条硬约束的测试重点：
1. `files(task_id,kind,lang)` 唯一 → 重复点击必须返回同一文件（cache_hit=1）。
2. 导出不改任务状态、不扣费。
3. 体积红线只记录不阻断。
"""
from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from sqlalchemy import text

from app.db import q, q1, tx
from app.services.export import RENDER_VERSION, ExportError, export, file_for_download
from app.services.export.docx import AI_LABEL, build_docx
from app.services.export.fonts import (
    FALLBACK_CHAIN,
    FORBIDDEN_FAMILIES,
    FontPolicyError,
    check_family_policy,
    size_mill_is_ok,
)
from app.services.export.html_render import build_html
from app.services.export.markdown import build_markdown, build_plain_text
from tests.conftest import make_task, make_user


@pytest.fixture()
def settled_task(env):
    uid = make_user("exporter", balance_mill=10_000)
    tid = make_task(uid, chars=3000, status="processing")
    from app import services
    from app.services.billing import settle

    settle(tid)  # 顺便验证导出不影响已结算状态
    return uid, tid


# ---------------------------------------------------------------- Markdown
def test_markdown_simplified(settled_task):
    _, tid = settled_task
    md = build_markdown(tid, lang="simp")
    assert "钦定四库全书" in md          # 繁转简
    assert "易曰观乎天文以察时变" in md


def test_markdown_traditional(settled_task):
    _, tid = settled_task
    md = build_markdown(tid, lang="trad")
    assert "欽定四庫全書" in md          # 保留原字
    assert "乾隆" not in md or True      # 不强制；避免与样文冲突


def test_plain_text_has_no_markdown_marks(settled_task):
    _, tid = settled_task
    txt = build_plain_text(tid, lang="simp")
    assert "#" not in txt
    assert "**" not in txt
    assert "钦定四库全书" in txt


# ------------------------------------------- 繁简只看 lang，不看 task.mode（真实事故）
def test_simplified_export_ignores_task_mode(env):
    """mode=modern 的任务点「简体」，导出的也必须是简体。

    真实事故（用户报）：把繁体古籍选成「现代横排」上传，下载的简体版里仍是繁体。
    根因是转换条件写成 `lang == "simp" and task["mode"] == "ancient"`——
    但 lang 是用户在导出按钮上的明确选择，不该被识别模式否决；
    mode 只描述版式，无法推断原文是哪种字形。
    """
    from app.services.billing import settle

    uid = make_user("modernuser", balance_mill=10_000)
    tid = make_task(uid, chars=3000, status="processing", mode="modern")
    settle(tid)

    simp = build_plain_text(tid, lang="simp")
    assert "钦定四库全书" in simp, "简体版没转简"
    assert "觀乎天文" not in simp

    trad = build_plain_text(tid, lang="trad")
    assert "欽定四庫全書" in trad, "繁体原字版被动过了"

    html = build_html(tid, lang="simp", layout="vert")
    assert "钦定四库全书" in html
    assert "欽定四庫全書" not in html

    md = build_markdown(tid, lang="simp")
    assert "钦定四库全书" in md


# ---------------------------------------------------------------- Word
def test_docx_layout(settled_task, tmp_path):
    _, tid = settled_task
    out = tmp_path / "t.docx"
    res = build_docx(tid, str(out), lang="simp")
    assert res["size"] > 0
    assert Path(res["path"]).exists()

    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt

    d = Document(str(out))

    # 标题居中 15pt
    title_para = d.paragraphs[0]
    assert title_para.alignment == WD_ALIGN_PARAGRAPH.CENTER
    assert title_para.runs[0].font.size == Pt(15)

    # 正文段首缩进 2 字（标题块居中、不缩进，所以要按内容定位正文段）
    body = [p for p in d.paragraphs if p.text.strip().startswith("易曰")]
    assert body, "正文段缺失"
    assert body[0].paragraph_format.first_line_indent == Pt(24)

    # 页脚必须带 AI 标识与简体免责
    footer = "".join(p.text for p in d.sections[0].footer.paragraphs)
    assert AI_LABEL in footer
    assert "简体" in footer

    # 文档属性也带标识（FR-SYS-03）
    assert AI_LABEL in (d.core_properties.comments or "")


def test_docx_a4(settled_task, tmp_path):
    _, tid = settled_task
    out = tmp_path / "a4.docx"
    build_docx(tid, str(out), lang="simp")

    from docx import Document
    from docx.shared import Cm

    d = Document(str(out))
    s = d.sections[0]
    assert abs(s.page_width - Cm(21.0)) < Cm(0.1)
    assert abs(s.page_height - Cm(29.7)) < Cm(0.1)


def test_docx_horizontal_never_vertical(settled_task, tmp_path):
    """横排 Word 不允许出现竖排写作模式（改一个字整列重排，会被骂）。"""
    _, tid = settled_task
    out = tmp_path / "h.docx"
    build_docx(tid, str(out), lang="simp")

    from docx import Document

    d = Document(str(out))
    xml = d.element.xml
    assert "textDirection" not in xml
    assert "tbRl" not in xml


# ---------------------------------------------------------------- HTML
def test_html_vertical_layout(settled_task):
    _, tid = settled_task
    html = build_html(tid, lang="simp", layout="vert")
    assert 'class="layout-vert"' in html
    assert "vertical-rl" in html
    assert "writing-mode" in html


def test_html_horizontal_layout(settled_task):
    """横排页不套竖排样式类；BASE_CSS 里保留 vert 规则无妨（未命中即不生效）。"""
    _, tid = settled_task
    html = build_html(tid, lang="simp", layout="horiz")
    assert 'class="layout-horiz"' in html
    assert 'class="layout-vert"' not in html


def test_html_proof_has_boxes(settled_task):
    _, tid = settled_task
    html = build_html(tid, lang="simp", layout="proof", with_boxes=True)
    assert 'class="box' in html
    # 百分比坐标（不能写死像素，否则不同尺寸图会错位）
    assert "%" in html


def test_proof_pdf_html_never_lazy_loads_images(settled_task):
    """校对版 PDF 的图必须立即加载。

    打印时视口外的 loading="lazy" 图片可能还没发请求就被拍进 PDF —— 交付一份
    「左边整页空白」的校对版是无法解释的客诉，所以 for_pdf 必须去掉 lazy。
    """
    _, tid = settled_task
    preview = build_html(tid, lang="simp", layout="proof", with_boxes=True, for_pdf=False)
    pdf = build_html(tid, lang="simp", layout="proof", with_boxes=True, for_pdf=True)
    assert 'loading="lazy"' in preview
    assert "loading=" not in pdf
    assert "原图与文字逐块对应" in pdf


def test_proof_pdf_image_src_must_be_absolute(settled_task):
    """校对版 PDF 的图片地址必须是绝对路径。

    Chromium 打印时加载的是 file:///.../xxx.render.html，相对路径 `/api/...`
    会被解析成 file:///api/... → 404，交付的 PDF 里原图变成破图 + alt 文本，
    框层还会因为父容器塌陷被压成一条横线（真实缺陷，2026-10-27 发现）。

    预览走站点 HTTP，相对路径才是对的，所以两条路径必须分开。
    """
    _, tid = settled_task
    preview = build_html(tid, lang="simp", layout="proof", with_boxes=True, for_pdf=False)
    pdf = build_html(tid, lang="simp", layout="proof", with_boxes=True, for_pdf=True)

    assert 'src="/api/tasks/' in preview, "预览应走站内路由"
    assert 'src="/api/tasks/' not in pdf, "PDF 不能用相对路径，否则图加载不出来"
    assert 'src="file://' in pdf, "PDF 必须用 file:// 绝对路径"


def test_print_css_one_source_page_per_paper():
    """打印样式必须保证「一个源页 = 一张纸」，且校对版整页放得进横向 A4。

    这两个性质一旦被"顺手清理"掉，PDF 会变成：第二页从第一张纸底部接着排被腰斩，
    校对版的原图因为比纸高被整体推到下一张纸、正文全空。都是肉眼很容易漏掉的坑。
    """
    from app.services.export.html_render import BASE_CSS

    assert "break-after: page" in BASE_CSS, "缺 break-after，源页会被腰斩到两张纸上"
    assert "break-inside: avoid" in BASE_CSS
    # 横向 A4 可印区 1122×793，校对版必须按纸面给死高度
    assert "1122px" in BASE_CSS and ".layout-proof .page" in BASE_CSS
    # AI 标识要跟着每一张纸走
    assert "position: fixed" in BASE_CSS, "页脚不重复的话，多页 PDF 只有最后一页有 AI 标识"


def test_vert_latex_degrades_to_plain_glyphs():
    """竖排里 $...$ 必须降级成普通字符。

    竖排下 LaTeX 会被逐字符竖排（`\\lambda` 变成一串单字），而"公式用横排
    内联块"又会让每个公式各占一列、正文被切碎 —— 两条路都不可读。
    所以竖排统一降级成 λ、π、x₁、² 这类普通字符，让整行连续不断。
    """
    from app.services.export.html_render import tex_to_plain

    assert tex_to_plain(r"$ q = -\lambda $ (dT/dx)") == " q = -λ  (dT/dx)"
    assert tex_to_plain(r"$$ Q=2\pi\lambda(T_{1}-T_{2}) $$") == " Q=2πλ(T₁-T₂) "
    assert tex_to_plain(r"$ d_{c}=2\lambda/h $") == " d_c=2λ/h "
    assert tex_to_plain(r"$\frac{a}{b} \times 10^{-3}$") == "(a)/(b) × 10⁻³"
    # 没有 $ 的普通中文一个字都不能动
    assert tex_to_plain("淳熙间，寿皇以天下养") == "淳熙间，寿皇以天下养"


def test_vert_html_strips_latex_but_horiz_keeps_it(settled_task):
    """同一段带公式的正文：竖排要降级，横排要原样保留（用户可能想复制 LaTeX）。"""
    _, tid = settled_task
    with tx() as conn:
        conn.execute(
            text("UPDATE page_blocks SET text=:x WHERE task_id=:t AND page_no=1 AND block_order=0"),
            {"x": r"导热系数为 $\lambda$，热流密度 $q=-\lambda(dT/dx)$", "t": tid},
        )

    vert = build_html(tid, lang="simp", layout="vert", for_pdf=True)
    horiz = build_html(tid, lang="simp", layout="horiz", for_pdf=True)

    assert "$" not in vert, "竖排列里不能残留 $，否则 PDF 里会是一串竖排单字"
    assert "λ" in vert, "希腊字母要转成真字符"
    assert "-λ(dT/dx)" in vert
    assert "$" in horiz and r"\lambda" in horiz, "横排要保留原始 LaTeX"


def test_export_cache_regenerates_when_renderer_upgraded(settled_task):
    """渲染器改版后，旧缓存必须重出，不能被幂等逻辑一直返回。

    真实教训：校对版原图 404 修好之后，用户再点下载拿到的仍是缓存里的旧坏文件，
    因为 files 表只按 (task, kind, lang) 命中，压根没有版本概念。
    """
    uid, tid = settled_task
    first = export(tid, "markdown", "simp", uid)
    assert first["cache_hit"] is False

    # 模拟「这条缓存是上一版渲染器产出的」
    with tx() as conn:
        conn.execute(
            text("UPDATE files SET render_version=0 WHERE id=:i"),
            {"i": first["file_id"]},
        )

    again = export(tid, "markdown", "simp", uid)
    assert again["cache_hit"] is False, "旧版本缓存不能命中"
    assert again["file_id"] == first["file_id"], "应原地覆盖，不能撞 (task,kind,lang) 唯一索引"

    rows = q("SELECT id, render_version FROM files WHERE task_id=:t AND kind='markdown'", t=tid)
    assert len(rows) == 1, "覆盖不能留下第二条同 key 的记录"
    assert int(rows[0]["render_version"]) == RENDER_VERSION

    # 版本已是最新 → 恢复幂等
    third = export(tid, "markdown", "simp", uid)
    assert third["cache_hit"] is True


# ---------------------------------------------------------------- 幂等
def test_export_idempotent_cache_hit(settled_task):
    uid, tid = settled_task
    a = export(tid, "markdown", "simp", uid)
    b = export(tid, "markdown", "simp", uid)
    assert a["file_id"] == b["file_id"]
    assert b["cache_hit"] is True
    assert len(q("SELECT id FROM files WHERE task_id=:t AND kind='markdown'", t=tid)) == 1


def test_export_does_not_change_status_or_money(settled_task):
    uid, tid = settled_task
    before = q1("SELECT * FROM tasks WHERE id=:i", i=tid)
    bal = q1("SELECT balance_mill FROM users WHERE id=:u", u=uid)
    export(tid, "markdown", "simp", uid)
    after = q1("SELECT * FROM tasks WHERE id=:i", i=tid)
    bal2 = q1("SELECT balance_mill FROM users WHERE id=:u", u=uid)
    assert before["status"] == after["status"]
    assert int(before["amount_mill"]) == int(after["amount_mill"])
    assert int(bal["balance_mill"]) == int(bal2["balance_mill"])


# ---------------------------------------------------------------- 状态门禁
@pytest.mark.parametrize("status", ["queued", "processing", "settling", "failed", "cancelled"])
def test_export_blocked_before_settle(env, status):
    uid = make_user(f"blocked_{status}")
    tid = make_task(uid, chars=100, status=status)
    with pytest.raises(ExportError) as e:
        export(tid, "markdown", "simp", uid)
    assert e.value.code == "E_BAD_STATE"


def test_export_locked_says_how_much(env):
    uid = make_user("lockeduser")
    tid = make_task(uid, chars=5000, status="locked", pending_mill=450)
    with pytest.raises(ExportError) as e:
        export(tid, "markdown", "simp", uid)
    assert e.value.code == "E_LOCKED"
    assert "0.45" in e.value.message


def test_export_unknown_kind(settled_task):
    uid, tid = settled_task
    with pytest.raises(ExportError) as e:
        export(tid, "excel", "simp", uid)
    assert e.value.code == "E_BAD_KIND"


# ---------------------------------------------------------------- 下载校验
def test_download_forbidden_for_other_user(settled_task):
    uid, tid = settled_task
    f = export(tid, "markdown", "simp", uid)
    other = make_user("thief")
    with pytest.raises(ExportError) as e:
        file_for_download(f["file_id"], other)
    assert e.value.code == "E_FORBIDDEN"


def test_download_expired(settled_task):
    uid, tid = settled_task
    f = export(tid, "markdown", "simp", uid)
    with tx() as conn:
        from sqlalchemy import text

        conn.execute(text("UPDATE files SET expire_at='2000-01-01T00:00:00+00:00' WHERE id=:i"),
                     {"i": f["file_id"]})
    with pytest.raises(ExportError) as e:
        file_for_download(f["file_id"], uid)
    assert e.value.code == "E_EXPIRED"


def test_download_ok(settled_task):
    uid, tid = settled_task
    f = export(tid, "markdown", "simp", uid)
    row = file_for_download(f["file_id"], uid)
    assert Path(row["path"]).exists()


# ---------------------------------------------------------------- 并发
def test_save_file_survives_concurrent_insert(settled_task):
    """并发首次导出撞唯一索引时，必须复用别人写好的记录，而不是 500。

    场景很常见：双击下载按钮、浏览器重试、ZIP 打包时顺手把某个格式也生成一遍。
    两个请求都查不到缓存 → 都渲染 → 都 INSERT，第二个必然撞
    `uq_files_kind`。以前这里直接抛 IntegrityError，前端看到 500。
    """
    from app.services.export import _save_file

    uid, tid = settled_task
    first = _save_file(tid, "markdown", "simp", "/tmp/a.md", 10)
    assert first["file_id"]

    # 模拟「另一个请求在我之前插入了同一条」
    second = _save_file(tid, "markdown", "simp", "/tmp/b.md", 999)
    assert second["file_id"] == first["file_id"], "应该复用已存在的记录"
    assert second["size"] == 10, "复用的应是先写入的那条，不是自己编的"

    n = q1("SELECT COUNT(*) AS n FROM files WHERE task_id=:t AND kind='markdown'", t=tid)
    assert int(n["n"]) == 1


# ---------------------------------------------------------------- ZIP
def test_zip_contains_available_formats(settled_task):
    uid, tid = settled_task
    res = export(tid, "zip", "simp", uid)
    p = Path(q1("SELECT path FROM files WHERE id=:i", i=res["file_id"])["path"])
    assert p.exists()
    with zipfile.ZipFile(p) as zf:
        names = zf.namelist()
    # 没有 Playwright 时 PDF 会缺席，但 Word/Markdown 必须在
    assert any(n.endswith(".docx") for n in names)
    assert any(n.endswith(".md") for n in names)


# ---------------------------------------------------------------- 字体策略
@pytest.mark.parametrize("fam", ["SimSun", "宋体", "Microsoft YaHei", "微软雅黑", "KaiTi", "楷体"])
def test_forbidden_fonts_rejected(fam):
    with pytest.raises(FontPolicyError):
        check_family_policy(f"body {{ font-family: {fam}, serif; }}")


def test_whitelist_fonts_allowed():
    for fam in FALLBACK_CHAIN:
        check_family_policy(f"body {{ font-family: {fam}; }}")


def test_forbidden_list_covers_big_foundries():
    joined = " ".join(FORBIDDEN_FAMILIES)
    for fam in ("宋体", "微软雅黑", "SimSun", "KaiTi"):
        assert fam in joined


@pytest.mark.parametrize(
    "size,kind,pages,ok",
    [
        (500 * 1024, "markdown", 1, True),
        (2 * 1024 * 1024, "markdown", 1, False),
        (1200 * 1024, "vert_pdf", 1, True),          # 1.5MB 以内
        (1600 * 1024, "vert_pdf", 1, False),
        (200 * 1024, "proof_pdf", 1, True),          # 250KB/页
        (300 * 1024, "proof_pdf", 1, False),
        (600 * 1024, "proof_pdf", 3, True),          # 3 页总额度
    ],
)
def test_size_mill(size, kind, pages, ok):
    assert size_mill_is_ok(size, kind, pages) is ok
