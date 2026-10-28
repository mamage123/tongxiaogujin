"""对外验收：对着**正在跑的服务**把真实用户流程走一遍。

和另外两套检查的分工：
  pytest          —— 单元/接口级，进程内跑，不碰网络
  app.cli status  —— 内部组件体检（库、字体、OCR、PDF 引擎、队列、账实）
  tools/accept.py —— 端到端：真 HTTP、真上传、真识别、真下载，并按 magic 校验文件

用法：
  python tools/accept.py                        # 默认 http://127.0.0.1:8000
  BASE=http://1.2.3.4 python tools/accept.py
  ADMIN_USER=admin ADMIN_PWD=xxx python tools/accept.py

退出码 0 = 全部通过；非 0 = 有 N 项不通过（可直接接 CI 或上线前脚本）。
退出码 2 = 服务连不上。
"""
from __future__ import annotations

import io
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

BASE = os.environ.get("BASE", "http://127.0.0.1:8000").rstrip("/")
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PWD = os.environ.get("ADMIN_PWD", "")

PAGES = ["/", "/login", "/terms", "/recharge", "/account"]
ADMIN_APIS = [
    "/admin/api/dashboard", "/admin/api/users", "/admin/api/recharge",
    "/admin/api/ledger", "/admin/api/ledger/daily", "/admin/api/integrity",
    "/admin/api/tasks", "/admin/api/exports/health", "/admin/api/settings",
    "/admin/api/quality/stats", "/admin/api/cleanup/records", "/admin/api/logs",
]
KINDS = ["vert_pdf", "vert_pdf_trad", "horiz_pdf", "proof_pdf", "markdown", "horiz_docx", "zip"]
MAGIC = {".pdf": b"%PDF-", ".docx": b"PK\x03\x04", ".zip": b"PK\x03\x04"}


class Checker:
    def __init__(self) -> None:
        self.bad = 0
        self.warned = 0

    @staticmethod
    def _pad(s: str, width: int = 26) -> str:
        """按显示宽度补齐（中文算两列），否则中英混排的标签会参差不齐。"""
        w = sum(2 if ord(ch) > 0x2E80 else 1 for ch in s)
        return s + " " * max(0, width - w)

    def check(self, ok: bool, label: str, extra: str = "") -> None:
        self.bad += 0 if ok else 1
        print(f"  {'✓' if ok else '✗'} {self._pad(label)}{extra}")

    def warn(self, ok: bool, label: str, extra: str = "") -> None:
        """环境依赖项：缺了不判失败，但要显眼地指出来。"""
        self.warned += 0 if ok else 1
        print(f"  {'✓' if ok else '!'} {self._pad(label)}{extra}")


_SAMPLE_COLUMNS = [
    "西湖游幸",
    "淳熙间，寿皇以天下养，每奉德寿宫，游幸湖山，御大龙舟。",
    "宰相执政，以至京尹，皆预焉。小舟两旁，各列数十人，",
    "撑篙者、打桨者、牵挽者，皆选少年善泅者为之。",
]

_FONT_CANDIDATES = (
    "/System/Library/Fonts/Supplemental/Songti.ttc",
    "/System/Library/Fonts/Supplemental/Kaiti.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
)


def _cjk_font(size: int):
    from PIL import ImageFont

    for p in _FONT_CANDIDATES:
        if Path(p).exists():
            try:
                return ImageFont.truetype(p, size)
            except Exception:  # noqa: BLE001
                continue
    return None


