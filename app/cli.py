"""命令行工具：初始化、建库、字体体检、清理、V0 验证。

用法：
  python -m app.cli init            初始化数据库 + 管理员 + 字体登记
  python -m app.cli fonts           字体准入体检（SHA256 + 竖排能力 + 简体覆盖）
  python -m app.cli cleanup         执行 72h 清理
  python -m app.cli reconcile       打印今日对账
  python -m app.cli integrity       账实核对
  python -m app.cli v0 <pdf>        验证 PDF 是否被供应商截断（V0）
  python -m app.cli mock            生成一页测试图并跑通识别→结算（离线自测）
"""
from __future__ import annotations

import sys
from pathlib import Path


def cmd_init() -> int:
    from .db import init_db
    from .main import ensure_admin

    init_db(seed=True)
    ensure_admin()
    print("初始化完成：schema.sql 已执行，settings 与 font_assets 已写入")
    return 0


def cmd_fonts() -> int:
    from sqlalchemy import text

    from .db import tx
    from .services.export.fonts import FORBIDDEN_FAMILIES, load_font_assets
    from .utils import now_iso, sha256_file

    register = "--register" in sys.argv[2:]
    assets = load_font_assets()
    if not assets:
        print("font_assets 为空，请先执行 `python -m app.cli init`")
        return 1
    print(f"{'名称':<20}{'文件':<34}{'角色':<10}{'授权':<6}{'字节':>12}  SHA256")
    print("-" * 110)
    ok = 0
    drift = 0
    for a in assets:
        size = a.path.stat().st_size if a.exists else 0
        sha = a.sha256[:16] + "…" if a.sha256 else "（未登记）"
        print(f"{a.name:<20}{a.filename:<34}{a.role:<10}{a.license_grade:<6}{size:>12}  {sha}")
        if a.exists:
            ok += 1
            real = sha256_file(str(a.path))
            if register:
                with tx() as conn:
                    conn.execute(
                        text("UPDATE font_assets SET sha256=:s, captured_at=:c WHERE name=:n"),
                        {"s": real, "c": now_iso(), "n": a.name},
                    )
                print(f"{'':<20}  已登记 SHA256 = {real[:32]}…")
            elif a.sha256 and a.sha256 != real:
                # 授权台账靠 SHA256 认文件：对不上说明换过字体，必须人工确认授权
                drift += 1
                print(f"{'':<20}  ⚠ 与登记值不一致（实际 {real[:16]}…），核对授权后用 --register 重登记")
    print()
    if ok == 0:
        print("⚠ 三个字体文件都不在 fonts/ 目录。缺字体时导出会退化为系统 serif，")
        print("  后果不只是「观感变差」：PDF 文本层会出现康熙部首码位（复制出来搜不到），")
        print("  并且每页几十个 Type3 碎片字体。可用 tools/audit_pdf_text.py 复现。")
        print("  请把白名单字体放进 fonts/，再执行 `python -m app.cli fonts --register`。")
        return 2
    print(f"可用字体：{ok} / {len(assets)}")
    if drift:
        print(f"⚠ {drift} 个文件的 SHA256 与台账不符，授权状态需要重新确认。")
    print(f"禁用字族（出现即构建失败）：{', '.join(FORBIDDEN_FAMILIES[:6])} … 共 {len(FORBIDDEN_FAMILIES)} 项")
    print("\n提示：逐字竖排能力（是否有 vert/vmtx）用 tools/check_fonts.py 检测：")
    print("  .venv/bin/python tools/check_fonts.py fonts/*.ttf fonts/*.otf")
    return 0


def cmd_cleanup() -> int:
    from .services.cleanup import cleanup_expired, purge_blocks

    r = cleanup_expired()
    n = purge_blocks()
    print(f"清理：{r['cleaned']} 个任务，失败 {r['failed']}，释放 {r['freed_bytes'] / 1024 / 1024:.2f} MB")
    print(f"清除 90 天前的块结构：{n} 行")
    return 0


