"""离线 Mock 供应商。

用途：
1. `OCR_MOCK=1` 时无 token 也能跑通「上传 → 识别 → 结算 → 导出」全链路；
2. pytest 里避免真实调用（省钱、可重复）。

行为刻意贴近真实：会自动生成带坐标的块，并对 ancient 模式生成「右起多列」的块，
这样行序自检、框层渲染、竖排导出都能被测到。
"""
from __future__ import annotations

import random
import time

from PIL import Image

from .base import OcrBlock, OcrPageResult

# 一段繁体古籍样文（用于验证繁简转换与行序）
SAMPLE_TRAD = [
    "欽定四庫全書",
    "卷一 天文",
    "易曰觀乎天文以察時變",
    "日月星辰麗乎天者也",
    "聖人仰觀俯察而知其道",
    "故天垂象見吉凶",
    "此古之遺法也",
]


class MockProvider:
    name = "mock"

    def __init__(self, columns: int = 3, seed: int | None = None):
        self.columns = columns
        self.rng = random.Random(seed)

    def recognize(self, image_path: str, mode: str = "ancient") -> OcrPageResult:
        started = time.time()
        with Image.open(image_path) as im:
            width, height = im.size

        if mode == "ancient":
            blocks = self._vertical_blocks(width, height)
        else:
            blocks = self._horizontal_blocks(width, height)

        raw = "\n".join(b.text for b in sorted(blocks, key=lambda b: b.order))
        return OcrPageResult(
            page_no=1,
            width=width,
            height=height,
            blocks=blocks,
            plain_text="",
            raw_text=raw,
            provider=self.name,
            model="mock-v1",
            latency_ms=int((time.time() - started) * 1000) + 5,
        )

    # ------------------------------------------------------------ 造块
    def _vertical_blocks(self, width: int, height: int) -> list[OcrBlock]:
        """右起 N 列。order 0 = 最右列第一行。"""
        blocks: list[OcrBlock] = []
        margin = int(width * 0.08)
        col_w = max(40, (width - margin * 2) // self.columns)
        top = int(height * 0.08)
        order = 0
        lines = SAMPLE_TRAD
        # 每列行数尽量均分；用累计偏移切片，避免列长不均时串行重复
        base, extra = divmod(len(lines), self.columns)
        cursor = 0
        for c in range(self.columns):
            per = base + (1 if c < extra else 0)
            start = cursor
            cursor += per
            # 第 c 列：从右往左
            x2 = width - margin - c * col_w
            x1 = x2 - int(col_w * 0.7)
            for i in range(per):
                text = lines[(start + i) % len(lines)]
                y1 = top + i * int((height - top * 2) / per)
                y2 = y1 + int((height - top * 2) / per) - 8
                blocks.append(
                    OcrBlock(
                        block_id=f"c{c}l{i}",
                        label="text",
                        order=order,
                        text=text,
                        bbox=(x1, y1, x2, y2),
                        polygon=[(x1, y1), (x2, y1), (x2, y2), (x1, y2)],
                        group_id=f"col{c}",
                    )
                )
                order += 1
        return blocks

    def _horizontal_blocks(self, width: int, height: int) -> list[OcrBlock]:
        blocks: list[OcrBlock] = []
        margin = int(width * 0.1)
        top = int(height * 0.1)
        n = 5
        step = max(30, (height - top * 2) // n)
        for i in range(n):
            y1 = top + i * step
            blocks.append(
                OcrBlock(
                    block_id=f"l{i}",
                    label="title" if i == 0 else "text",
                    order=i,
                    text=f"第 {i + 1} 段：横排示例文本，用于验证段落合并与导出。",
                    bbox=(margin, y1, width - margin, y1 + step - 6),
                )
            )
        return blocks