def sample_image() -> bytes:
    """造一张能真的被 OCR 认出字的竖排样张。

    早期版本只画一块纯色底 → mock 能过、**真实供应商返回 0 字**，验收脚本就会
    误报「识别失败」。样张必须带真字：右起竖排四列，字距行距都拉开。
    """
    im = Image.new("RGB", (1240, 1754), (247, 241, 229))
    draw = ImageDraw.Draw(im)
    font = _cjk_font(46)
    if font is not None:
        size = 46
        step = 62
        for ci, col in enumerate(_SAMPLE_COLUMNS):
            x = 1100 - ci * 230
            y = 150 + (40 if ci == 0 else 0)
            for ch in col:
                if y > 1640:
                    break
                draw.text((x, y), ch, font=font, fill=(28, 26, 22))
                y += step

    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def main() -> int:
    ck = Checker()
    c = httpx.Client(base_url=BASE, timeout=180, follow_redirects=True)

    try:
        c.get("/")
    except Exception as e:  # noqa: BLE001
        print(f"✗ 连不上 {BASE}：{e}")
        print("  先起服务：./run.sh")
        return 2

    print(f"目标 {BASE}\n")

    print("页面")
    for p in PAGES:
        r = c.get(p)
        ck.check(r.status_code == 200, p, str(r.status_code))

    print("\n健康检查")
    r = c.get("/api/meta/health")
    data = r.json().get("data", {}) if r.status_code == 200 else {}
    ck.check(r.status_code == 200, "/api/meta/health", str(r.status_code))
    if data:
        db = data.get("db") or {}
        fo = data.get("fonts") or {}
        pdf = data.get("pdf_engine") or {}
        ocr = data.get("ocr") or {}
        n_font = sum(1 for a in fo.get("assets") or [] if a.get("exists"))
        ck.check(bool(db.get("ok")), "数据库", "ok" if db.get("ok") else str(db.get("error"))[:60])
        ck.check(bool(pdf.get("ok")), "PDF 引擎", "就绪" if pdf.get("ok") else str(pdf.get("reason"))[:60])
        ck.warn(bool(fo.get("ok")), "字体", f"{n_font} 个可用" if n_font else "0 个（PDF 将退回系统 serif，观感与体积无法定论）")
        ck.warn(not ocr.get("mock"), "OCR 通道",
                f"{ocr.get('primary')} mock={'开' if ocr.get('mock') else '关'}"
                f" token={'有' if ocr.get('token_configured') else '无'}")

    print("\n真实用户流程")
    name = f"accept{int(time.time()) % 1000000}"
    r = c.post("/api/auth/register", json={"username": name, "password": "pass12345", "agree": True})
    ck.check(r.status_code == 200, f"注册 {name}", str(r.status_code))
    if r.status_code != 200:
        print("  注册失败，后续流程无法继续")
        return ck.bad
    uid = r.json()["data"]["id"]

    blob = sample_image()
    r = c.post("/api/upload/init", json={"filename": "验收样张.png", "size": len(blob)})
    ck.check(r.status_code == 200, "上传初始化", str(r.status_code))
    if r.status_code != 200:
        print(f"      {r.text[:120]}")
        return ck.bad
    up = r.json()["data"]["upload_id"]
    c.post(f"/api/upload/chunk?upload_id={up}&index=0",
           files={"file": ("blob", blob, "application/octet-stream")})
    c.post("/api/upload/complete", json={"upload_id": up})

    r = c.post("/api/tasks", json={"upload_id": up, "mode": "ancient"})
    if r.status_code == 402 and ADMIN_PWD:
        # 同一设备 24h 内第 3 个账号起不发免费额度（风控正常行为）。
        # 验收要跑完导出，就用管理员的「调整余额」接口给测试账号充一笔 —— 顺带把这条路径也验了。
        print("  · 该账号无免费额度（同设备短时间多次注册），用管理员调整余额后续跑")
        c2 = httpx.Client(base_url=BASE, timeout=120)
        lg = c2.post("/api/auth/login", json={"username": ADMIN_USER, "password": ADMIN_PWD})
        if lg.status_code == 200:
            rr = c2.post(f"/admin/api/users/{uid}/balance",
                         json={"delta_mill": 200000, "reason": "验收脚本注入测试余额",
                               "voucher": f"ACCEPT-{int(time.time())}"})
            ck.check(rr.status_code == 200, "管理员调整余额",
                     (rr.json().get("data") or {}).get("balance_after_text", ""))
        else:
            ck.check(False, "管理员登录", str(lg.status_code))
        r = c.post("/api/tasks", json={"upload_id": up, "mode": "ancient"})
    if r.status_code == 402:
        print("  · 该账号免费额度已用完且余额为 0（新账号同设备多次注册会被扣发额度）")
        print("    这是预期的风控行为；要跑完整导出验收，请提供 ADMIN_PWD 让脚本注入测试余额。")
        return ck.bad
    ck.check(r.status_code == 200, "建任务", str(r.status_code))
    if r.status_code != 200:
        print(f"      {r.text[:120]}")
        return ck.bad
    tid = r.json()["data"]["id"]

    st, d = "?", {}
    for _ in range(90):
        time.sleep(1)
        d = c.get(f"/api/tasks/{tid}").json()["data"]
        st = d["status"]
        if st in ("settled", "partial", "failed", "locked", "expired"):
            break
    ck.check(st in ("settled", "partial"), f"识别完成 {tid[:8]}", f"{st} {d.get('chars')} 字 {d.get('amount_text')}")
    if st not in ("settled", "partial"):
        print("  ⚠ 任务未完成，导出无法验收。检查 worker 是否在跑（./run.sh status）")
        return ck.bad

    print("\n预览三排版（与 PDF 同源）")
    for lay in ("horiz", "vert", "proof"):
        r = c.get(f"/api/tasks/{tid}/preview?layout={lay}&lang=simp")
        ck.check(r.status_code == 200 and f'class="layout-{lay}"' in r.text,
                 f"layout={lay}", f"{r.status_code} {len(r.text)}B")

    print("\n七种导出（按 magic 校验，不看大小猜）")
    for k in KINDS:
        r = c.post(f"/api/tasks/{tid}/exports", json={"kind": k, "lang": "simp"})
        if r.status_code != 200:
            ck.check(False, k, f"{r.status_code} {r.text[:60]}")
            continue
        url = r.json()["data"]["download_url"]
        dl = c.get(url)
        body = dl.content
        ext = "." + url.rsplit(".", 1)[-1].split("?")[0]
        want = MAGIC.get(ext)
        ck.check(dl.status_code == 200 and (want is None or body.startswith(want)) and len(body) > 0,
                 k, f"{len(body) / 1024:.1f}KB {body[:6]!r}")

    print("\n后台接口")
    if not ADMIN_PWD:
        print("  · 跳过：未提供 ADMIN_PWD")
    else:
        c2 = httpx.Client(base_url=BASE, timeout=120)
        r = c2.post("/api/auth/login", json={"username": ADMIN_USER, "password": ADMIN_PWD})
        ck.check(r.status_code == 200, f"管理员登录 {ADMIN_USER}", str(r.status_code))
        if r.status_code == 200:
            for p in ADMIN_APIS:
                rr = c2.get(p)
                ck.check(rr.status_code == 200, p, str(rr.status_code))

    print()
    tail = "" if ck.warned == 0 else f"（另有 {ck.warned} 处环境依赖项待补，见上）"
    print("结论：" + ("全部通过" if ck.bad == 0 else f"{ck.bad} 项不通过") + tail)
    return 0 if ck.bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
