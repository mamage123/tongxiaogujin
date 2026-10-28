#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 test/ 目录里的样张成批跑一遍真实流程，并给出逐份体检表。

它解决的是「手上有一堆扫描件，想知道到底识别成什么样」这件事：
每份文件都走完整用户路径（登录 → 分片上传 → 建任务 → 等 worker 识别 →
七种格式导出 → 下载），最后把字数、耗时、体积、PDF 文本层质量列在一起。

和另外三套检查的分工：
  pytest              单元/接口级，进程内，不碰网络
  app.cli status      内部组件体检
  tools/accept.py     单张自造样张的端到端验收
  tools/batch_run.py  真实样张批量跑分（本脚本）

用法：
  python tools/batch_run.py                                  # 跑 test/ 下全部
  python tools/batch_run.py --dir test --mode ancient
  ADMIN_PWD=xxx python tools/batch_run.py --dir test --keep-going
  python tools/batch_run.py --dir test --only test.pdf

说明：
  · 必须给 --password 或 ADMIN_PWD（用管理员账号跑，避免免费额度/余额卡住批量流程）
  · 单任务识别是异步的，脚本只负责轮询；请确保 worker 在跑（./run.sh status）
  · 报告写到 docs/reports/<日期>-batch.md
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from audit_pdf_text import audit, verdict  # noqa: E402

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}
PDF_EXT = {".pdf"}

# (kind, lang) —— 与站内导出面板一致：繁体原字只给竖排
EXPORTS = [
    ("vert_pdf", "simp"),
    ("vert_pdf_trad", "trad"),
    ("horiz_pdf", "simp"),
    ("proof_pdf", "simp"),
    ("markdown", "simp"),
    ("horiz_docx", "simp"),
    ("zip", "simp"),
]
DONE_STATES = {"settled", "partial", "failed", "locked", "expired", "cancelled"}
EXPORT_EXT = {
    "vert_pdf": ".pdf",
    "vert_pdf_trad": ".pdf",
    "horiz_pdf": ".pdf",
    "proof_pdf": ".pdf",
    "markdown": ".md",
    "horiz_docx": ".docx",
    "zip": ".zip",
}


def _w(s: str) -> int:
    """显示宽度：中文按两列算，否则表格会错位。"""
    return sum(2 if ord(ch) > 0x2E80 else 1 for ch in s)


def pad(s: str, width: int) -> str:
    return s + " " * max(0, width - _w(s))


def collect(dir_path: Path, only: list[str]) -> list[Path]:
    files = []
    for p in sorted(dir_path.iterdir()):
        if not p.is_file() or p.name.startswith("."):
            continue
        if p.suffix.lower() not in IMAGE_EXT | PDF_EXT:
            continue
        if only and p.name not in only:
            continue
        files.append(p)
    return files


def login(base: str, user: str, pwd: str) -> httpx.Client:
    c = httpx.Client(base_url=base, timeout=300, follow_redirects=True)
    r = c.post("/api/auth/login", json={"username": user, "password": pwd})
    if r.status_code != 200:
        raise SystemExit(f"登录失败 {r.status_code}：{r.text[:200]}")
    return c


def upload(c: httpx.Client, path: Path) -> str:
    data = path.read_bytes()
    r = c.post("/api/upload/init", json={"filename": path.name, "size": len(data)})
    if r.status_code != 200:
        raise RuntimeError(f"上传初始化失败 {r.status_code}：{r.text[:160]}")
    d = r.json()["data"]
    up, chunk, total = d["upload_id"], int(d["chunk_size"]), int(d["total_chunks"])
    for i in range(total):
        part = data[i * chunk:(i + 1) * chunk]
        rr = c.post(
            f"/api/upload/chunk?upload_id={up}&index={i}",
            files={"file": (f"chunk_{i}", part, "application/octet-stream")},
        )
        if rr.status_code != 200:
            raise RuntimeError(f"第 {i} 片上传失败：{rr.text[:160]}")
    rr = c.post("/api/upload/complete", json={"upload_id": up})
    if rr.status_code != 200:
        raise RuntimeError(f"合并失败 {rr.status_code}：{rr.text[:160]}")
    return up


