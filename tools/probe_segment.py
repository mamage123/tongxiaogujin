"""大文件分段建任务验证：1296 页 PDF 取 1-200 页能不能正常建，建完立刻取消。

只验接口层（页数、页码表达式、预估、扣费准入），不真等识别 ——
200 页按实测 1.5 分钟/页要 5 小时，那是另一个量级的事。

用法：ADMIN_PWD=ma .venv/bin/python tools/probe_segment.py test/test.pdf
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

BASE = os.environ.get("BASE", "http://127.0.0.1:8000").rstrip("/")
ROOT = Path(__file__).resolve().parent.parent
CHUNK = 4 * 1024 * 1024


def main() -> int:
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "test" / "test.pdf"
    if not src.exists():
        print(f"找不到 {src}")
        return 2
    blob = src.read_bytes()
    print(f"样本 {src.name}  {len(blob)/1024/1024:.1f}MB\n")

    c = httpx.Client(base_url=BASE, timeout=120)
    name = f"seg{int(time.time()) % 10**7}"
    r = c.post("/api/auth/register", json={"username": name, "password": "Seg!23456", "agree": True})
    assert r.status_code == 200, r.text

    # 给点余额，否则 200 页 > 5 页免费额度且余额 0 会被 E_NO_BALANCE 拦
    from sqlalchemy import text

    from app.db import q1, tx
    from app.services.billing import credit

    uid = q1("SELECT id FROM users WHERE username=:u", u=name)["id"]
    with tx() as conn:
        credit(conn, uid, 50000, ref_type="test_seed", ref_id=uid,
               remark="分段验证用余额", ltype="adjust")
    print(f"测试用户 {name}  余额 ¥50\n")

    # 上传
    r = c.post("/api/upload/init", json={"filename": src.name, "size": len(blob)})
    assert r.status_code == 200, r.text
    up = r.json()["data"]["upload_id"]
    total = (len(blob) + CHUNK - 1) // CHUNK
    for i in range(total):
        part = blob[i * CHUNK : (i + 1) * CHUNK]
        rr = c.post(
            f"/api/upload/chunk?upload_id={up}&index={i}",
            files={"file": ("blob", part, "application/octet-stream")},
        )
        assert rr.status_code == 200, rr.text
    r = c.post("/api/upload/complete", json={"upload_id": up})
    assert r.status_code == 200, r.text
    print(f"上传完成（{total} 片）")

    # 整本超限 → 必须被拒
    r = c.post("/api/tasks", json={"upload_id": up, "mode": "ancient", "page_range": "all"})
    print(f"整本建任务：{r.status_code}  {r.json().get('code')}  {r.json().get('message','')[:50]}")
    assert r.status_code == 400 and r.json()["code"] == "E_PAGE_LIMIT", "整本应被页数上限拦住"

    # 分段 1-200 → 必须成功
    r = c.post("/api/tasks", json={"upload_id": up, "mode": "ancient", "page_range": "1-200"})
    print(f"分段 1-200 建任务：{r.status_code}")
    assert r.status_code == 200, r.text
    created = r.json()["data"]        # 只有创建响应带 estimate
    tid = created["id"]
    detail = c.get(f"/api/tasks/{tid}").json()["data"]   # 只有详情带 pages 列表
    pages = detail["pages"]

    print(f"  pages_total = {created['pages_total']}")
    print(f"  pages 条数  = {len(pages)}")
    print(f"  预估        = {created['estimate']['pages']} 页 / {created['estimate']['estimated_amount_text']}")
    print(f"  状态        = {created['status']}")

    ok = True
    if created["pages_total"] != 200:
        print(f"  !! 期望 200 页，实际 {created['pages_total']}")
        ok = False
    if len(pages) != 200:
        print(f"  !! 页记录条数不对：{len(pages)}")
        ok = False
    if pages[0]["page_no"] != 1 or pages[-1]["page_no"] != 200:
        print("  !! 首页/末页不对")
        ok = False
    if created["estimate"]["pages"] != 200:
        print(f"  !! 预估页数不对：{created['estimate']['pages']}")
        ok = False

    # 立刻取消，不占队列也不等识别
    r = c.post(f"/api/tasks/{tid}/cancel")
    print(f"取消：{r.status_code}  {r.json().get('data',{}).get('status')}")
    if r.status_code != 200:
        ok = False

    # 取消后余额不应该被扣（还没结算）
    bal = q1("SELECT balance_mill FROM users WHERE id=:u", u=uid)["balance_mill"]
    print(f"取消后余额 ¥{int(bal)/1000:.2f}（应仍为 50.00）")
    if int(bal) != 50000:
        print("  !! 未结算的任务被扣费了")
        ok = False

    print()
    print("分段建任务验证通过。" if ok else "有问题，见上面 !!")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
