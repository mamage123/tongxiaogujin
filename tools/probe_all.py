"""全站巡检：三种身份 × 全部 GET 端点，抓 500 与越权。

和另几套检查的分工：
  pytest          —— 单元/接口级，进程内
  app.cli status  —— 内部组件体检
  tools/accept.py —— 端到端主流程（真上传真识别，慢）
  tools/probe_all.py —— 广度巡检：一个不漏地打一遍所有端点，专门找 500 和越权

用法：
  ADMIN_PWD=ma .venv/bin/python tools/probe_all.py
退出码 0 = 无 500 且无越权。
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

BASE = os.environ.get("BASE", "http://127.0.0.1:8000").rstrip("/")
ADMIN_PWD = os.environ.get("ADMIN_PWD", "")

# 无参 GET，用于三档身份横向对比
COMMON_GET = [
    "/", "/login", "/terms", "/recharge",
    "/api/meta/config", "/api/meta/health",
    "/api/billing/rules", "/api/export/kinds",
    "/api/auth/me",
    "/api/tasks", "/api/billing/ledger", "/api/recharge/orders",
    "/api/recharge/info",
    "/api/billing/estimate?pages=10&mode=std",
    "/admin/api/dashboard", "/admin/api/users", "/admin/api/recharge",
    "/admin/api/ledger", "/admin/api/ledger/daily", "/admin/api/integrity",
    "/admin/api/tasks", "/admin/api/exports/health", "/admin/api/settings",
    "/admin/api/quality/stats", "/admin/api/cleanup/records", "/admin/api/logs",
    "/admin/api/events",
]

# SSR 页面（鉴权层在 main.py，走 302）
SSR_NEED_LOGIN = ["/account", "/admin"]


def latest_ids() -> dict[str, str]:
    """从库里取几个真实 id，供带参端点使用。"""
    db = Path(__file__).resolve().parent.parent / "data" / "app.db"
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    out: dict[str, str] = {}
    r = c.execute("select id from tasks order by created_at desc limit 1").fetchone()
    if r:
        out["task_id"] = r["id"]
    r = c.execute("select id from files order by rowid desc limit 1").fetchone()
    if r:
        out["file_id"] = r["id"]
    r = c.execute("select id from recharge_orders order by rowid desc limit 1").fetchone()
    if r:
        out["order_id"] = r["id"]
    r = c.execute("select id from upload_sessions order by rowid desc limit 1").fetchone()
    if r:
        out["upload_id"] = r["id"]
    r = c.execute("select id from users where role='admin' limit 1").fetchone()
    if r:
        out["user_id"] = r["id"]
    c.close()
    return out


PARAM_GET = [
    "/admin/api/users/{user_id}",
    "/api/tasks/{task_id}",
    "/api/tasks/{task_id}/exports",
    "/api/tasks/{task_id}/preview",
    "/api/tasks/{task_id}/pages/1/blocks",
    "/api/tasks/{task_id}/pages/1/image",
    "/api/upload/session/{upload_id}",
    "/api/exports/{file_id}/download",
    "/pay/qr/alipay",
    "/task/{task_id}",
]


def login(client: httpx.Client, user: str, pwd: str) -> bool:
    r = client.post("/api/auth/login", json={"username": user, "password": pwd})
    return r.status_code == 200 and r.json().get("ok")


def register_temp_user(client: httpx.Client) -> tuple[str, str]:
    """注册一个一次性普通用户（顺便覆盖注册链路）。"""
    import time

    name = f"probe{int(time.time()) % 10**7}"
    pwd = "Probe!2345"
    r = client.post(
        "/api/auth/register",
        json={"username": name, "password": pwd, "agree": True},
    )
    if r.status_code != 200 or not r.json().get("ok"):
        raise SystemExit(f"注册失败: {r.status_code} {r.text[:300]}")
    return name, pwd


def probe(client: httpx.Client, paths: list[str], ids: dict[str, str]) -> dict[str, tuple[int, str]]:
    out: dict[str, tuple[int, str]] = {}
    for p in paths:
        url = p
        missing = False
        for k, v in ids.items():
            if "{" + k + "}" in url:
                url = url.replace("{" + k + "}", v)
        if "{" in url:
            out[p] = (-1, "缺参数")
            continue
        try:
            r = client.get(url)
        except Exception as e:  # noqa: BLE001
            out[p] = (-2, f"{type(e).__name__}: {e}")
            continue
        body = ""
        if r.status_code >= 500:
            body = r.text[:400].replace("\n", " ")
        out[p] = (r.status_code, body)
        _ = missing
    return out


def fmt(code: int) -> str:
    if code == -1:
        return "skip"
    if code == -2:
        return "ERR"
    return str(code)


def main() -> int:
    ids = latest_ids()
    print(f"BASE={BASE}  可用 id: {sorted(ids)}\n")

    sessions: dict[str, httpx.Client] = {}
    for name in ("anon", "user", "admin"):
        sessions[name] = httpx.Client(base_url=BASE, timeout=60.0, follow_redirects=False)

    # 普通用户：注册后自动登录
    uname, upwd = register_temp_user(sessions["user"])
    sessions["user"].post("/api/auth/login", json={"username": uname, "password": upwd})

    if not ADMIN_PWD:
        print("!! 未提供 ADMIN_PWD，管理员档会全是 401")
    else:
        if not login(sessions["admin"], "admin", ADMIN_PWD):
            print("!! 管理员登录失败")
            return 2

    all_paths = COMMON_GET + PARAM_GET + SSR_NEED_LOGIN
    results = {n: probe(c, all_paths, ids) for n, c in sessions.items()}

    # 表格
    w = max(len(p) for p in all_paths) + 2
    print(f"{'path'.ljust(w)}{'anon':>6}{'user':>6}{'admin':>6}   备注")
    print("-" * (w + 30))
    problems: list[str] = []
    for p in all_paths:
        a = results["anon"][p]
        u = results["user"][p]
        d = results["admin"][p]
        note = ""
        for tag, code, body in (("anon", a[0], a[1]), ("user", u[0], u[1]), ("admin", d[0], d[1])):
            if code >= 500:
                problems.append(f"[500] {tag} {p} -> {body[:200]}")
                note = "500!"
            elif code == -2:
                problems.append(f"[ERR] {tag} {p} -> {body[:200]}")
                note = "ERR"
        # 越权：普通用户不应拿到后台数据
        if p.startswith("/admin") and u[0] in (200, 500):
            problems.append(f"[越权] 普通用户可访问 {p} -> {u[0]}")
            note += " 越权!"
        print(f"{p.ljust(w)}{fmt(a[0]):>6}{fmt(u[0]):>6}{fmt(d[0]):>6}   {note}")

    print()
    if problems:
        print(f"发现 {len(problems)} 个问题：")
        for x in problems:
            print("  " + x)
        return 1
    print("全部通过：无 5xx，无越权。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
