"""时间、金额、ID 等基础工具。

铁律：
- 金额一律「厘」（整数），禁止 float 参与计算。
- 时间一律 UTC ISO-8601 字符串（与 DDL 一致，避免 sqlite 时区坑）。
"""
from __future__ import annotations

import hashlib
import json
import secrets
import unicodedata
import uuid
from datetime import datetime, timedelta, timezone


_RADICAL_MAP_CACHE: dict[str, str] | None = None


def _radical_map() -> dict[str, str]:
    """加载 CJK 部首补充区（U+2E80–U+2EFF）→ 等价汉字的映射。

    NFKC 只覆盖康熙部首区（U+2F00–U+2FDF），补充区**没有任何分解**：
    `⻩`(U+2EE9) NFKC 后还是 `⻩`。所以补充区靠一份由思源宋体 cmap
    导出的等价表（同一 glyph 被多个码位引用即视为同一字）。
    表里没有的（约 36 个纯偏旁，如 ⺀ ⻄ ⻊）保持原样——宁可留怪字，
    也不要做没有依据的猜字映射。
    """
    global _RADICAL_MAP_CACHE
    if _RADICAL_MAP_CACHE is None:
        import json
        from pathlib import Path

        path = Path(__file__).resolve().parent / "data" / "radical_map.json"
        try:
            pairs = json.loads(path.read_text(encoding="utf-8"))["pairs"]
            _RADICAL_MAP_CACHE = {pairs[i]: pairs[i + 1] for i in range(0, len(pairs) - 1, 2)}
        except Exception:  # noqa: BLE001 - 表缺失不该阻塞识别，退化到只做 NFKC
            _RADICAL_MAP_CACHE = {}
    return _RADICAL_MAP_CACHE


def fix_radicals(s: str) -> str:
    """把 CJK 部首字符还原成正常汉字：⻩ → 黄、⼆ → 二、⻚ → 页。

    OCR 供应商经常输出康熙部首码位（U+2E80–U+2FDF）。字形与汉字完全一样，
    但身份是「部首」：搜不到、粘贴到 Word 是怪字、字数统计也乱。
    逐字处理，**不能整串 NFKC**——整串 NFKC 会把全角标点 `，；：` 折成半角，
    而中文标点是交付稿排版的一部分。
    """
    if not any(0x2E80 <= ord(ch) <= 0x2FDF for ch in s):
        return s
    table = _radical_map()
    out = []
    for ch in s:
        if 0x2E80 <= ord(ch) <= 0x2FDF:
            fixed = unicodedata.normalize("NFKC", ch)
            if fixed == ch:
                fixed = table.get(ch, ch)
            out.append(fixed)
        else:
            out.append(ch)
    return "".join(out)


# ---------------------------------------------------------------- 时间
def now() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return now().replace(microsecond=0).isoformat()


def iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def hours_later(hours: float) -> str:
    return iso(now() + timedelta(hours=hours))


def minutes_later(minutes: float) -> str:
    return iso(now() + timedelta(minutes=minutes))


def is_expired(s: str | None) -> bool:
    dt = parse_iso(s)
    return bool(dt and dt <= now())


# ---------------------------------------------------------------- ID
def new_id() -> str:
    return str(uuid.uuid4())


def short_id(n: int = 8) -> str:
    return uuid.uuid4().hex[:n]


def new_token(nbytes: int = 24) -> str:
    return secrets.token_urlsafe(nbytes)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def sha256_file(path: str, chunk: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


# ---------------------------------------------------------------- 金额（厘）
def mill_to_yuan(mill: int) -> str:
    """1234 厘 → '1.23'（展示保留 2 位，四舍五入由整数运算完成）。"""
    sign = "-" if mill < 0 else ""
    m = abs(int(mill))
    return f"{sign}{m // 1000}.{(m % 1000) // 10:02d}"


def mill_to_yuan_short(mill: int) -> str:
    """1234 厘 → '¥1.234'（流水明细用，3 位精度，ceil 后正好 3 位）。"""
    m = int(mill)
    sign = "-" if m < 0 else ""
    m = abs(m)
    return f"{sign}¥{m // 1000}.{m % 1000:03d}"


def yuan_to_mill(yuan: float | str) -> int:
    """'1.23' → 1230 厘。仅用于表单输入解析，解析后即转为整数。"""
    from decimal import Decimal

    return int((Decimal(str(yuan)) * 1000).to_integral_value())


# ---------------------------------------------------------------- JSON
def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def loads(s: str | None, default=None):
    if not s:
        return default
    try:
        return json.loads(s)
    except (ValueError, TypeError):
        return default