def topup(c: httpx.Client, yuan: float) -> None:
    """给当前账号注入测试余额（走管理员的「调整余额」接口，会留审计痕迹）。"""
    me = c.get("/api/auth/me")
    if me.status_code != 200:
        raise SystemExit(f"读取当前账号失败 {me.status_code}：{me.text[:160]}")
    uid = me.json()["data"]["id"]
    r = c.post(
        f"/admin/api/users/{uid}/balance",
        json={
            "delta_mill": int(yuan * 1000),
            "reason": "batch_run.py 批量体检注入测试余额",
            "voucher": f"BATCH-{int(time.time())}",
        },
    )
    if r.status_code != 200:
        raise SystemExit(f"注入余额失败 {r.status_code}：{r.text[:200]}")
    print(f"· 已注入测试余额 ¥{yuan:g}（走后台调整余额接口，留审计）\n")


def run_one(c: httpx.Client, path: Path, mode: str, optimize: bool, wait_s: int) -> dict:
    row: dict = {"file": path.name, "size": path.stat().st_size}
    t0 = time.time()
    up = upload(c, path)
    row["upload_s"] = round(time.time() - t0, 1)

    r = c.post("/api/tasks", json={"upload_id": up, "mode": mode, "optimize": optimize, "page_range": "all"})
    if r.status_code != 200:
        body = r.text[:200]
        code = ""
        try:
            code = (r.json().get("error") or {}).get("code") or ""
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError(f"建任务失败 {r.status_code} {code}：{body}")

    tid = r.json()["data"]["id"]
    row["task_id"] = tid
    st, d = "?", {}
    deadline = time.time() + wait_s
    while time.time() < deadline:
        d = c.get(f"/api/tasks/{tid}").json()["data"]
        st = d["status"]
        if st in DONE_STATES:
            break
        time.sleep(2)
    row["status"] = st
    row["chars"] = d.get("chars") or 0
    row["amount"] = d.get("amount_text") or ""
    row["spread"] = "双页→2" if d.get("spread_split") else "单页"
    pg_list = d.get("pages")
    row["pages"] = len(pg_list) if isinstance(pg_list, list) else (d.get("page_count") or "")
    # 行序自检通过页数：横版页 + 竖排多列是星河最容易切错块的场景，
    # 这一列低了就说明「可读性」有问题，光看字数看不出来。
    if isinstance(pg_list, list) and pg_list:
        ok_n = sum(1 for p in pg_list if p.get("order_check_passed") == 1)
        row["order"] = f"{ok_n}/{len(pg_list)}"
    else:
        row["order"] = "—"
    row["recognize_s"] = round(time.time() - t0 - row["upload_s"], 1)
    if st not in ("settled", "partial"):
        row["error"] = d.get("error_text") or d.get("error_msg") or "任务未完成"
        return row

    exports = {}
    save_dir = ROOT / ".tmp" / "batch" / tid
    save_dir.mkdir(parents=True, exist_ok=True)
    for kind, lang in EXPORTS:
        t1 = time.time()
        rr = c.post(f"/api/tasks/{tid}/exports", json={"kind": kind, "lang": lang})
        if rr.status_code != 200:
            exports[kind] = {"ok": False, "err": f"{rr.status_code} {rr.text[:80]}"}
            continue
        url = rr.json()["data"]["download_url"]
        dl = c.get(url)
        body = dl.content
        ok = dl.status_code == 200 and len(body) > 0
        path = ""
        if ok:
            # 下载接口是 /api/exports/<file_id>/download，拿不到本地路径，
            # 就落一份到 .tmp/batch/<task_id>/ 供体检和人工翻看。
            ext = EXPORT_EXT.get(kind, ".bin")
            p = save_dir / f"{kind}_{lang}{ext}"
            p.write_bytes(body)
            path = str(p)
        exports[kind] = {
            "ok": ok,
            "bytes": len(body),
            "url": url,
            "sec": round(time.time() - t1, 1),
            "path": path,
        }
    row["exports"] = exports
    row["saved"] = str(save_dir)

    # PDF 文本层体检（只对下载成功的本地 PDF；下载走 /data 静态目录时可直接读盘）
    pdfs = []
    for kind in ("vert_pdf", "vert_pdf_trad", "horiz_pdf", "proof_pdf"):
        e = exports.get(kind) or {}
        p = Path(e.get("path") or "")
        if e.get("ok") and p.is_file() and p.suffix.lower() == ".pdf":
            try:
                a = audit(p)
                ok, reasons = verdict(a)
                pdfs.append({"kind": kind, "ok": ok, "radicals": len(a["radicals"]),
                             "type3": a["type3"], "chars": a["chars"],
                             "fonts": a["fonts"], "reasons": reasons})
            except Exception as e2:  # noqa: BLE001
                pdfs.append({"kind": kind, "ok": False, "reasons": [str(e2)[:80]]})
    row["pdf_audit"] = pdfs
    return row


