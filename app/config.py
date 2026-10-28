"""配置：全部来自环境变量（见 .env.example）。

设计约束（开发规格 §11）：
- 本文件是唯一读取 os.environ 的地方，其他模块一律 `from .config import settings`。
- 所有金额字段都是「厘」（整数），环境变量也用厘表达，避免浮点。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path


def _env(key: str, default: str = "") -> str:
    v = os.environ.get(key)
    return default if v is None or v == "" else v


def _env_int(key: str, default: int) -> int:
    raw = _env(key, "")
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    raw = _env(key, "")
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """读取 .env（可选，零依赖）。

    规则：已存在的环境变量优先（容器/CI 注入的值不会被文件覆盖）；
    行内 `#` 注释与空行忽略；值两侧的引号会被去掉。
    """
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.split(" #")[0].strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


_load_dotenv(BASE_DIR / ".env")


@dataclass(frozen=True)
class Settings:
    # 基础
    app_env: str = field(default_factory=lambda: _env("APP_ENV", "dev"))
    app_secret: str = field(default_factory=lambda: _env("APP_SECRET", "dev-insecure-secret-change-me"))
    base_url: str = field(default_factory=lambda: _env("BASE_URL", "http://localhost:8000"))

    # 数据与存储
    database_url: str = field(
        default_factory=lambda: _env("DATABASE_URL", f"sqlite:///{BASE_DIR / 'data' / 'app.db'}")
    )
    storage_dir: Path = field(default_factory=lambda: Path(_env("STORAGE_DIR", str(BASE_DIR / "data" / "files"))))
    schema_path: Path = field(default_factory=lambda: BASE_DIR / "schema.sql")

    # OCR 供应商：星河 PaddleOCR-VL（异步 Job API）
    aistudio_token: str = field(default_factory=lambda: _env("AISTUDIO_TOKEN"))
    aistudio_endpoint: str = field(
        default_factory=lambda: _env(
            "AISTUDIO_ENDPOINT", "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs"
        )
    )
    aistudio_model: str = field(default_factory=lambda: _env("AISTUDIO_MODEL", "PaddleOCR-VL-1.6"))
    # 异步任务轮询：间隔太短浪费请求，太长拖慢单页
    aistudio_poll_interval_s: float = field(
        default_factory=lambda: _env_float("AISTUDIO_POLL_INTERVAL_S", 1.5)
    )
    # 整任务超时（提交→done）。30 页实测约 25 秒，单页 3~8 秒
    aistudio_job_timeout_s: int = field(default_factory=lambda: _env_int("AISTUDIO_JOB_TIMEOUT_S", 300))
    # 三个预处理开关默认全关。开了 orientation/unwarping 会改变坐标基准，
    # 校对版框层将相对本地原图整体偏移（详见 services/ocr/xinghe.py 模块头「坑 3」）
    aistudio_use_orientation: bool = field(
        default_factory=lambda: _env("AISTUDIO_USE_ORIENTATION", "0") == "1"
    )
    aistudio_use_unwarping: bool = field(
        default_factory=lambda: _env("AISTUDIO_USE_UNWARPING", "0") == "1"
    )
    aistudio_use_chart: bool = field(default_factory=lambda: _env("AISTUDIO_USE_CHART", "0") == "1")
    ocr_primary: str = field(default_factory=lambda: _env("OCR_PRIMARY", "xinghe"))
    ocr_fallback: str = field(default_factory=lambda: _env("OCR_FALLBACK", "glm"))
    glm_api_key: str = field(default_factory=lambda: _env("GLM_API_KEY"))
    glm_model: str = field(default_factory=lambda: _env("GLM_MODEL", "glm-ocr"))
    glm_endpoint: str = field(default_factory=lambda: _env("GLM_ENDPOINT", "https://open.bigmodel.cn/api/paas/v4/chat/completions"))
    ocr_timeout_s: int = field(default_factory=lambda: _env_int("OCR_TIMEOUT_S", 120))
    ocr_mock: bool = field(default_factory=lambda: _env("OCR_MOCK", "0") == "1")

    # 业务上限
    max_upload_mb: int = field(default_factory=lambda: _env_int("MAX_UPLOAD_MB", 500))
    max_pages: int = field(default_factory=lambda: _env_int("MAX_PAGES", 200))
    max_pages_low_balance: int = field(default_factory=lambda: _env_int("MAX_PAGES_LOW_BALANCE", 10))
    low_balance_mill: int = field(default_factory=lambda: _env_int("LOW_BALANCE_MILL", 1000))
    circuit_break_ratio: float = field(default_factory=lambda: _env_float("CIRCUIT_BREAK_RATIO", 1.5))
    file_ttl_hours: int = field(default_factory=lambda: _env_int("FILE_TTL_HOURS", 72))
    session_days: int = field(default_factory=lambda: _env_int("SESSION_DAYS", 7))
    dup_window_seconds: int = field(default_factory=lambda: _env_int("DUP_WINDOW_SECONDS", 60))

    # 计费默认值（可被 settings 表覆盖）
    price_per_10k_mill: int = field(default_factory=lambda: _env_int("PRICE_PER_10K_MILL", 1000))
    min_bill_chars: int = field(default_factory=lambda: _env_int("MIN_BILL_CHARS", 1000))
    free_pages_default: int = field(default_factory=lambda: _env_int("FREE_PAGES_DEFAULT", 5))

    # 字体与导出
    font_dir: Path = field(default_factory=lambda: Path(_env("FONT_DIR", str(BASE_DIR / "fonts"))))
    playwright_chromium_path: str = field(default_factory=lambda: _env("PLAYWRIGHT_CHROMIUM_PATH"))
    export_max_sync_pages: int = field(default_factory=lambda: _env_int("EXPORT_MAX_SYNC_PAGES", 10))

    # 管理员初始化
    admin_init_user: str = field(default_factory=lambda: _env("ADMIN_INIT_USER", "admin"))
    admin_init_pwd: str = field(default_factory=lambda: _env("ADMIN_INIT_PWD", ""))

    # 风控
    device_account_limit: int = field(default_factory=lambda: _env_int("DEVICE_ACCOUNT_LIMIT", 2))
    ip_account_limit: int = field(default_factory=lambda: _env_int("IP_ACCOUNT_LIMIT", 5))
    # 回环地址豁免：本机请求（127.0.0.1/::1）不可能来自真实用户，却会把 IP 配额占满，
    # 导致本机自测之后真人注册全部被判"超限、不发免费额度"。默认豁免，线上无副作用。
    anti_abuse_skip_loopback: bool = field(
        default_factory=lambda: _env("ANTI_ABUSE_SKIP_LOOPBACK", "1") == "1"
    )

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


settings = Settings()


def reload_settings() -> Settings:
    """测试用：重新读取环境变量。

    必须**原地**改写同一个对象：其他模块都是 `from .config import settings`，
    若这里 `settings = Settings()` 重新绑定，它们仍持有旧引用，换库/换 token 不会生效。
    """
    fresh = Settings()
    for f in fields(Settings):
        object.__setattr__(settings, f.name, getattr(fresh, f.name))
    return settings
