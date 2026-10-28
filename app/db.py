"""数据库访问层（SQLAlchemy Core + 原生 SQL）。

为什么不用 ORM 声明式模型：
1. 表结构的唯一事实源是 `schema.sql`（与《开发规格 §3》逐字对齐，含 CHECK / 部分唯一索引）；
   ORM 再声明一遍等于两份事实源，最容易漂移。
2. 本项目查询都很短、都在 service 里，`text()` + dict 行映射足够，且 AI 改写成本最低。
3. 事务边界由 `with tx() as conn` 显式控制，资金场景必须看得见边界。

使用：
    from .db import q, q1, tx
    rows = q("SELECT * FROM tasks WHERE user_id=:u", u=user_id)
    with tx() as conn:
        conn.execute(text("UPDATE ..."), {...})
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Connection, Engine

from .config import settings

_engine: Engine | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is not None:
        return _engine

    url = settings.database_url
    if url.startswith("sqlite"):
        # 确保父目录存在
        raw = url.replace("sqlite:///", "").replace("sqlite://", "")
        if raw and raw != ":memory:":
            Path(raw).parent.mkdir(parents=True, exist_ok=True)
        _engine = create_engine(
            url,
            future=True,
            connect_args={"check_same_thread": False, "timeout": 30},
        )

        @event.listens_for(_engine, "connect")
        def _set_sqlite_pragma(dbapi_conn, _rec):  # noqa: ANN001
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.close()

    else:
        _engine = create_engine(url, future=True, pool_pre_ping=True)
    return _engine


def reset_engine() -> None:
    """测试用：切换 DATABASE_URL 后重置引擎。"""
    global _engine
    if _engine is not None:
        _engine.dispose()
    _engine = None


# ---------------------------------------------------------------- 查询
def _rows(result) -> list[dict[str, Any]]:
    return [dict(r._mapping) for r in result]


def q(sql: str, **params: Any) -> list[dict[str, Any]]:
    with get_engine().connect() as conn:
        return _rows(conn.execute(text(sql), params))


def q1(sql: str, **params: Any) -> dict[str, Any] | None:
    rows = q(sql, **params)
    return rows[0] if rows else None


def scalar(sql: str, **params: Any) -> Any:
    with get_engine().connect() as conn:
        return conn.execute(text(sql), params).scalar()


# ---------------------------------------------------------------- 事务
@contextmanager
def tx():
    """显式事务：正常退出提交，异常回滚。资金操作必须包在这里面。"""
    with get_engine().begin() as conn:
        yield conn


def exec_in(conn: Connection, sql: str, params: dict[str, Any] | Iterable[dict[str, Any]] | None = None):
    return conn.execute(text(sql), params or {})


# ---------------------------------------------------------------- 初始化
def init_db(seed: bool = True) -> None:
    """执行 schema.sql（幂等，全部 CREATE ... IF NOT EXISTS）+ 写入初始配置。"""
    schema = Path(settings.schema_path).read_text(encoding="utf-8")
    raw = get_engine().raw_connection()
    try:
        # 用底层连接执行多语句脚本（executescript 只在 sqlite3 上有）
        if isinstance(raw, sqlite3.Connection):
            raw.executescript(schema)
            raw.commit()
        else:  # pragma: no cover - 未来 Postgres 时改走 alembic
            with get_engine().begin() as conn:
                for stmt in schema.split(";"):
                    if stmt.strip():
                        conn.execute(text(stmt))
    finally:
        raw.close()

    if seed:
        seed_settings()
        seed_font_assets()
    _migrate_columns()


# 轻量迁移：schema.sql 全是 CREATE ... IF NOT EXISTS，已存在的库不会补新列。
# 加列必须同时登记在这里，否则老库升级后会因「no such column」直接 500。
_ADDED_COLUMNS = [
    ("tasks", "ocr_provider", "TEXT NULL"),
    ("files", "render_version", "INTEGER NOT NULL DEFAULT 0"),
]


def _migrate_columns() -> None:
    """给已存在的表补新列（幂等，Postgres 与 SQLite 都支持 ADD COLUMN）。"""
    engine = get_engine()
    with engine.begin() as conn:
        for table, column, decl in _ADDED_COLUMNS:
            try:
                rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
                if rows:
                    existing = {r[1] for r in rows}
                else:  # pragma: no cover - Postgres
                    existing = {
                        r[0]
                        for r in conn.execute(
                            text(
                                "SELECT column_name FROM information_schema.columns"
                                " WHERE table_name=:t"
                            ),
                            {"t": table},
                        )
                    }
                if column not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {decl}"))
            except Exception:  # noqa: BLE001 - 迁移失败不能挡住启动，但要留痕
                import logging

                logging.getLogger(__name__).warning(
                    "补列失败 %s.%s（可能已存在或权限不足）", table, column, exc_info=True
                )


DEFAULT_SETTINGS = {
    "bbox_global_enabled": "1",
    "bbox_gray_ratio": "1.0",
    "price_per_10k_mill": "1000",
    "min_bill_chars": "1000",
    "free_pages_default": "5",
    "max_pages_normal": "200",
    "max_pages_low_balance": "10",
    "low_balance_mill": "1000",
    "circuit_break_ratio": "1.5",
    "ai_label": "本文件由 AI 识别生成，供参考。",
}


def seed_settings() -> None:
    from .utils import now_iso

    with tx() as conn:
        for k, v in DEFAULT_SETTINGS.items():
            conn.execute(
                text("INSERT INTO settings(key,value,updated_at) VALUES(:k,:v,:t) ON CONFLICT(key) DO NOTHING"),
                {"k": k, "v": v, "t": now_iso()},
            )


# 字体白名单（FR-EXP-07/13）：只有这三个文件允许进入渲染服务
DEFAULT_FONTS = [
    {
        "name": "KingHwaOldSong",
        "filename": "KingHwa_OldSong.ttf",
        "role": "body",
        "license_grade": "B",
        "license_url": "",
        "archive_url": "",
    },
    {
        "name": "SourceHanSerifSC",
        # 用 TTF 而不是官方分发的 OTF：Chromium 打印 PDF 时无法子集化
        # CID-keyed CFF，会退化成「每个字一个 Type3 字体」（体积虚高、
        # 文本层变差）。同名 TTF（otf2ttf 转换，字形与度量不变）能正常
        # 嵌入为 Type0。见 app/services/export/fonts.py::_is_cid_cff
        "filename": "SourceHanSerifSC-Regular.ttf",
        "role": "fallback",
        "license_grade": "A",
        "license_url": "https://scripts.sil.org/OFL",
        "archive_url": "https://github.com/adobe-fonts/source-han-serif/releases/tag/2.003R",
    },
    {
        "name": "SourceHanSerifTC",
        "filename": "SourceHanSerifTC-Regular.ttf",
        "role": "fallback",
        "license_grade": "A",
        "license_url": "https://scripts.sil.org/OFL",
        "archive_url": "https://github.com/adobe-fonts/source-han-serif/releases/tag/2.003R",
    },
]


def seed_font_assets() -> None:
    """登记白名单字体。sha256 由 `python -m app.cli fonts-check` 回填。"""
    from .utils import now_iso

    with tx() as conn:
        for f in DEFAULT_FONTS:
            path = settings.font_dir / f["filename"]
            sha = ""
            if path.exists():
                from .utils import sha256_file

                sha = sha256_file(str(path))
            conn.execute(
                text(
                    "INSERT INTO font_assets(name,filename,role,license_grade,sha256,license_url,archive_url,captured_at,enabled)"
                    " VALUES(:n,:f,:r,:g,:s,:lu,:au,:ca,1) ON CONFLICT(name) DO NOTHING"
                ),
                {
                    "n": f["name"],
                    "f": f["filename"],
                    "r": f["role"],
                    "g": f["license_grade"],
                    "s": sha,
                    "lu": f["license_url"],
                    "au": f["archive_url"],
                    "ca": now_iso() if sha else None,
                },
            )


# ---------------------------------------------------------------- 配置读取
def get_setting(key: str, default: str = "") -> str:
    row = q1("SELECT value FROM settings WHERE key=:k", k=key)
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    from .utils import now_iso

    with tx() as conn:
        conn.execute(
            text("INSERT INTO settings(key,value,updated_at) VALUES(:k,:v,:t)"
                 " ON CONFLICT(key) DO UPDATE SET value=:v, updated_at=:t"),
            {"k": key, "v": value, "t": now_iso()},
        )


def setting_int(key: str, default: int) -> int:
    try:
        return int(get_setting(key, str(default)))
    except (TypeError, ValueError):
        return default


def setting_float(key: str, default: float) -> float:
    try:
        return float(get_setting(key, str(default)))
    except (TypeError, ValueError):
        return default
