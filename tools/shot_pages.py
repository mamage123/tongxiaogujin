"""页面巡检：逐页截图 + 收集浏览器控制台报错。

找的是「视觉坏了」和「JS 报错但页面仍然 200」这两类，别的工具抓不到：
  pytest / probe_all 只看 HTTP 状态，页面白屏、样式塌、Alpine 报错它们一律无感。

用法：
  ADMIN_PWD=ma .venv/bin/python tools/shot_pages.py
输出：tmp/shots/*.png + 控制台错误清单
退出码 0 = 无 JS 报错、无 5xx 导航。
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.sync_api import sync_playwright  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
BASE = os.environ.get("BASE", "http://127.0.0.1:8000").rstrip("/")
ADMIN_PWD = os.environ.get("ADMIN_PWD", "")
OUT = ROOT / "tmp" / "shots"


def latest_task_id() -> str:
    c = sqlite3.connect(ROOT / "data" / "app.db")
    r = c.execute("select id from tasks order by created_at desc limit 1").fetchone()
    c.close()
    return r[0] if r else ""


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    task_id = latest_task_id()
    print(f"BASE={BASE}  task={task_id}\n")

    pages = [
        ("home", "/", False),
        ("login", "/login", False),
        ("terms", "/terms", False),
        ("recharge", "/recharge", False),
        ("payqr", "/pay/qr/alipay", False),
        ("account", "/account", True),
        ("admin", "/admin", True),
    ]
    if task_id:
        pages.append(("task", f"/task/{task_id}", True))

    errors: list[str] = []
    with sync_playwright() as pw:
        br = pw.chromium.launch()
        ctx = br.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()

        bucket: list[str] = []
        page.on("console", lambda m: bucket.append(f"console.{m.type}: {m.text}") if m.type == "error" else None)
        page.on("pageerror", lambda e: bucket.append(f"pageerror: {e}"))

        # 登录（先 admin，后面普通页也够用）
        if ADMIN_PWD:
            page.goto(f"{BASE}/login", wait_until="networkidle")
            page.fill("input[type=text]", "admin")
            page.fill("input[type=password]", ADMIN_PWD)
            # 提交按钮是 @click 驱动的 <button class="btn-block">，没有 type=submit
            page.click("button.btn-block")
            page.wait_for_timeout(1500)
            print(f"登录后 URL: {page.url}")

        for name, path, need_login in pages:
            bucket.clear()
            resp = page.goto(f"{BASE}{path}", wait_until="networkidle")
            page.wait_for_timeout(600)
            shot = OUT / f"{name}.png"
            page.screenshot(path=str(shot), full_page=True)
            code = resp.status if resp else -1
            # 过滤掉第三方/字体类噪音
            real = [e for e in bucket if "favicon" not in e and "net::ERR_ABORTED" not in e]
            flag = ""
            if code >= 500:
                flag = " 5xx!"
                errors.append(f"[5xx] {path} -> {code}")
            if real:
                flag += " js!"
                errors.extend(f"[js] {path} :: {e}" for e in real)
            print(f"{path:34s} {code}  {shot.name:14s}{flag}")

        br.close()

    print()
    if errors:
        print(f"发现 {len(errors)} 个问题：")
        for e in errors:
            print("  " + e)
        return 1
    print("全部通过：无 JS 报错，无 5xx。截图见 tmp/shots/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