def cmd_reconcile() -> int:
    from datetime import datetime, timezone

    from .services.billing import daily_reconcile
    from .utils import mill_to_yuan

    d = sys.argv[2] if len(sys.argv) > 2 else datetime.now(timezone.utc).strftime("%Y-%m-%d")
    r = daily_reconcile(d)
    print(f"对账日期 {d}")
    print(f"  期初   ¥{mill_to_yuan(r['begin_total'])}")
    print(f"  + 充值 ¥{mill_to_yuan(r['recharge'])}")
    print(f"  - 消费 ¥{mill_to_yuan(-r['consume'])}")
    print(f"  + 退款 ¥{mill_to_yuan(r['refund'])}")
    print(f"  ± 调整 ¥{mill_to_yuan(r['adjust'])}")
    print(f"  = 期末 ¥{mill_to_yuan(r['end_total'])}")
    print(f"  校验：{'自洽' if r['balanced'] else '不平，需排查'}")
    return 0 if r["balanced"] else 1


def cmd_integrity() -> int:
    from .services.billing import check_ledger_integrity

    r = check_ledger_integrity()
    print(f"核对 {r['checked']} 个用户：{'账实相符' if r['ok'] else '发现不平'}")
    for m in r["mismatches"]:
        print(f"  {m['username']}: 余额 {m['balance_mill']} / 台账 {m['ledger_sum_mill']} / 差 {m['diff_mill']}")
    return 0 if r["ok"] else 1


def cmd_v0() -> int:
    """V0：验证 30 页 PDF 是否被静默截断为 10 页（v2.0 选型的头号风险）。"""
    if len(sys.argv) < 3:
        print("用法：python -m app.cli v0 <文件.pdf>")
        return 1
    from .config import settings
    from .services.ocr.xinghe import XingheProvider

    path = sys.argv[2]
    if not Path(path).exists():
        print(f"文件不存在：{path}")
        return 1
    if not settings.aistudio_token or not settings.aistudio_endpoint:
        print("未配置 AISTUDIO_TOKEN / AISTUDIO_ENDPOINT，无法调用星河 API")
        print("（可用 OCR_MOCK=1 python -m app.cli mock 先跑本地链路）")
        return 2

    p = XingheProvider()
    print(f"调用星河 API：{path}")
    pages = p.recognize_file(path, file_type=0)
    print(f"返回页数：{len(pages)}")
    print(f"每页块数：{[len(x.blocks) for x in pages]}")
    print(f"每页尺寸：{[(x.width, x.height) for x in pages[:3]]}")
    if len(pages) <= 10:
        print()
        print("⚠ 只返回 10 页或更少 —— 很可能命中「超过 10 页只处理前 10 页」的静默截断。")
        print("  对策：上传链路必须由本服务按页渲染后逐页调用（app/services/worker.py render_pages）。")
        return 3
    print()
    print("✓ 未发现截断，可整份提交；但仍建议按页调用以获得页级重试与流式输出。")
    return 0


