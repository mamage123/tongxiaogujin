"""页码范围：解析、压缩、以及「大 PDF 不能被截断」的回归。

`tasks.page_range` 建表时的注释是 `'all' 或 '1-50'`，即**表达式**。2026-10-28
之前建任务却把展开后的页号列表塞进去再 `[:200]` 截断：113 页的书展开成
`1,2,3,...,113` 要 380 字符，截断后 worker 只读到 70 个页号 —— 后半本书
静默消失，而且照 70 页计费（用户付了钱少拿内容，还看不出来）。
test6.pdf（113 页）实测复现。
"""
from __future__ import annotations

import io

import pymupdf

from app.services.preprocess import compact_page_range, parse_page_range
from app.services.worker import _selected_pages
from app.db import tx
from sqlalchemy import text


# ---------------------------------------------------------------- 纯函数
def test_compact_continuous_range():
    assert compact_page_range(list(range(1, 114))) == "1-113"


def test_compact_keeps_gaps():
    assert compact_page_range([1, 2, 3, 5, 7, 8, 9]) == "1-3,5,7-9"
    assert compact_page_range([4]) == "4"
    assert compact_page_range([]) == "all"


def test_compact_roundtrip_is_lossless():
    """压缩必须可逆——它是写进库的持久表达式，丢一页就是丢一页内容。"""
    pages = [1, 2, 3, 10, 11, 12, 15, 40, 41, 42, 200]
    assert parse_page_range(compact_page_range(pages), 200) == pages


def test_compact_fits_in_200_chars_for_thousands_of_pages():
    """schema 里这列按 200 字符用，压缩后 5000 页也只有几十个字符。"""
    tiny = compact_page_range(list(range(1, 5001)))
    assert len(tiny) < 200
    assert tiny == "1-5000"


def test_worker_understands_range_syntax():
    """worker 侧必须认 `1-50` 这种区间写法（以前只 split 逗号，区间会解析成空）。"""
    assert _selected_pages({"page_range": "1-50"}, 113) == list(range(1, 51))
    assert _selected_pages({"page_range": "all"}, 3) == [1, 2, 3]
    assert _selected_pages({"page_range": ""}, 2) == [1, 2]
    # 旧数据是展开的逗号列表，也要继续认
    assert _selected_pages({"page_range": "2,4"}, 5) == [2, 4]
    # 解析不了就退回全部页：建页失败会让整个任务失败，代价太大
    assert _selected_pages({"page_range": "乱写的"}, 4) == [1, 2, 3, 4]


# ---------------------------------------------------------------- 接口回归
def _pdf_bytes(pages: int) -> bytes:
    doc = pymupdf.open()
    for _ in range(pages):
        doc.new_page(width=595, height=842)  # A4 空白
    blob = doc.tobytes()
    doc.close()
    return blob


def test_large_pdf_keeps_every_page(client):
    """113 页 PDF 建任务后页数必须还是 113（不能被 200 字符截断成 70）。"""
    from tests.conftest import upload

    client.post(
        "/api/auth/register",
        json={"username": "bigpdf", "password": "pass12345", "agree": True},
    )
    me = client.get("/api/auth/me").json()["data"]
    # 113 页要付费，先给够余额（直接记账，避免再走一遍后台接口）
    from app.services.billing import credit

    with tx() as conn:
        credit(conn, me["id"], 20000, ref_type="test_seed", ref_id=me["id"],
               remark="大 PDF 测试", ltype="adjust")

    up = upload(client, data=_pdf_bytes(113), filename="big.pdf")
    r = client.post("/api/tasks", json={"upload_id": up, "mode": "ancient", "page_range": "all"})
    assert r.status_code == 200, r.text
    tid = r.json()["data"]["id"]

    row = client.get(f"/api/tasks/{tid}").json()["data"]
    assert row["pages_total"] == 113, f"页数被截断了：{row['pages_total']}"
    assert len(row["pages"]) == 113
    assert row["pages"][-1]["page_no"] == 113
