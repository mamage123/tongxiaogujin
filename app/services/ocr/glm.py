"""GLM-OCR 降级实现（备用通道）。

用途：星河 429/5xx 时兜底。返回 Markdown，**无坐标** → bbox=(0,0,0,0)，
框层会因坐标异常而静默隐藏（EX-27），这是已知取舍，不是缺陷。
"""
from __future__ import annotations

import base64
import json
import time
from pathlib import Path

import httpx
from PIL import Image

from ...config import settings
from .base import OcrBlock, OcrProviderError, OcrPageResult, OcrQuotaExhausted

PROMPT = (
    "请识别这张图片中的全部文字，按阅读顺序输出 Markdown。"
    "如果是竖排古籍，请从最右一列开始、逐列自上而下输出，多栏时整栏输出完再换下一栏。"
    "保留段落结构，不要添加任何解释、前言或结语。"
)


class GlmProvider:
    name = "glm"

    def __init__(self, api_key: str | None = None, model: str | None = None, endpoint: str | None = None):
        self.api_key = api_key if api_key is not None else settings.glm_api_key
        self.model = model or settings.glm_model
        self.endpoint = endpoint or settings.glm_endpoint

    def recognize(self, image_path: str, mode: str = "ancient") -> OcrPageResult:
        if not self.api_key:
            raise OcrProviderError("E_NO_KEY", "未配置 GLM_API_KEY")

        p = Path(image_path)
        with Image.open(p) as im:
            width, height = im.size

        b64 = base64.b64encode(p.read_bytes()).decode("ascii")
        mime = "image/png" if p.suffix.lower() == ".png" else "image/jpeg"

        started = time.time()
        try:
            with httpx.Client(timeout=settings.ocr_timeout_s) as client:
                resp = client.post(
                    self.endpoint,
                    headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                    json={
                        "model": self.model,
                        "messages": [
                            {
                                "role": "user",
                                "content": [
                                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                                    {"type": "text", "text": PROMPT},
                                ],
                            }
                        ],
                        "temperature": 0.01,
                    },
                )
        except httpx.TimeoutException as e:
            raise OcrProviderError("E_TIMEOUT", f"GLM 超时：{e}", retryable=True) from e
        except httpx.HTTPError as e:
            raise OcrProviderError("E_NETWORK", f"GLM 网络错误：{e}", retryable=True) from e

        latency_ms = int((time.time() - started) * 1000)

        if resp.status_code == 429:
            raise OcrQuotaExhausted("GLM 额度/频率受限（429）")
        if resp.status_code >= 500:
            raise OcrProviderError("E_5XX", f"GLM {resp.status_code}", retryable=True)
        if resp.status_code != 200:
            raise OcrProviderError("E_BAD_STATUS", f"GLM 返回 {resp.status_code}：{resp.text[:300]}")

        try:
            data = resp.json()
            text = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError) as e:
            raise OcrProviderError("E_BAD_JSON", f"GLM 返回结构异常：{e}", retryable=True) from e

        if isinstance(text, list):  # 少数兼容格式
            text = "".join(str(x.get("text", "")) for x in text if isinstance(x, dict))

        # 无坐标：整页一个块，bbox 全 0 → 上层判定坐标异常并隐藏框层
        blocks = [OcrBlock(block_id="p0", label="text", order=0, text=text, bbox=(0, 0, 0, 0))]

        return OcrPageResult(
            page_no=1,
            width=int(width),
            height=int(height),
            blocks=blocks,
            plain_text="",
            raw_text=text,
            provider=self.name,
            model=self.model,
            latency_ms=latency_ms,
        )
