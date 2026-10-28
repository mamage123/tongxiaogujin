"""双页合拍检测与拆分（M1b）。

为什么值得单独立一组：
古籍影印件大量是「一本书摊开拍一张」，一张图里塞了两个半页。不拆的后果
2026-10 实测过：整幅送 OCR 后每半页的有效分辨率只剩一半，两页的列序还会
互相穿插（右页末尾接着左页开头），2026-10-27 的 test4.png 整张识别 283 字、
拆开后 280 字但分页与顺序都对了。

检测算法在 2026-10 重写过一次，旧的对比度判据方向是反的（单页竖排的文字列
对比度 0.65 > 真双页 0.29），旧的 `w < h*1.1` 前置条件还会把 1000×910 的
合拍图直接挡掉。这两条都补了回归测试。
"""
from __future__ import annotations

import io

from PIL import Image, ImageDraw

from app.services.preprocess import detect_spread, split_spread
from app.services.worker import init_pages, render_pages
from tests.conftest import make_task
from sqlalchemy import text

from app.db import q, q1, tx


# ---------------------------------------------------------------- 造样张
def _spread_png(w: int = 1000, h: int = 900, left_edge: int = 478, right_edge: int = 503) -> bytes:
    """双页合拍：左右各一个版框，两个内边紧挨着（中缝双线）。"""
    im = Image.new("L", (w, h), 236)
    d = ImageDraw.Draw(im)
    d.rectangle([40, 60, left_edge, 840], outline=30, width=3)
    d.rectangle([right_edge, 60, w - 40, 840], outline=30, width=3)
    for x in range(90, left_edge - 20, 42):
        d.line([x, 110, x, 800], fill=70, width=2)
    for x in range(right_edge + 25, w - 70, 42):
        d.line([x, 110, x, 800], fill=70, width=2)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def _single_png(w: int = 1000, h: int = 1400) -> bytes:
    """单页竖排：一个版框，内部只有等距的文字列（列距远大于中缝间距）。"""
    im = Image.new("L", (w, h), 236)
    d = ImageDraw.Draw(im)
    d.rectangle([60, 60, w - 60, h - 60], outline=30, width=3)
    for x in range(110, w - 90, 72):
        d.line([x, 110, x, h - 110], fill=70, width=2)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def _solid_seam_png(w: int = 1000, h: int = 900) -> bytes:
    """双页合拍，但中缝被纸弯阴影糊成**一条**实心暗带（test8.png 的真实形态）。

    判据 A 找的是「一对间距极小的竖线」，这种图只数得到一条，曾经直接漏判：
    2026-10-28 用户拿 test8.png 来测，1000×830 的摊开合拍图被判成单页。
    """
    im = Image.new("L", (w, h), 236)
    d = ImageDraw.Draw(im)
    # 两页框线各离中线 47px，给装订缝两侧留出白纸边（这是判据 C 的关键特征）
    d.rectangle([40, 60, w // 2 - 50, 840], outline=30, width=3)
    d.rectangle([w // 2 + 50, 60, w - 40, 840], outline=30, width=3)
    # 实心缝：贯穿整页高度，模拟书脊阴影
    d.rectangle([w // 2 - 3, 0, w // 2 + 3, h - 1], fill=20)
    # 文字列要画成**断续**短段：贯通直列会被形态学开运算保留，
    # 于是「框线 + 相邻文字列」凑成一对 24px 的线，判据 A 先误命中。
    # 真实古籍的字本来就是断开的，这也是判据 A 成立的前提。
    for x in range(90, w // 2 - 70, 42):
        for y0 in range(110, 780, 38):
            d.line([x, y0, x, y0 + 30], fill=70, width=4)
    for x in range(w // 2 + 90, w - 70, 42):
        for y0 in range(110, 780, 38):
            d.line([x, y0, x, y0 + 30], fill=70, width=4)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def _dense_text_single_png(w: int = 1000, h: int = 900) -> bytes:
    """单页，正中一条界行、两侧紧邻就是密排文字（test9.png 的真实形态）。

    这条线是真的，但两侧不是白纸边，所以不能拆 —— 判据 C 就是靠这个把
    「装订缝」和「单页界行」分开的。
    """
    im = Image.new("L", (w, h), 236)
    d = ImageDraw.Draw(im)
    d.rectangle([60, 60, w - 60, 840], outline=30, width=3)
    d.line([w // 2, 80, w // 2, 820], fill=25, width=3)  # 正中界行，贯通
    # 密排文字：短竖段拼接，列内很暗但**不贯通**（开运算留不下，判据 A 不会误触）
    for x in range(80, w - 70, 20):
        for y0 in range(110, 780, 38):
            d.line([x, y0, x, y0 + 30], fill=60, width=6)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def _write(tmp_path, name: str, blob: bytes) -> str:
    p = tmp_path / name
    p.write_bytes(blob)
    return str(p)


# ---------------------------------------------------------------- 检测
def test_detect_single_page(tmp_path):
    """单页竖排不能被判成双页——误拆会把一页内容切成两半，比不拆更糟。"""
    ok, conf, seam = detect_spread(_write(tmp_path, "one.png", _single_png()))
    assert ok is False
    assert seam is None
    assert conf == 0.0


def test_detect_spread_finds_seam(tmp_path):
    ok, conf, seam = detect_spread(_write(tmp_path, "two.png", _spread_png()))
    assert ok is True
    assert conf > 0.5
    # 中缝落在中间双线之间，允许几个像素的容差
    assert 470 <= seam <= 515


def test_detect_spread_near_square_is_not_skipped(tmp_path):
    """1000×910（比例 1.10）曾被旧的 `w < h*1.1` 前置条件挡掉，差 1px 就漏。"""
    blob = _spread_png(w=1000, h=910)
    ok, _conf, _seam = detect_spread(_write(tmp_path, "square.png", blob))
    assert ok is True


def test_detect_spread_tolerates_broken_file(tmp_path):
    """检测失败不能挡住识别：读不了就当单页放行。"""
    p = tmp_path / "bad.png"
    p.write_bytes(b"not an image")
    assert detect_spread(str(p)) == (False, 0.0, None)


def test_detect_solid_seam_spread(tmp_path):
    """中缝糊成一条实心暗带时也要判成双页（判据 C）。

    2026-10-28 test8.png（1000×830 摊开合拍）实测漏判：判据 A 数不到一对线。
    """
    ok, conf, seam = detect_spread(_write(tmp_path, "solid.png", _solid_seam_png()))
    assert ok is True
    assert conf > 0.6
    assert 480 <= seam <= 520


def test_detect_dense_single_page_not_split(tmp_path):
    """单页正中有界行、两侧紧邻密排文字时不能误拆（test9.png 实测）。"""
    ok, conf, seam = detect_spread(_write(tmp_path, "dense.png", _dense_text_single_png()))
    assert ok is False
    assert seam is None
    assert conf == 0.0


def test_split_spread_keeps_both_halves(tmp_path):
    src = _write(tmp_path, "two.png", _spread_png())
    right, left = split_spread(src, 490, tmp_path / "out")
    for p in (right, left):
        with Image.open(p) as im:
            assert im.size[1] == 900
    with Image.open(right) as im:
        assert im.size[0] == 1000 - 490
    with Image.open(left) as im:
        assert im.size[0] == 490


# ---------------------------------------------------------------- 建页 / 渲染
def _task_with_source(env, tmp_path, blob: bytes, name: str = "spread.png") -> str:
    tid = make_task(make_adminish(), status="queued")
    src = _write(tmp_path, name, blob)
    with tx() as conn:
        conn.execute(
            text("UPDATE tasks SET source_path=:s, file_type='image', pages_total=1 WHERE id=:i"),
            {"s": src, "i": tid},
        )
    return tid


def make_adminish() -> str:
    from tests.conftest import make_user

    return make_user(username="spread_owner")


def test_init_pages_splits_spread_into_two(env, tmp_path):
    """双页合拍 → 两条页记录，右半在前（古籍右起），source_page_no 共享 1。"""
    tid = _task_with_source(env, tmp_path, _spread_png())
    n = init_pages(tid)
    assert n == 2

    pages = q("SELECT * FROM task_pages WHERE task_id=:t ORDER BY page_no", t=tid)
    assert [int(p["page_no"]) for p in pages] == [1, 2]
    assert [int(p["is_split_half"]) for p in pages] == [1, 2]
    assert {int(p["source_page_no"]) for p in pages} == {1}
    assert int(q1("SELECT pages_total FROM tasks WHERE id=:i", i=tid)["pages_total"]) == 2


def test_init_pages_keeps_single_page(env, tmp_path):
    tid = _task_with_source(env, tmp_path, _single_png(), "one.png")
    assert init_pages(tid) == 1
    page = q1("SELECT * FROM task_pages WHERE task_id=:t", t=tid)
    assert int(page["is_split_half"]) == 0


def test_render_pages_writes_each_half(env, tmp_path):
    tid = _task_with_source(env, tmp_path, _spread_png())
    init_pages(tid)
    render_pages(tid)

    pages = q("SELECT * FROM task_pages WHERE task_id=:t ORDER BY page_no", t=tid)
    dims = []
    for p in pages:
        with Image.open(p["image_path"]) as im:
            dims.append(im.size)
        assert int(p["img_width"]) == dims[-1][0]
    # 两半加起来应回到原图宽度（中缝那一列像素两半都不含，允许差 1~2px）
    assert sum(d[0] for d in dims) >= 998
    assert all(d[1] == 900 for d in dims)
    # 原图留档，供人工核对/撤销
    kept = tmp_path / "files" / "tasks" / tid / "pages" / "source_original.png"
    assert kept.exists()


def test_render_pages_single_page_copies_asis(env, tmp_path):
    tid = _task_with_source(env, tmp_path, _single_png(), "one.png")
    init_pages(tid)
    render_pages(tid)
    page = q1("SELECT * FROM task_pages WHERE task_id=:t", t=tid)
    with Image.open(page["image_path"]) as im:
        assert im.size == (1000, 1400)


# ---------------------------------------------------------------- 建任务的连带影响
def test_create_task_recounts_estimated_chars_for_spread(client):
    """双页图建任务后 estimated_chars 必须按拆分后的页数重算。

    不重算的后果：熔断线 = est×1.5，1 页按 400 字算 → 600 字就断，
    而拆开后两页正常能出 700+ 字，于是每张双页图都会误判成 partial。
    """
    from tests.conftest import upload

    client.post(
        "/api/auth/register",
        json={"username": "spreader", "password": "pass12345", "agree": True},
    )
    up = upload(client, data=_spread_png(), filename="two.png")
    r = client.post("/api/tasks", json={"upload_id": up, "mode": "ancient"})
    assert r.status_code == 200, r.text
    tid = r.json()["data"]["id"]

    task = q1("SELECT pages_total, estimated_chars FROM tasks WHERE id=:i", i=tid)
    assert int(task["pages_total"]) == 2
    # 新账号 5 页免费 → 2 页都在免费额度内，预估付费字数为 0
    assert int(task["estimated_chars"]) == 0

    pages = q("SELECT * FROM task_pages WHERE task_id=:t ORDER BY page_no", t=tid)
    assert [int(p["is_split_half"]) for p in pages] == [1, 2]

    detail = client.get(f"/api/tasks/{tid}").json()["data"]
    assert detail["spread_split"] is True
    assert [p["split_half"] for p in detail["pages"]] == [1, 2]


def test_create_task_estimate_uses_split_page_count(client):
    """建任务返回值里的预估也必须按拆后页数报。

    按原页数报的话，双页图用户看到「预估 1 页、不扣费」，
    结算时却是 2 页 —— 额度是不是被多扣，用户没法自己验证。
    """
    from tests.conftest import upload

    client.post(
        "/api/auth/register",
        json={"username": "estspread", "password": "pass12345", "agree": True},
    )
    up = upload(client, data=_spread_png(), filename="two2.png")
    r = client.post("/api/tasks", json={"upload_id": up, "mode": "ancient"})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["pages_total"] == 2
    assert data["estimate"]["pages"] == 2, "预估页数还在按原图算"


def test_single_page_task_not_flagged_as_spread(client):
    from tests.conftest import upload

    client.post(
        "/api/auth/register",
        json={"username": "single1", "password": "pass12345", "agree": True},
    )
    up = upload(client, data=_single_png(), filename="one.png")
    tid = client.post("/api/tasks", json={"upload_id": up}).json()["data"]["id"]
    detail = client.get(f"/api/tasks/{tid}").json()["data"]
    assert detail["spread_split"] is False
    assert detail["pages_total"] == 1


# ---------------------------------------------------------------- 决策留痕
def test_init_pages_records_split_decision(env, tmp_path):
    """拆了就要留痕：action / confidence / 中缝位置。

    `preprocess_records` 表建好了却一直没人写，双页判错时（test8 那种
    纸弯阴影把两条框线糊成一条带）事后完全无法复盘当时命中的是哪条判据。
    """
    import json

    tid = _task_with_source(env, tmp_path, _spread_png())
    init_pages(tid)

    rows = q("SELECT * FROM preprocess_records WHERE task_id=:t", t=tid)
    assert len(rows) == 1, "拆分决策没有留痕"
    rec = rows[0]
    assert rec["action"] == "split"
    assert rec["source_page_no"] == 1
    assert rec["confidence"] is not None and float(rec["confidence"]) > 0
    assert rec["reverted"] == 0
    seam = json.loads(rec["bbox"])
    assert len(seam) == 4 and seam[0] == seam[2]  # 竖直线 x 相同
    assert 400 < seam[0] < 600                    # 中缝在正中附近


def test_init_pages_records_no_split_decision(env, tmp_path):
    """没拆也要留痕——否则统计里只有「拆过」的，看不出漏拆率。"""
    tid = _task_with_source(env, tmp_path, _single_png(), "one.png")
    init_pages(tid)

    rows = q("SELECT * FROM preprocess_records WHERE task_id=:t", t=tid)
    assert len(rows) == 1
    assert rows[0]["action"] == "no_split"
    assert rows[0]["bbox"] is None


def test_init_pages_rewrite_does_not_duplicate_records(env, tmp_path):
    """重复建页（改判定后重跑同一任务）不该堆出一串旧记录。"""
    tid = _task_with_source(env, tmp_path, _spread_png())
    init_pages(tid)
    init_pages(tid)

    rows = q("SELECT * FROM preprocess_records WHERE task_id=:t", t=tid)
    assert len(rows) == 1

