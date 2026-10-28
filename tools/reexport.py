"""对**已完成的任务**重跑七种导出并校验交付件（不重新识别，不花钱不占 OCR）。

用途：改了渲染/字体/模板之后，确认「用户现在下载到的文件」是好的。
batch_run.py 会连识别一起跑，等不起；这个只打导出。

用法：
  ADMIN_PWD=ma .venv/bin/python tools/reexport.py                 # 取最近一个 settled 任务
  ADMIN_PWD=ma .venv/bin/python tools/reexport.py <task_id>
退出码 0 = 七种全出且 magic 正确。
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
BASE = os.environ.get("BASE", "http://127.0.0.1:8000").rstrip("/")
ADMIN_PWD = os.environ.get("ADMIN_PWD", "")
OUT = ROOT / "tmp" / "reexport"

KINDS = [
    ("vert_pdf", "simp", b"%PDF-"),
    ("vert_pdf_trad", "trad", b"%PDF-"),
    ("horiz_pdf", "simp", b"%PDF-"),
    ("proof_pdf", "simp", b"%PDF-"),
    ("horiz_docx", "simp", b"PK\x03\x04"),
    ("markdown", "simp", None),          # 纯文本，看开头有没有 # 或文字
    ("zip", "simp", b"PK\x03\x04"),
]

# 落盘用：download_url 里没有后缀，扩展名得自己按 kind 给
EXT = {
    "vert_pdf": ".pdf", "vert_pdf_trad": ".pdf", "horiz_pdf": ".pdf",
    "proof_pdf": ".pdf", "horiz_docx": ".docx", "markdown": ".md", "zip": ".zip",
}


def latest_settled() -> tuple[str, str]:
    c = sqlite3.connect(ROOT / "data" / "app.db")
    c.row_factory = sqlite3.Row
    r = c.execute(
        "select id, filename from tasks where status='settled' order by created_at desc limit 1"
    ).fetchone()
    c.close()
    return (r["id"], r["filename"]) if r else ("", "")


def main() -> int:
    tid = sys.argv[1] if len(sys.argv) > 1 else ""
    fname = ""
    if not tid:
        tid, fname = latest_settled()
    if not tid:
        print("没有可用的已完成任务")
        return 2
    print(f"任务 {tid}  ({fname})\n")
    OUT.mkdir(parents=True, exist_ok=True)

    c = httpx.Client(base_url=BASE, timeout=180)
    if ADMIN_PWD and not c.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PWD}).status_code == 200:
        print("管理员登录失败")
        return 2

    bad: list[str] = []
    w = 16
    print(f"{'kind'.ljust(w)}{'生成':>6}{'下载':>6}{'字节':>12}  magic/说明")
    print("-" * 78)
    for kind, lang, magic in KINDS:
        r = c.post(f"/api/tasks/{tid}/exports", json={"kind": kind, "lang": lang})
        if r.status_code != 200:
            print(f"{kind.ljust(w)}{r.status_code:>6}{'-':>6}{'-':>12}  {r.text[:120]}")
            bad.append(f"{kind}: 生成 {r.status_code} {r.text[:120]}")
            continue
        d = r.json()["data"]
        dr = c.get(d["download_url"])
        if dr.status_code != 200:
            print(f"{kind.ljust(w)}{'200':>6}{dr.status_code:>6}{'-':>12}  下载失败")
            bad.append(f"{kind}: 下载 {dr.status_code}")
            continue
        blob = dr.content
        p = OUT / f"{kind}-{lang}{EXT.get(kind, '')}"
        # 用响应头里的文件名落盘，顺便验证 Content-Disposition 带了扩展名
        cd = dr.headers.get("content-disposition", "")
        p.write_bytes(blob)

        note = ""
        if magic and not blob.startswith(magic):
            note = f"magic 不对（前 8 字节 {blob[:8]!r}）"
            bad.append(f"{kind}: {note}")
        elif kind == "markdown":
            head = blob[:40].decode("utf-8", "replace").strip()
            note = repr(head)
            if not head:
                bad.append(f"{kind}: 内容为空")
        else:
            note = "ok"
        # 文件名必须带扩展名，否则用户下下来双击打不开
        if "filename" in cd:
            import urllib.parse

            fn = urllib.parse.unquote(cd.split("filename*=UTF-8''")[-1] if "UTF-8''" in cd else cd)
            if "." not in fn.replace(" ", ""):
                bad.append(f"{kind}: 下载文件名没有扩展名（{fn}）")
                note += f"  ⚠ 文件名 {fn}"
        cache = " 缓存" if d.get("cache_hit") else ""
        print(f"{kind.ljust(w)}{'200':>6}{'200':>6}{len(blob):>12}  {note}{cache}")

    print()
    print(f"落盘目录：{OUT}")
    if bad:
        print(f"\n{len(bad)} 个问题：")
        for b in bad:
            print("  " + b)
        return 1
    print("七种导出全部通过（magic 正确、文件名带扩展名）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
