"""OCR 门面：取供应商、重试、降级（开发规格 §5.5）。

三条硬规则：
1. 重试只在这三种情况：空输出 / 乱码率 >30% / 5xx。每次重试都是双倍成本。
2. 429 → 立即降级，不重试（额度没了，重试只会更糟）。
3. 业务层通过本模块拿结果，**不许自己判断供应商**。
"""
from __future__ import annotations

import re
import time

from ...config import settings
from .base import OcrPageResult, OcrProvider, OcrProviderError, OcrQuotaExhausted
from .glm import GlmProvider
from .mock import MockProvider
from .normalize import normalize
from .ordercheck import apply_order_check
from .xinghe import XingheProvider

RETRY_DELAYS = [3, 8]          # FR-OCR-05：单页重试 2 次，间隔 3s/8s
MAX_RETRY = 2
GARBLE_THRESHOLD = 0.30        # 乱码率阈值

# 乱码/控制字符：不属于正常中文文档的字符
_GARBLE_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufffd\ue000-\uf8ff]")


def build_provider(name: str) -> OcrProvider:
    """按名字造实例。名字来自配置（OCR_PRIMARY / OCR_FALLBACK），不硬编码分支在业务层。"""
    key = (name or "").strip().lower()
    if settings.ocr_mock or key in ("mock", ""):
        return MockProvider()
    if key == "xinghe":
        return XingheProvider()
    if key == "glm":
        return GlmProvider()
    raise OcrProviderError("E_UNKNOWN_PROVIDER", f"未知供应商：{name}")


def garble_rate(text: str) -> float:
    if not text:
        return 1.0
    bad = len(_GARBLE_RE.findall(text))
    return bad / max(1, len(text))


def _should_retry(result: OcrPageResult | None, err: Exception | None) -> bool:
    if err is not None:
        return isinstance(err, OcrProviderError) and err.retryable
    if result is None:
        return True
    if not (result.plain_text or "").strip() and not (result.raw_text or "").strip():
        return True  # 空输出
    if garble_rate(result.plain_text or result.raw_text) > GARBLE_THRESHOLD:
        return True  # 乱码率过高
    return False


def recognize_page(
    image_path: str,
    mode: str = "ancient",
    optimize: bool = True,
    keep_traditional: bool = True,
    provider_name: str | None = None,
) -> tuple[OcrPageResult, str, list[str]]:
    """识别单页，含重试与降级。

    返回 (归一化结果, 实际使用的 provider, 尝试记录)。
    异常：所有通道都失败时抛最后一个 OcrProviderError。
    """
    attempts: list[str] = []
    primary = provider_name or settings.ocr_primary
    fallback = settings.ocr_fallback
    order = [primary] + ([fallback] if fallback and fallback != primary else [])

    last_err: Exception | None = None
    for name in order:
        provider = build_provider(name)
        for attempt in range(MAX_RETRY + 1):
            try:
                res = provider.recognize(image_path, mode)
                res = normalize(
                    res, mode=mode, optimize=optimize, keep_traditional=keep_traditional
                )
                # 行序自检（仅 ancient）。trusted 由供应商声明：有可信阅读顺序时不重排
                blocks, passed, score = apply_order_check(
                    res.blocks, mode, trusted=res.order_trusted
                )
                res.blocks = blocks
                res.order_ok = passed if mode == "ancient" else None
                res.provider = provider.name

                if _should_retry(res, None) and attempt < MAX_RETRY:
                    attempts.append(f"{provider.name}#{attempt + 1}:empty_or_garble")
                    time.sleep(RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)])
                    continue
                if mode == "ancient":
                    res._order_score = score  # type: ignore[attr-defined]
                attempts.append(f"{provider.name}#{attempt + 1}:ok")
                return res, provider.name, attempts
            except OcrQuotaExhausted as e:
                last_err = e
                attempts.append(f"{provider.name}:quota")
                break  # 不重试，直接降级
            except OcrProviderError as e:
                last_err = e
                attempts.append(f"{provider.name}#{attempt + 1}:{e.code}")
                if e.retryable and attempt < MAX_RETRY:
                    time.sleep(RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)])
                    continue
                break  # 不可重试或已用完重试 → 换下一家
            except Exception as e:  # noqa: BLE001 - 供应商适配层必须兜住一切
                last_err = OcrProviderError("E_UNEXPECTED", str(e), retryable=False)
                attempts.append(f"{provider.name}:unexpected")
                break

    if isinstance(last_err, OcrProviderError):
        raise last_err
    raise OcrProviderError("E_ALL_PROVIDERS_FAILED", f"所有通道均失败：{last_err}")
