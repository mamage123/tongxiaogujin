"""健康检查与元信息（T01 验收项）。"""
from __future__ import annotations

from fastapi import APIRouter

from ..config import settings
from ..db import q1, setting_int
from ..deps import ok
from ..enums import RECHARGE_PRESETS_MILL
from ..services.export import fonts as fonts_mod
from ..services.export.pdf import engine_status, which_pyftsubset

router = APIRouter(prefix="/api/meta", tags=["meta"])


@router.get("/health")
def health():
    db_ok, db_err = True, None
    try:
        q1("SELECT 1 AS x")
    except Exception as e:  # noqa: BLE001
        db_ok, db_err = False, str(e)

    assets = fonts_mod.load_font_assets()
    fonts = [
        {
            "name": a.name,
            "exists": a.exists,
            "role": a.role,
            "license_grade": a.license_grade,
            "sha256_set": bool(a.sha256),
        }
        for a in assets
    ]
    font_ok = any(a["exists"] for a in fonts)

    pdf = engine_status()

    return ok(
        {
            "app_env": settings.app_env,
            "db": {"ok": db_ok, "error": db_err, "url_scheme": settings.database_url.split(":")[0]},
            "fonts": {
                "ok": font_ok,
                "assets": fonts,
                "policy": "仅允许白名单 3 文件；禁用系统宋体/黑体/楷体",
            },
            "pdf_engine": pdf,
            "pyftsubset": which_pyftsubset() or "未安装（pip install fonttools）",
            "ocr": {
                "primary": settings.ocr_primary,
                "fallback": settings.ocr_fallback,
                "mock": settings.ocr_mock,
                "token_configured": bool(settings.aistudio_token),
                "endpoint_configured": bool(settings.aistudio_endpoint),
            },
            "price": {
                "per_10k_mill": setting_int("price_per_10k_mill", settings.price_per_10k_mill),
                "min_bill_chars": setting_int("min_bill_chars", settings.min_bill_chars),
                "text": "¥1 / 万字，最低按 1000 字计费",
            },
        }
    )


@router.get("/config")
def public_config():
    """前端需要的公开配置（不含任何密钥）。"""
    return ok(
        {
            "price_text": "¥1 / 万字",
            "min_bill_text": "最低按 1000 字计费",
            "free_pages": setting_int("free_pages_default", settings.free_pages_default),
            "max_upload_mb": settings.max_upload_mb,
            "max_pages": setting_int("max_pages_normal", settings.max_pages),
            "max_pages_low_balance": setting_int("max_pages_low_balance", settings.max_pages_low_balance),
            # 档位以「厘」为准（全站金额口径），元值只是给前端展示用。
            # 下单时传 amount_mill，别把元当厘传，否则 ¥10 会变成 ¥0.01。
            "recharge_presets_mill": list(RECHARGE_PRESETS_MILL),
            "recharge_presets_yuan": [round(m / 1000, 2) for m in RECHARGE_PRESETS_MILL],
            "ttl_hours": settings.file_ttl_hours,
            "modes": [
                {"value": "ancient", "label": "繁体竖排古籍", "desc": "古籍扫描件，自动繁→简"},
                {"value": "formula", "label": "现代公式 / 简体", "desc": "含数学公式、表格的教材论文"},
                {"value": "modern", "label": "现代横排", "desc": "普通现代中文书籍文档"},
            ],
        }
    )
