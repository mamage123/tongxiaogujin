"""星河社区 PaddleOCR-VL 实现（主通道）。

事实依据：2026-10-27 用真实令牌**实测**（不是照文档推测），验证脚本与结论见
`tools/probe_xinghe.py` 与 `.workbuddy/memory/` 当日日志。

## 接口形态：异步 Job，不是同步返回

    POST /api/v2/ocr/jobs        multipart: file + model + optionalPayload(JSON 字符串)
      -> {"code":0,"data":{"jobId":"108433281942192128"}}
    GET  /api/v2/ocr/jobs/{id}   轮询
      -> data.state: pending / running / done / failed
         running 时 data.extractProgress = {totalPages, extractedPages, startTime}
         done    时 data.resultUrl.jsonUrl = 结果 JSONL 的签名 URL（7 天有效）
    GET  {jsonUrl}               结果 JSONL

## 三个必须知道的坑（都实测踩过）

1. **结果 JSONL 是分片的，必须遍历全部行**。
   30 页 PDF 返回 **8 行**，每行 `layoutParsingResults` 4 页（最后一行 2 页），
   8×4 展平后正好 30 页。**只读第一行会得到 4 页**——这正是「静默截断」陷阱的真实形态，
   只不过截断发生在「你没遍历分片」，而不是供应商少给页。`_flatten_jsonl()` 是唯一入口。

2. **`dataInfo.numPages` 是「本分片的页数」，不是文件总页数**。
   校验总页数只能用提交后 `extractProgress.totalPages`，两者混用会误判。

3. **`useDocUnwarping` / `useDocOrientationClassify` 会改变坐标基准**。
   开启后供应商先矫正图像再检测，`prunedResult.width/height` 与 `block_bbox` 虽自洽，
   但与**本地原图**的像素比例不再一致，校对版框层会整体偏移。默认全关；
   要开就必须同时把「原图」换成返回的 `inputImage`，否则禁止开启。

## 返回的紧框坐标（这是校对版框层的数据源）

`prunedResult.parsing_res_list[]` 每项：

    {"block_label": "vertical_text", "block_content": "淳熙間，壽皇以…",
     "block_bbox": [876, 117, 921, 1056], "block_id": 3, "block_order": 4,
     "group_id": 3, "block_polygon_points": [[876.0,117.0], ...]}

实测竖排古籍页 `block_label` 为 `vertical_text`，**一块 = 一整列**（bbox 窄而高，
如 45×939），`block_order` 天然是从右往左的列序 → 正好喂给 ordercheck 的栏连续性信号。
现代横排页为 `text` / `paragraph_title` / `inline_formula`（公式给 LaTeX）。

## 质量实测（2026-10-27，PaddleOCR-VL-1.6）

- 3 页混排 PDF：3 页全回，页序正确
- 30 页 PDF：30 页全回，无截断
- 竖排繁体页：14 块（2 标题 + 12 列），繁简未转（交给 normalize）
- 含公式页：`$$Q=2\\pi\\lambda(T_1-T_2)/\\ln(r_2/r_1)$$` 识别正确
- 单页耗时 3~8 秒；30 页约 25 秒
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import httpx

from ...config import settings
from .base import OcrBlock, OcrProviderError, OcrPageResult, OcrQuotaExhausted

# 官方示例的默认值；token / endpoint 走配置，不硬编码
DEFAULT_ENDPOINT = "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs"
DEFAULT_MODEL = "PaddleOCR-VL-1.6"


def _resolve_token() -> str:
    """后台 settings 表优先，其次环境变量。

    后台改完 AISTUDIO_TOKEN 立即生效（每次识别都会新建 provider 实例、重新读表），
    无需重启服务；settings 表没配时回退到 .env 里的 AISTUDIO_TOKEN。
    """
    from ...db import get_setting

    v = get_setting("aistudio_token", "").strip()
    return v or settings.aistudio_token


def _resolve_endpoint() -> str:
    from ...db import get_setting

    v = get_setting("aistudio_endpoint", "").strip()
    return v or settings.aistudio_endpoint


class XingheProvider:
    name = "xinghe"

    def __init__(
        self,
        token: str | None = None,
        endpoint: str | None = None,
        model: str | None = None,
        timeout_s: int | None = None,
        poll_interval_s: float | None = None,
        job_timeout_s: int | None = None,
    ):
        self.token = token if token is not None else _resolve_token()
        self.endpoint = (endpoint or _resolve_endpoint() or DEFAULT_ENDPOINT).rstrip("/")
        self.model = model or settings.aistudio_model or DEFAULT_MODEL
        self.timeout_s = timeout_s or settings.ocr_timeout_s
        self.poll_interval_s = poll_interval_s or settings.aistudio_poll_interval_s
        self.job_timeout_s = job_timeout_s or settings.aistudio_job_timeout_s

    # ------------------------------------------------------------ 对外
    def recognize(self, image_path: str, mode: str = "ancient") -> OcrPageResult:
        """识别一页图片。多页文件（PDF）请用 recognize_file。"""
        pages = self.recognize_file(image_path, mode=mode)
        if not pages:
            raise OcrProviderError("E_EMPTY_RESPONSE", "星河返回 0 页", retryable=True)
        return pages[0]

    def recognize_file(self, path: str, mode: str = "ancient") -> list[OcrPageResult]:
        """提交整个文件（图片或 PDF），返回全部页，顺序即文件页序。"""
        if not self.token:
            raise OcrProviderError(
                "E_NO_TOKEN",
                "未配置 AISTUDIO_TOKEN，无法调用星河 API（本地联调可设 OCR_MOCK=1）",
            )

        p = Path(path)
        if not p.exists():
            raise OcrProviderError("E_NO_FILE", f"文件不存在：{path}")

        started = time.time()
        job_id = self._submit(p)
        json_url = self._wait(job_id)
        pages_raw = self._flatten_jsonl(self._download(json_url))
        if not pages_raw:
            raise OcrProviderError("E_EMPTY_RESPONSE", "星河结果 JSONL 中没有页", retryable=True)

        latency_ms = int((time.time() - started) * 1000)
        return [self._parse_page(item, i + 1, latency_ms) for i, item in enumerate(pages_raw)]

    # ------------------------------------------------------------ 提交 / 轮询 / 下载
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"bearer {self.token}"}

    def _optional_payload(self) -> dict[str, bool]:
        # 见模块头「坑 3」：这两个开关默认必须关，否则框层相对本地原图会偏移
        return {
            "useDocOrientationClassify": settings.aistudio_use_orientation,
            "useDocUnwarping": settings.aistudio_use_unwarping,
            "useChartRecognition": settings.aistudio_use_chart,
        }

    def _submit(self, p: Path) -> str:
        data = {"model": self.model, "optionalPayload": json.dumps(self._optional_payload())}
        mime = _mime_of(p)
        try:
            with httpx.Client(timeout=self.timeout_s) as client, p.open("rb") as f:
                resp = client.post(
                    self.endpoint,
                    headers=self._headers(),
                    data=data,
                    files={"file": (p.name, f, mime)},
                )
        except httpx.TimeoutException as e:
            raise OcrProviderError("E_TIMEOUT", f"星河提交超时：{e}", retryable=True) from e
        except httpx.HTTPError as e:
            raise OcrProviderError("E_NETWORK", f"星河提交网络错误：{e}", retryable=True) from e

        if resp.status_code in (401, 403):
            raise OcrProviderError(
                "E_AUTH", f"星河鉴权失败（{resp.status_code}），检查 AISTUDIO_TOKEN 是否过期"
            )
        if resp.status_code == 429:
            raise OcrQuotaExhausted("星河额度已用尽（429）")
        if resp.status_code >= 500:
            raise OcrProviderError("E_5XX", f"星河提交 {resp.status_code}", retryable=True)
        if resp.status_code != 200:
            raise OcrProviderError(
                "E_BAD_STATUS", f"星河提交返回 {resp.status_code}：{resp.text[:300]}"
            )

        try:
            body = resp.json()
        except ValueError as e:
            raise OcrProviderError("E_BAD_JSON", "星河提交返回非 JSON", retryable=True) from e

        if body.get("code") not in (0, None):
            raise OcrProviderError(
                "E_SUBMIT_REJECTED",
                f"星河拒绝任务 code={body.get('code')} msg={body.get('msg')}",
            )
        job_id = ((body.get("data") or {}).get("jobId") or "").strip()
        if not job_id:
            raise OcrProviderError("E_NO_JOB_ID", f"星河未返回 jobId：{str(body)[:300]}")
        return job_id

    def _wait(self, job_id: str) -> str:
        """轮询到 done，返回结果 jsonUrl。总时长受 AISTUDIO_JOB_TIMEOUT_S 约束。"""
        deadline = time.time() + self.job_timeout_s
        url = f"{self.endpoint}/{job_id}"
        last_state = ""

        while True:
            if time.time() > deadline:
                raise OcrProviderError(
                    "E_JOB_TIMEOUT",
                    f"星河任务 {job_id} 超过 {self.job_timeout_s}s 未完成（最后状态 {last_state}）",
                    retryable=True,
                )
            try:
                with httpx.Client(timeout=self.timeout_s) as client:
                    resp = client.get(url, headers=self._headers())
            except httpx.HTTPError as e:
                # 轮询期间偶发网络抖动不该让整页失败，继续等
                last_state = f"poll_error:{type(e).__name__}"
                time.sleep(self.poll_interval_s)
                continue

            if resp.status_code in (401, 403):
                raise OcrProviderError("E_AUTH", f"星河鉴权失败（{resp.status_code}）")
            if resp.status_code == 429:
                raise OcrQuotaExhausted("星河轮询命中限流（429）")
            if resp.status_code >= 500:
                last_state = f"http_{resp.status_code}"
                time.sleep(self.poll_interval_s)
                continue

            try:
                data = (resp.json() or {}).get("data") or {}
            except ValueError:
                last_state = "bad_json"
                time.sleep(self.poll_interval_s)
                continue

            state = (data.get("state") or "").lower()
            last_state = state

            if state == "done":
                json_url = ((data.get("resultUrl") or {}).get("jsonUrl") or "").strip()
                if not json_url:
                    raise OcrProviderError("E_NO_RESULT_URL", "星河任务完成但未给 jsonUrl")
                return json_url

            if state == "failed":
                msg = data.get("errorMsg") or "未知原因"
                low = str(msg).lower()
                # 文件本身的问题重试无用；服务侧问题才值得重试
                fatal = any(k in low for k in ("too large", "format", "unsupported", "corrupt", "invalid file"))
                raise OcrProviderError(
                    "E_JOB_FAILED", f"星河任务失败：{msg}", retryable=not fatal
                )

            time.sleep(self.poll_interval_s)

    def _download(self, json_url: str) -> str:
        try:
            with httpx.Client(timeout=max(self.timeout_s, 60), follow_redirects=True) as client:
                resp = client.get(json_url)
        except httpx.HTTPError as e:
            raise OcrProviderError("E_RESULT_FETCH", f"下载星河结果失败：{e}", retryable=True) from e
        if resp.status_code != 200:
            raise OcrProviderError(
                "E_RESULT_FETCH", f"下载星河结果返回 {resp.status_code}", retryable=True
            )
        return resp.text

    # ------------------------------------------------------------ 解析
    @staticmethod
    def _flatten_jsonl(text: str) -> list[dict]:
        """把分片 JSONL 展平成「一项一页」。

        **这是防页丢失的唯一入口**：只读第一行会丢页（30 页返回 8 行 × 4 页）。
        坏行跳过不抛错——一行损坏不该让整本失败，但全空会让上层重试。
        """
        pages: list[dict] = []
        for line in (text or "").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            result = row.get("result") if isinstance(row, dict) else None
            if not isinstance(result, dict):
                continue
            lpr = result.get("layoutParsingResults")
            if isinstance(lpr, list):
                pages.extend(x for x in lpr if isinstance(x, dict))
        return pages

    def _parse_page(self, item: dict, page_no: int, latency_ms: int) -> OcrPageResult:
        pruned = item.get("prunedResult")
        if not isinstance(pruned, dict):
            pruned = {}

        width, height = self._page_size(pruned)
        blocks = self._extract_blocks(pruned)
        raw_text = self._extract_raw_text(item, pruned, blocks)

        return OcrPageResult(
            page_no=page_no,
            width=width,
            height=height,
            blocks=blocks,
            plain_text="",  # 由 normalize 填
            raw_text=raw_text,
            provider=self.name,
            model=self.model,
            latency_ms=latency_ms,
            # 实测：block_order 就是右起列序（第 1 块在最右列），可直接采信。
            # 自检不通过时只标记存疑，不用坐标猜测去推翻它。
            order_trusted=True,
        )

    @staticmethod
    def _page_size(pruned: dict) -> tuple[int, int]:
        """坐标系基准尺寸。必须用供应商给的值——bbox 是相对它量出来的。"""
        w = pruned.get("width")
        h = pruned.get("height")
        if isinstance(w, (int, float)) and isinstance(h, (int, float)) and w > 0 and h > 0:
            return int(w), int(h)
        # 极老版本可能塞在 dataInfo.pages[0]
        pages = (pruned.get("dataInfo") or {}).get("pages") if isinstance(pruned.get("dataInfo"), dict) else None
        if isinstance(pages, list) and pages and isinstance(pages[0], dict):
            w = pages[0].get("width")
            h = pages[0].get("height")
            if w and h:
                return int(w), int(h)
        return 0, 0

    @staticmethod
    def _extract_blocks(pruned: dict) -> list[OcrBlock]:
        raw = pruned.get("parsing_res_list") or pruned.get("parsingResList") or []
        if not isinstance(raw, list):
            return []

        items: list[tuple[int, int, OcrBlock]] = []
        for i, b in enumerate(raw):
            if not isinstance(b, dict):
                continue
            text = b.get("block_content")
            if text is None:
                text = b.get("blockContent") or b.get("text") or ""

            bbox = b.get("block_bbox") or b.get("blockBbox") or b.get("bbox")
            if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
                x1, y1, x2, y2 = (int(float(v)) for v in bbox)
                if x2 < x1:  # 少数返回 [x,y,w,h]
                    x2 = x1 + abs(x2)
                if y2 < y1:
                    y2 = y1 + abs(y2)
            else:
                x1 = y1 = x2 = y2 = 0

            polygon = None
            poly = b.get("block_polygon_points") or b.get("blockPolygonPoints")
            if isinstance(poly, list) and poly and isinstance(poly[0], (list, tuple)):
                polygon = [(int(float(pt[0])), int(float(pt[1]))) for pt in poly if len(pt) >= 2]

            raw_order = b.get("block_order")
            if raw_order is None:
                raw_order = b.get("blockOrder")
            seq = int(raw_order) if isinstance(raw_order, (int, float)) else i

            blk = OcrBlock(
                block_id=str(b.get("block_id") if b.get("block_id") is not None else f"b{i}"),
                label=str(b.get("block_label") or b.get("blockLabel") or "text"),
                order=0,  # 下面按 seq 重排后统一编号
                text=str(text),
                bbox=(x1, y1, x2, y2),
                polygon=polygon,
                group_id=(
                    str(b["group_id"]) if b.get("group_id") is not None
                    else (str(b["groupId"]) if b.get("groupId") is not None else None)
                ),
            )
            items.append((seq, i, blk))

        # 供应商的 block_order 就是阅读顺序（竖排页实测为右起列序）；缺失时按原序
        items.sort(key=lambda t: (t[0], t[1]))
        out: list[OcrBlock] = []
        for new_order, (_, _, blk) in enumerate(items):
            blk.order = new_order
            out.append(blk)
        return out

    @staticmethod
    def _extract_raw_text(item: dict, pruned: dict, blocks: list[OcrBlock]) -> str:
        md = item.get("markdown")
        if isinstance(md, dict) and isinstance(md.get("text"), str) and md["text"].strip():
            return md["text"]
        if isinstance(md, str) and md.strip():
            return md
        if blocks:
            return "\n".join(b.text for b in blocks if b.text)
        return ""


def _mime_of(p: Path) -> str:
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
        ".tif": "image/tiff",
        ".tiff": "image/tiff",
        ".pdf": "application/pdf",
    }.get(p.suffix.lower(), "application/octet-stream")


__all__ = ["XingheProvider", "DEFAULT_ENDPOINT", "DEFAULT_MODEL"]
