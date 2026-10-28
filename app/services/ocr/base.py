"""统一 OCR 返回结构与供应商接口（开发规格 §5）。

铁律：业务层禁止出现 `if provider == "xinghe"`。要看供应商就读 `OcrPageResult.provider`。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass
class OcrBlock:
    block_id: str
    label: str                                  # text/title/table/formula/figure/header
    order: int                                  # 阅读顺序，从 0 起
    text: str
    bbox: tuple[int, int, int, int] = (0, 0, 0, 0)
    polygon: list[tuple[int, int]] | None = None
    group_id: str | None = None

    def to_row(self) -> dict:
        import json

        return {
            "block_id": self.block_id,
            "block_label": self.label or "text",
            "block_order": int(self.order),
            "text": self.text or "",
            "bbox_x1": int(self.bbox[0]),
            "bbox_y1": int(self.bbox[1]),
            "bbox_x2": int(self.bbox[2]),
            "bbox_y2": int(self.bbox[3]),
            "polygon_json": json.dumps(self.polygon, ensure_ascii=False) if self.polygon else None,
            "group_id": self.group_id,
        }


# 唯一一处 page_blocks 插入语句，绑定名与 OcrBlock.to_row() 的键一一对应。
# 放进 base 是为了防止 worker / retry 两处各写一份导致漂移（曾因 :bid 与 block_id 不匹配踩坑）。
INSERT_BLOCK_SQL = (
    "INSERT INTO page_blocks(task_id,page_no,block_id,block_label,block_order,text,"
    "bbox_x1,bbox_y1,bbox_x2,bbox_y2,polygon_json,group_id)"
    " VALUES(:t,:p,:block_id,:block_label,:block_order,:text,"
    ":bbox_x1,:bbox_y1,:bbox_x2,:bbox_y2,:polygon_json,:group_id)"
)


@dataclass
class OcrPageResult:
    page_no: int
    width: int                                  # 原图像素宽（坐标基准，必填）
    height: int
    blocks: list[OcrBlock] = field(default_factory=list)
    plain_text: str = ""                        # 归一化纯文本，长度即计费字数依据
    raw_text: str = ""                          # 供应商原始文本（保留换行）
    provider: str = ""                          # xinghe / glm / mock
    model: str = ""
    latency_ms: int = 0
    cost_mill: int = 0
    order_ok: bool | None = None
    # 供应商给出的 block_order 是否可直接采信（决定行序自检失败时要不要重排）。
    # 星河实测返回的 block_order 就是右起列序，重排反而会把它弄坏；
    # glm/mock 没有可信顺序，必须允许重排兜底。
    order_trusted: bool = False

    @property
    def chars(self) -> int:
        """计费字数 = 归一化纯文本去掉所有空白后的长度（唯一口径，FR-BIL-12）。"""
        return len("".join(self.plain_text.split()))

    def bbox_sane(self) -> bool:
        """坐标是否可用（FR-RES-11）：缺失、越界、面积占比 >90% / <0.1% 判为异常。"""
        if self.width <= 0 or self.height <= 0:
            return False
        page_area = self.width * self.height
        if not self.blocks:
            return False
        bad = 0
        for b in self.blocks:
            x1, y1, x2, y2 = b.bbox
            if x2 <= x1 or y2 <= y1:
                bad += 1
                continue
            if x1 < 0 or y1 < 0 or x2 > self.width or y2 > self.height:
                bad += 1
                continue
            area = (x2 - x1) * (y2 - y1)
            ratio = area / page_area
            if ratio > 0.9 or ratio < 0.001:
                bad += 1
        return bad == 0


@dataclass
class OcrCallStats:
    """单次供应商调用的用量（写入日志与成本看板）。"""

    provider: str = ""
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    pages: int = 0
    latency_ms: int = 0
    cost_mill: int = 0


@runtime_checkable
class OcrProvider(Protocol):
    name: str

    def recognize(self, image_path: str, mode: str) -> OcrPageResult:
        """识别单张图片（一页）。实现必须保证：width/height 非 0、blocks 有 order。"""
        ...


class OcrProviderError(Exception):
    """供应商错误。code 用于判断是否可降级/重试。"""

    def __init__(self, code: str, message: str, retryable: bool = False):
        self.code = code
        self.message = message
        self.retryable = retryable
        super().__init__(f"{code}: {message}")


class OcrQuotaExhausted(OcrProviderError):
    def __init__(self, message: str = "供应商额度已用尽"):
        super().__init__("E_QUOTA", message, retryable=False)
