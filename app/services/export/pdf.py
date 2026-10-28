"""HTML → PDF（Chromium 打印，FR-EXP-02/05/06）。

为什么必须走 Chromium：竖排（writing-mode: vertical-rl）与中文字体子集只有真实
浏览器引擎能正确排版。WeasyPrint 对竖排支持很弱，会得到"看起来对、读起来错"的 PDF。

同一份 HTML + 同一份 CSS 既用于浏览器预览，也用于这里打印 —— 同源是硬约束。
"""
from __future__ import annotations

import shutil
from pathlib import Path

from ...config import settings


class PdfEngineUnavailable(Exception):
    pass


def playwright_available() -> bool:
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False
    return True


def chromium_path() -> str | None:
    if settings.playwright_chromium_path and Path(settings.playwright_chromium_path).exists():
        return settings.playwright_chromium_path

    # 首选问 Playwright 自己：它的目录布局随版本变化（曾因写死 chromium-*/chrome-mac/... 而误判未安装）
    if playwright_available():
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as p:
                exe = p.chromium.executable_path
            if exe and Path(exe).exists():
                return exe
        except Exception:  # noqa: BLE001 - 拿不到就走下面的兜底
            pass

    candidates = [
        Path.home() / "Library" / "Caches" / "ms-playwright",
        Path.home() / ".cache" / "ms-playwright",
    ]
    subpaths = (
        "chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
        "chrome-mac/Chromium.app/Contents/MacOS/Chromium",
        "chrome-linux/chrome",
        "chrome-win/chrome.exe",
    )
    for base in candidates:
        if not base.exists():
            continue
        for pattern in ("chromium-*", "chromium_headless_shell-*"):
            for d in sorted(base.glob(pattern), reverse=True):
                for sub in subpaths:
                    p = d / sub
                    if p.exists():
                        return str(p)
    return None


FIT_JS = """
() => {
  // 把 [data-fit] 容器里的内容缩到装得下为止。
  // 横排看 scrollHeight，竖排（writing-mode: vertical-rl）内容往左溢出，看 scrollWidth。
  // 缩到 8px 还装不下时，允许版面长高、流到下一张纸 —— 校对版截字比多一页严重得多。
  const report = [];
  for (const el of document.querySelectorAll('[data-fit]')) {
    const base = parseFloat(getComputedStyle(el).fontSize) || 15;
    let fs = base;
    const over = () => el.scrollHeight > el.clientHeight + 2 || el.scrollWidth > el.clientWidth + 2;
    let guard = 0;
    while (over() && fs > 8 && guard++ < 60) {
      fs = Math.max(8, fs - 0.5);
      el.style.fontSize = fs + 'px';
    }
    const stillOver = over();
    if (stillOver) {
      const pg = el.closest('.page');
      if (pg) { pg.style.height = 'auto'; pg.style.overflow = 'visible'; pg.style.breakInside = 'auto'; }
      el.style.overflow = 'visible';
    }
    report.push({ base, final: fs, overflow: stillOver });
  }
  return report;
}
"""


def html_to_pdf(
    html_str: str,
    out_path: str,
    landscape: bool = False,
    width_px: int | None = None,
    height_px: int | None = None,
    wait_for_fonts: bool = True,
    fit_containers: bool = True,
) -> dict:
    """把 HTML 字符串打印成 PDF。返回 {path, size}。"""
    if not playwright_available():
        raise PdfEngineUnavailable(
            "未安装 Playwright。请运行：.venv/bin/pip install playwright && .venv/bin/playwright install chromium"
        )

    from playwright.sync_api import sync_playwright

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp_html = out.with_suffix(".render.html")
    tmp_html.write_text(html_str, encoding="utf-8")

    exe = chromium_path()
    shrunk: list[dict] = []
    try:
        with sync_playwright() as p:
            launch_kwargs = {
                "args": [
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    # 页面是从 file:// 打开的临时 HTML，字体也是 file://。
                    # 图片不受 CORS 限制所以看着正常，但**字体受**：默认策略下
                    # 跨 file:// 的 @font-face 会被拒绝，静默退回系统 serif，
                    # 于是 PDF 里出现康熙部首码位 + 每页几十个 Type3 碎片字体。
                    # 复现与验证：tools/audit_pdf_text.py
                    "--allow-file-access-from-files",
                ]
            }
            if exe:
                launch_kwargs["executable_path"] = exe
            browser = p.chromium.launch(**launch_kwargs)
            try:
                page = browser.new_page(
                    viewport={"width": width_px or 900, "height": height_px or 1400}
                )
                page.goto(tmp_html.resolve().as_uri(), wait_until="load")
                if wait_for_fonts:
                    try:
                        page.wait_for_function("document.fonts.ready.then(()=>true)", timeout=8000)
                    except Exception:  # noqa: BLE001 - 字体加载超时不阻塞导出
                        pass
                page.emulate_media(media="print")
                if fit_containers:
                    # 必须在 emulate_media(print) 之后量：打印样式会改版面尺寸，
                    # 按屏幕样式量出来的尺寸不是最终纸面上的尺寸。
                    page.wait_for_timeout(120)
                    try:
                        shrunk = page.evaluate(FIT_JS) or []
                    except Exception:  # noqa: BLE001 - 缩放失败不阻塞导出
                        shrunk = []
                pdf_kwargs = {
                    "path": str(out),
                    "print_background": True,
                    "prefer_css_page_size": False,
                    "margin": {"top": "0", "bottom": "0", "left": "0", "right": "0"},
                }
                if width_px and height_px:
                    pdf_kwargs["width"] = f"{width_px}px"
                    pdf_kwargs["height"] = f"{height_px}px"
                else:
                    pdf_kwargs["format"] = "A4"
                    pdf_kwargs["landscape"] = landscape
                page.pdf(**pdf_kwargs)
            finally:
                browser.close()
    finally:
        # 这里**故意不删** `*.render.html`。
        #
        # 1. 它是同名覆盖的（`task_<id>_<kind>_<lang>.render.html`），同一任务同一
        #    格式永远只有一个，不会堆积；随任务 72h 一起清理。
        # 2. 它是排查版式问题最直接的证据（字体没生效、分页被腰斩，都可以直接
        #    用浏览器打开它看），删掉反而要重新造。
        # 3. 关键：某些宿主环境把 `unlink` 代理成「移入废纸篓」并限制单轮删除次数，
        #    导出 PDF 时会因此抛 `SystemExit`，整次导出 500。导出的副作用里不该
        #    包含「删文件」这种非必要动作。
        _ = tmp_html

    return {"path": str(out), "size": out.stat().st_size, "fit": shrunk}


def engine_status() -> dict:
    """健康检查用：报告 Playwright 与 Chromium 就绪情况。"""
    if not playwright_available():
        return {"ok": False, "reason": "playwright 未安装"}
    exe = chromium_path()
    if not exe:
        return {"ok": False, "reason": "chromium 未安装（playwright install chromium）"}
    return {"ok": True, "chromium": exe}


def which_pyftsubset() -> str | None:
    return shutil.which("pyftsubset")