def cmd_mock() -> int:
    """离线自测：造一张带文字的图 → 建任务 → 跑 worker → 结算 → 导出 Word。"""
    import os

    os.environ["OCR_MOCK"] = "1"
    from .config import reload_settings

    reload_settings()

    from PIL import Image, ImageDraw

    from .config import BASE_DIR, settings as s2
    from .db import init_db, q1, tx
    from .main import ensure_admin
    from .security import hash_password
    from .services.billing import credit
    from .services.export import export
    from .services.worker import init_pages, run_task
    from .utils import hours_later, new_id, now_iso, sha256_file

    from sqlalchemy import text

    init_db(seed=True)
    ensure_admin()

    tmp = s2.storage_dir / "_mock"
    tmp.mkdir(parents=True, exist_ok=True)
    img_path = tmp / "sample.png"
    im = Image.new("RGB", (1240, 1754), (250, 246, 238))
    d = ImageDraw.Draw(im)
    y = 160
    for i in range(10):
        d.line([(160, y + i * 140), (1080, y + i * 140)], fill=(120, 110, 95), width=22)
    im.save(img_path)

    uid = new_id()
    tid = new_id()
    with tx() as conn:
        conn.execute(
            text(
                "INSERT INTO users(id,username,pwd_hash,role,balance_mill,free_pages_total,"
                "free_pages_used,status,created_at) VALUES(:i,:u,:p,'user',0,5,0,'active',:c)"
            ),
            {"i": uid, "u": f"mock_{uid[:6]}", "p": hash_password("mockpass123"), "c": now_iso()},
        )
        task_dir = s2.storage_dir / "tasks" / tid
        task_dir.mkdir(parents=True, exist_ok=True)
        dest = task_dir / "source.png"
        im.save(dest)
        conn.execute(
            text(
                "INSERT INTO tasks(id,user_id,filename,file_hash,file_type,pages_total,page_range,mode,"
                "optimize,status,chars,estimated_chars,amount_mill,pending_mill,free_pages_applied,"
                "is_rerun,rerun_free,source_path,created_at,expire_at,dedup_bucket)"
                " VALUES(:id,:u,'sample.png',:h,'image',1,'all','ancient',1,'queued',0,400,0,0,0,0,0,:sp,:c,:e,:db)"
            ),
            {
                "id": tid, "u": uid, "h": sha256_file(str(dest)), "sp": str(dest),
                "c": now_iso(), "e": hours_later(72), "db": "mock",
            },
        )
        # 余额必须走 credit 写台账，否则 integrity 会判「账实不平」
        credit(conn, uid, 100_000, ref_type="mock_seed", ref_id=uid,
               remark="离线自测初始余额", ltype="adjust")
    init_pages(tid)
    task = q1("SELECT * FROM tasks WHERE id=:id", id=tid)
    print("开始识别（mock）…")
    print("结果：", run_task(task))

    t = q1("SELECT * FROM tasks WHERE id=:id", id=tid)
    print(f"状态={t['status']} 字数={t['chars']} 扣费={t['amount_mill']} 厘 免费页={t['free_pages_applied']}")
    blocks = q1("SELECT COUNT(*) AS n FROM page_blocks WHERE task_id=:t", t=tid)
    print(f"块数={blocks['n']}")

    print("导出 Word …")
    r = export(tid, "horiz_docx", "simp", uid)
    print(f"  文件 {r['file_id']} 大小 {r['size'] / 1024:.1f} KB")
    print("导出 Markdown …")
    r2 = export(tid, "markdown", "simp", uid)
    print(f"  文件 {r2['file_id']} 大小 {r2['size']} B")
    print("\n完成。可用 `python -m app.cli integrity` 核对账实。")
    return 0


def cmd_resetpwd() -> int:
    """重置账号密码：`python -m app.cli resetpwd <用户名> [新密码]`。

    不给新密码就随机生成并打印。存在的意义很实际：管理员密码一旦忘记，
    整个后台（确认到账、看对账）就全锁死，而这是唯一不需要动数据库的救急方式。
    """
    import secrets

    from sqlalchemy import text

    from .db import q1, tx
    from .security import hash_password, password_problem

    if len(sys.argv) < 3:
        print("用法：python -m app.cli resetpwd <用户名> [新密码]")
        return 1
    username = sys.argv[2].strip()
    pwd = sys.argv[3] if len(sys.argv) > 3 else secrets.token_urlsafe(9)
    weak = password_problem(pwd)

    row = q1("SELECT id,username,role FROM users WHERE username=:u", u=username)
    if not row:
        print(f"没有这个账号：{username}")
        return 2

    with tx() as conn:
        conn.execute(text("UPDATE users SET pwd_hash=:p WHERE id=:i"), {"p": hash_password(pwd), "i": row["id"]})
        # 登录失败计数记在 login_attempts（scope=user:<名> / ip:<ip>），不在 users 表上
        conn.execute(text("DELETE FROM login_attempts WHERE scope=:s"), {"s": f"user:{username.lower()}"})
    print(f"已重置 {row['username']}（{row['role']}）的密码：{pwd}")
    print("同时清空了该账号的登录失败计数与锁定。请登录后尽快在后台改掉。")
    if weak:
        # 命令行是救急通道，刻意不拦短密码；但后台改密接口按 FR-ACC-01 卡 8 位，
        # 一旦在界面里改走，就再也设不回这个短密码，所以这里必须说清楚。
        print(f"注意：{weak}。此密码可以登录，但不满足注册/改密的强度策略，界面里改不回来。")
    return 0