def report(rows: list[dict], mode: str, base: str) -> str:
    lines = [
        f"# 批量识别体检 · {datetime.now():%Y-%m-%d %H:%M}",
        "",
        f"- 目标服务：`{base}`",
        f"- 识别模式：`{mode}`",
        f"- 样张数量：{len(rows)}",
        "",
        "## 逐份结果",
        "",
        "「拆分」列：`双页→2` 表示这张图是摊开拍的两页，已按中缝自动拆成右/左两页分别识别"
        "（古籍右起，右半在前）；`单页` 表示按整张识别。",
        "",
        "「行序」列：行序自检通过的页数 / 总页数。低于满分说明这页的块顺序可疑"
        "（多半是供应商把多列合并成一块，横版扫描页最容易出），要对照原图核对。",
        "",
        "| 文件 | 大小 | 页 | 拆分 | 行序 | 字数 | 金额 | 状态 | 上传 | 识别 | 部首 | Type3 |",
        "|---|---:|---:|---|---|---:|---:|---|---:|---:|---:|---:|",
    ]
    for r in rows:
        rad = "/".join(str(a.get("radicals", "—")) for a in r.get("pdf_audit") or [] if a.get("radicals") is not None) or "—"
        t3 = "/".join(str(a.get("type3", "—")) for a in r.get("pdf_audit") or [] if a.get("type3") is not None) or "—"
        lines.append(
            f"| {r['file'][:30]} | {r.get('size', 0) / 1024:.0f}KB | {r.get('pages', '')} | "
            f"{r.get('spread', '')} | {r.get('order', '—')} | {r.get('chars', 0)} | {r.get('amount', '')} | {r.get('status', '')} | "
            f"{r.get('upload_s', '')}s | {r.get('recognize_s', '')}s | {rad} | {t3} |"
        )

    bad = [r for r in rows if (r.get("status") not in ("settled", "partial"))]
    lines += ["", "## 失败与异常", ""]
    if not bad:
        lines.append("无。")
    for r in bad:
        lines.append(f"- `{r['file']}`：{r.get('error', '未完成')}")

    # 导出体积一览
    lines += ["", "## 导出体积（KB）", "",
              "| 文件 | " + " | ".join(k for k, _ in EXPORTS) + " |",
              "|---|" + "---:|" * len(EXPORTS)]
    for r in rows:
        cells = []
        for k, _ in EXPORTS:
            e = (r.get("exports") or {}).get(k) or {}
            cells.append(f"{e['bytes'] / 1024:.1f}" if e.get("ok") else ("✗" if e else "—"))
        lines.append(f"| {r['file'][:30]} | " + " | ".join(cells) + " |")

    lines += ["", "## PDF 文本层（复制/搜索能力）", "",
              "- 康熙部首应为 0：出现说明字体没走白名单，复制出来是 ⻩⼆ 这类怪字",
              "- Type3 应为 0：出现说明 Chromium 没拿到可子集化的字体，体积会虚高", ""]
    for r in rows:
        for a in r.get("pdf_audit") or []:
            flag = "✓" if a.get("ok") else "✗"
            lines.append(f"- {flag} `{r['file'][:24]}` / {a['kind']}：部首 {a.get('radicals', '?')}、Type3 {a.get('type3', '?')}、{a.get('chars', '?')} 字")
            for reason in a.get("reasons") or []:
                lines.append(f"  - {reason}")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=str(ROOT / "test"))
    ap.add_argument("--base", default=os.environ.get("BASE", "http://127.0.0.1:8000"))
    ap.add_argument("--user", default=os.environ.get("ADMIN_USER", "admin"))
    ap.add_argument("--password", default=os.environ.get("ADMIN_PWD", ""))
    ap.add_argument("--mode", default="ancient", choices=["ancient", "modern"])
    ap.add_argument("--no-optimize", action="store_true")
    ap.add_argument("--only", nargs="*", default=[])
    ap.add_argument("--wait", type=int, default=1800, help="单份最长等待秒数")
    ap.add_argument("--topup", type=float, default=0,
                    help="跑之前给账号注入测试余额（元）；批量跑通常需要，仅测试用")
    ap.add_argument("--keep-going", action="store_true", help="单份失败继续跑下一份")
    args = ap.parse_args()

    if not args.password:
        print("需要管理员密码：--password xxx 或 export ADMIN_PWD=xxx")
        print("（批量跑必须用管理员，否则第 2 份就会撞上『你有任务正在处理中』或余额不足）")
        return 2

    dir_path = Path(args.dir)
    if not dir_path.is_dir():
        print(f"目录不存在：{dir_path}")
        return 2
    files = collect(dir_path, args.only)
    if not files:
        print(f"{dir_path} 里没有可识别的图片/PDF（支持 {' '.join(sorted(IMAGE_EXT | PDF_EXT))}）")
        return 2

    print(f"目标 {args.base}｜模式 {args.mode}｜{len(files)} 份样张\n")
    c = login(args.base, args.user, args.password)
    if args.topup:
        topup(c, args.topup)

    rows: list[dict] = []
    for i, f in enumerate(files, 1):
        print(f"[{i}/{len(files)}] {f.name}  ({f.stat().st_size / 1024:.0f}KB)")
        try:
            row = run_one(c, f, args.mode, not args.no_optimize, args.wait)
        except Exception as e:  # noqa: BLE001
            row = {"file": f.name, "size": f.stat().st_size, "status": "error", "error": str(e)[:200]}
            print(f"      ✗ {row['error']}")
            rows.append(row)
            if not args.keep_going:
                break
            continue
        rows.append(row)
        mark = "✓" if row["status"] in ("settled", "partial") else "✗"
        print(f"      {mark} {row['status']}  {row.get('chars', 0)} 字  "
              f"{row.get('recognize_s', '')}s  导出 {sum(1 for e in (row.get('exports') or {}).values() if e.get('ok'))}/7")
        for a in row.get("pdf_audit") or []:
            if not a.get("ok"):
                print(f"        ! {a['kind']} 文本层：{'；'.join(a.get('reasons') or [])}")

    ok = sum(1 for r in rows if r.get("status") in ("settled", "partial"))
    print(f"\n完成 {ok}/{len(rows)} 份")

    out_dir = ROOT / "docs" / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{datetime.now():%Y-%m-%d}-batch.md"
    out.write_text(report(rows, args.mode, args.base), encoding="utf-8")
    print(f"报告：{out.relative_to(ROOT)}")
    return 0 if ok == len(rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