def cmd_status() -> int:
    """一条命令看清「现在能不能用」：库、字体、OCR、PDF 引擎、队列、账、磁盘。"""
    import shutil

    from .config import settings
    from .db import q, q1
    from .services.billing import check_ledger_integrity
    from .services.export.fonts import load_font_assets
    from .services.export.pdf import engine_status

    def pad(s: str, width: int = 14) -> str:
        """按显示宽度补齐（中文算两列），否则标签会对不齐。"""
        w = sum(2 if ord(c) > 0x2E80 else 1 for c in s)
        return s + " " * max(0, width - w)

    def line(k: str, v: str, bad: bool = False) -> None:
        print(f"  {pad(k)}{'✗ ' if bad else '· '}{v}")

    print("通晓古今 · 运行状态")
    print("-" * 62)
    line("环境", f"env={settings.app_env}  db={settings.database_url}")
    line("OCR", f"主通道={settings.ocr_primary}  mock={'开' if settings.ocr_mock else '关'}",
         bad=settings.ocr_mock)

    assets = load_font_assets()
    ready = [a.name for a in assets if a.exists]
    line("字体", f"{len(ready)}/{len(assets)} 可用 {('· ' + ', '.join(ready)) if ready else '（未放 fonts/）'}",
         bad=not ready)

    eng = engine_status()
    line("PDF 引擎", ("就绪 " + eng.get("chromium", "")) if eng.get("ok") else eng.get("reason", "不可用"),
         bad=not eng.get("ok"))

    try:
        for label, sql in [
            ("用户", "SELECT COUNT(*) AS n FROM users"),
            ("任务", "SELECT COUNT(*) AS n FROM tasks"),
            ("待处理任务", "SELECT COUNT(*) AS n FROM tasks WHERE status IN ('queued','processing','preprocessing')"),
            ("待确认充值", "SELECT COUNT(*) AS n FROM recharge_orders WHERE status='submitted'"),
            ("过期未清", "SELECT COUNT(*) AS n FROM tasks WHERE status NOT IN ('expired','cancelled') AND expire_at < datetime('now')"),
        ]:
            row = q1(sql)
            n = int(row["n"]) if row else 0
            line(label, str(n), bad=(label in ("待确认充值", "过期未清") and n > 0))

        by = q("SELECT status, COUNT(*) AS n FROM tasks GROUP BY status ORDER BY n DESC")
        if by:
            line("状态分布", "  ".join(f"{r['status']}={r['n']}" for r in by))

        integ = check_ledger_integrity()
        line("账实", "相符" if integ["ok"] else f"不平（{len(integ['mismatches'])} 个用户）", bad=not integ["ok"])
    except Exception as e:  # noqa: BLE001 - 库没建时不该抛栈
        line("数据库", f"查询失败：{e}（先跑 python -m app.cli init）", bad=True)

    free = shutil.disk_usage(str(settings.storage_dir))
    line("磁盘剩余", f"{free.free / 1024 ** 3:.1f} GB / 总 {free.total / 1024 ** 3:.1f} GB")

    fails = q(
        "SELECT kind,error_code,created_at FROM export_logs WHERE success=0 ORDER BY created_at DESC LIMIT 5"
    )
    if fails:
        line("近期导出失败", "；".join(f"{r['created_at'][11:16]} {r['kind']}({r['error_code']})" for r in fails), bad=True)
    print("-" * 62)
    print("提示：python -m app.cli integrity 核对账实；python -m app.cli cleanup 清理过期文件")
    return 0


COMMANDS = {
    "init": cmd_init,
    "fonts": cmd_fonts,
    "cleanup": cmd_cleanup,
    "reconcile": cmd_reconcile,
    "integrity": cmd_integrity,
    "status": cmd_status,
    "resetpwd": cmd_resetpwd,
    "v0": cmd_v0,
    "mock": cmd_mock,
}


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        print("可用命令：" + ", ".join(COMMANDS))
        return 1
    return COMMANDS[sys.argv[1]]()


if __name__ == "__main__":
    sys.exit(main())
