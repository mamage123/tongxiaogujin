"""预处理：PDF 按页渲染、图片探测、双页检测/拆分/裁边。

关键决定（与《技术选型》一致）：
**PDF 一律由本服务渲染成单页图片，逐页调用供应商。**
理由：① 规避星河「超过 10 页只处理前 10 页」的静默截断（V0 待验，但不能赌）；
     ② 选页、页级重试、流式推送都要求页级粒度；
     ③ 双页拆分与裁边只能在本地产出。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

try:
    import pymupdf as fitz  # PyMuPDF（新版命名，旧的 fitz 别名已废弃）
except ImportError:  # pragma: no cover
    try:
        import fitz  # 兼容旧版 PyMuPDF
    except ImportError:
        fitz = None

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".bmp"}
# PDF 渲染 DPI：200 足够 OCR，且能把 800 字/页控制在 ~1800px 长边（成本与精度平衡）
PDF_DPI = 200


class PreprocessError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass
class PageImage:
    page_no: int          # 原文件页号（1 起）
    path: str
    width: int
    height: int
    is_spread: bool = False   # 疑似双页合拍
    split: bool = False       # 实际已拆
    crop_box: tuple[int, int, int, int] | None = None


# ---------------------------------------------------------------- 文件探测
def detect_type(path: str) -> str:
    """按文件头判断真实类型，不信任扩展名（EX-02 伪装扩展名）。"""
    p = Path(path)
    head = p.read_bytes()[:16]
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image"
    if head.startswith(b"\xff\xd8\xff"):
        return "image"
    if head.startswith(b"BM"):
        return "image"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "image"
    raise PreprocessError("E_UNSUPPORTED", "文件已损坏或格式不支持")


def pdf_page_count(path: str) -> int:
    if fitz is None:
        raise PreprocessError("E_NO_PYMUPDF", "服务端未安装 PyMuPDF，无法处理 PDF")
    try:
        doc = fitz.open(path)
    except Exception as e:  # noqa: BLE001
        raise PreprocessError("E_CORRUPT", f"PDF 无法打开：{e}") from e
    try:
        if doc.is_encrypted and not doc.authenticate(""):
            raise PreprocessError("E_ENCRYPTED_PDF", "该 PDF 已加密，请解除密码后再上传")
        return doc.page_count
    finally:
        doc.close()


def image_size(path: str) -> tuple[int, int]:
    try:
        with Image.open(path) as im:
            return im.size
    except Exception as e:  # noqa: BLE001
        raise PreprocessError("E_CORRUPT", f"图片无法打开：{e}") from e


def probe(path: str) -> tuple[str, int, list[tuple[int, int]]]:
    """返回 (file_type, pages, 每页尺寸)。PDF 只读尺寸，不渲染。"""
    ftype = detect_type(path)
    if ftype == "image":
        w, h = image_size(path)
        return "image", 1, [(w, h)]
    if fitz is None:
        raise PreprocessError("E_NO_PYMUPDF", "服务端未安装 PyMuPDF")
    try:
        doc = fitz.open(path)
    except Exception as e:  # noqa: BLE001
        raise PreprocessError("E_CORRUPT", f"PDF 无法打开：{e}") from e
    try:
        if doc.is_encrypted and not doc.authenticate(""):
            raise PreprocessError("E_ENCRYPTED_PDF", "该 PDF 已加密，请解除密码后再上传")
        sizes = []
        for page in doc:
            r = page.rect
            scale = PDF_DPI / 72.0
            sizes.append((int(r.width * scale), int(r.height * scale)))
        return "pdf", doc.page_count, sizes
    finally:
        doc.close()


# ---------------------------------------------------------------- 渲染
def render_pdf(path: str, out_dir: Path, pages: list[int] | None = None, dpi: int = PDF_DPI) -> list[PageImage]:
    """把 PDF 指定页渲染成 PNG。pages 为 1 起的原页号，None = 全部。"""
    if fitz is None:
        raise PreprocessError("E_NO_PYMUPDF", "服务端未安装 PyMuPDF")
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        doc = fitz.open(path)
    except Exception as e:  # noqa: BLE001
        raise PreprocessError("E_CORRUPT", f"PDF 无法打开：{e}") from e

    result: list[PageImage] = []
    try:
        if doc.is_encrypted and not doc.authenticate(""):
            raise PreprocessError("E_ENCRYPTED_PDF", "该 PDF 已加密，请解除密码后再上传")
        idxs = [p - 1 for p in pages] if pages else list(range(doc.page_count))
        mat = fitz.Matrix(dpi / 72.0, dpi / 72.0)
        for i in idxs:
            if i < 0 or i >= doc.page_count:
                continue
            page = doc.load_page(i)
            pix = page.get_pixmap(matrix=mat, colorspace=fitz.csGRAY)
            out = out_dir / f"src_{i + 1:04d}.png"
            pix.save(str(out))
            result.append(PageImage(page_no=i + 1, path=str(out), width=pix.width, height=pix.height))
    finally:
        doc.close()
    return result


def load_image_page(path: str, page_no: int = 1) -> PageImage:
    w, h = image_size(path)
    return PageImage(page_no=page_no, path=path, width=w, height=h)


def parse_page_range(expr: str | None, total: int) -> list[int]:
    """'all' / '1-50' / '3' / '1-10,15' → 页号列表（1 起）。

    非法输入抛 PreprocessError（EX-03 由上层转文案）。
    """
    if expr is None or str(expr).strip().lower() in ("", "all"):
        return list(range(1, total + 1))
    expr = str(expr).strip().replace("，", ",")
    pages: list[int] = []
    for part in expr.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            if not (a.strip().isdigit() and b.strip().isdigit()):
                raise PreprocessError("E_BAD_RANGE", "页码格式不正确，示例：all 或 1-50")
            start, end = int(a), int(b)
            if start < 1 or end < start:
                raise PreprocessError("E_BAD_RANGE", "页码格式不正确，示例：all 或 1-50")
            pages.extend(range(start, min(end, total) + 1))
        else:
            if not part.isdigit():
                raise PreprocessError("E_BAD_RANGE", "页码格式不正确，示例：all 或 1-50")
            n = int(part)
            if n < 1 or n > total:
                raise PreprocessError("E_BAD_RANGE", f"页码超出范围（共 {total} 页）")
            pages.append(n)
    uniq = sorted(set(pages))
    if not uniq:
        raise PreprocessError("E_BAD_RANGE", "页码格式不正确，示例：all 或 1-50")
    return uniq


def compact_page_range(pages: list[int]) -> str:
    """把页号列表压成最省字的表达式：`1-113` / `1-5,8,10-20`。

    为什么要压：`tasks.page_range` 只是 TEXT 备注列（schema 注释写的是
    `'all' 或 '1-50'`），而建任务时曾把展开后的页号列表塞进去并 `[:200]` 截断。
    113 页展开成 `1,2,3,...,113` 要 380 字符，截断后 worker 只读到 70 个页号 ——
    **后半本书被静默丢掉，还按 70 页计费**（2026-10-28 test6.pdf 实测）。
    压缩后 200 字符足够表达几千页，且人眼可读。
    """
    if not pages:
        return "all"
    seq = sorted(set(int(p) for p in pages))
    out: list[str] = []
    start = prev = seq[0]
    for p in seq[1:]:
        if p == prev + 1:
            prev = p
            continue
        out.append(f"{start}-{prev}" if prev > start else str(start))
        start = prev = p
    out.append(f"{start}-{prev}" if prev > start else str(start))
    return ",".join(out)


# ---------------------------------------------------------------- 双页检测（M1b）
def _vertical_lines(gray: "np.ndarray") -> list[int]:
    """提取「长竖直线」的 x 坐标（版框边线、中缝线）。

    为什么不用「暗像素列占比」：竖排古籍**每一列文字**本身就是一条暗竖线，
    占比和版框线一样高（实测单页样张 12 条、占比 0.87），完全分不开。
    形态学开运算要求**连续**暗像素长度 ≥ h×min_frac，而竖排字的字间空隙
    会让单列文字断开（一个字约 40px < 阈值），只有真正贯通的版框线能留下。
    """
    h, w = gray.shape
    binary = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 25, 8
    )
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(15, int(h * 0.28))))
    vert = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel)
    prof = vert.sum(axis=0) / 255.0 / h
    hits = np.where(prof > 0.25)[0]
    lines: list[int] = []
    if len(hits) == 0:
        return lines
    start = prev = int(hits[0])
    for c in hits[1:]:
        c = int(c)
        if c - prev > 8:
            lines.append((start + prev) // 2)
            start = c
        prev = c
    lines.append((start + prev) // 2)
    return lines


def _col_dark_ratio(gray: "np.ndarray") -> "np.ndarray":
    """每列的暗像素占页高比例（0~1），逐列一个数。

    与 `_vertical_lines` 的差别：这里**不做形态学开运算**。要抓的是「整列全暗」
    的实心装订缝（纸弯曲处的阴影带），而开运算会把只有 2~3px 宽的阴影带削掉。
    """
    binary = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 25, 8
    )
    return (binary > 0).mean(axis=0)


def _frames(gray: "np.ndarray") -> list[tuple[int, int, int, int]]:
    """提取可能的版框矩形 (x, y, w, h)，按 x 排序。作为中缝双线法的兜底。"""
    h, w = gray.shape
    binary = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 25, 8
    )
    hk = cv2.getStructuringElement(cv2.MORPH_RECT, (max(15, w // 12), 1))
    vk = cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(15, h // 12)))
    lines = cv2.bitwise_or(
        cv2.morphologyEx(binary, cv2.MORPH_OPEN, hk),
        cv2.morphologyEx(binary, cv2.MORPH_OPEN, vk),
    )
    cnts, _ = cv2.findContours(lines, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    out = []
    for c in cnts:
        x, y, cw, ch = cv2.boundingRect(c)
        if cw < w * 0.30 or ch < h * 0.35:
            continue
        out.append((x, y, cw, ch))
    return sorted(out)


def detect_spread(img_path: str, path_out_dir: Path | None = None) -> tuple[bool, float, int | None]:
    """检测是否双页合拍。返回 (是否双页, 置信度, 中缝 x 坐标)。

    判据（三条，命中任一即判双页）：

    A. **中缝双线**——双页合拍时左右两页各自的版框边线紧挨着，在中间区域
       形成一对间距极小的竖线（实测样张 19~23px）。单页竖排图的相邻文字列
       间距是 60~90px，不会触发。

    B. **并排版框**——检测到两个宽度相近（各占 30%~62% 图宽）且紧邻的版框
       矩形。用于中缝被纸张弯曲糊掉、竖线断开的扫描件。

    C. **单条实心装订缝**——纸弯阴影把两页框线糊成**一条**贯穿整页的暗带时，
       判据 A 找不到「一对线」就漏判（2026-10-28 用 test8.png 实测漏判）。
       特征是：该列几乎全高皆暗（列暗占比 ≥0.5），而紧邻两侧 12~40px 是白纸边
       （页边距）。单页图中心的那条线（文字列 / 界行）两侧紧邻就是别的字，
       窗内暗占比 0.12 以上，挡得住（test2/test9 实测 0.118~0.274）。

    2026-10 之前用的是「中间区最暗列 vs 两侧平均亮度」的对比度判据，
    实测无效：单页竖排古籍的正文列本身就是最暗列，对比度 0.65，
    比真双页的 0.29~0.35 还高，方向完全反了。同时旧的
    `w < h * 1.1` 前置条件会把 1000×910（比例 1.10）的合拍图直接挡掉。
    """
    try:
        with Image.open(img_path) as im:
            gray_src = np.asarray(im.convert("L"))
    except Exception:  # noqa: BLE001 - 检测失败一律按单页走，不能挡住识别
        return False, 0.0, None

    src_h, src_w = gray_src.shape
    # 长边归一到 1000，下面的像素阈值才有可比性
    target = 1000
    if src_w > target:
        ratio = target / src_w
        gray = cv2.resize(gray_src, (target, max(1, int(src_h * ratio))), interpolation=cv2.INTER_AREA)
    else:
        gray = gray_src
        ratio = 1.0
    h, w = gray.shape

    # 双页合拍一定是横的或近方的；窄长图直接排除（单页竖排古籍 0.6~0.8）
    if w < h * 0.9:
        return False, 0.0, None

    lo, hi = int(w * 0.30), int(w * 0.70)
    lines = [x for x in _vertical_lines(gray) if lo <= x <= hi]

    # 判据 A：中缝双线。
    # 候选对里挑「最靠近画面中心」的那一对，而不是「间距最小」的：
    # 版框内边线与它旁边第一列文字也可能凑成一对（间距 25px 左右），
    # 但书的开合缝永远在正中，文字列不会。实测样张 0.486 / 0.507 / 0.492。
    center = w / 2
    best = None  # (中心偏离, gap, 中点)
    for a, b in zip(lines, lines[1:]):
        gap = b - a
        if gap > max(10, int(w * 0.030)):
            continue
        mid = (a + b) / 2
        cand = (abs(mid - center), gap, int(mid))
        if best is None or cand < best:
            best = cand
    if best is not None and best[0] <= w * 0.08:
        _off, gap, seam_small = best
        # 缝越窄越确定；23px/1000px 对应 conf≈0.85
        conf = max(0.55, 1.0 - (gap / max(1.0, w * 0.030)) * 0.45)
        return True, round(min(conf, 0.99), 3), int(seam_small / ratio)

    # 判据 C：单条实心装订缝。
    # 适用场景：书脊被压得死紧，两页框线连同纸弯阴影糊成一条 2~5px 的暗带。
    # 判据 A 要求「一对线」，这时只数得到一条，直接漏判（test8.png 就是这样）。
    # 关键鉴别点不是「中间有没有暗线」（单页的文字列、界行也暗），而是
    # **暗线两侧紧邻是不是白纸边**：装订缝两边必然是两页各自的页边距。
    col = _col_dark_ratio(gray)
    band_lo, band_hi = int(w * 0.32), int(w * 0.68)
    max_band = max(4, int(w * 0.030))
    best_c = None  # (中心偏离, 边长暗占比, 中点)
    x = band_lo
    while x < band_hi:
        if col[x] < 0.50:
            x += 1
            continue
        s = x
        while x < band_hi and col[x] >= 0.50:
            x += 1
        e = x - 1  # 暗带是 [s, e]
        if e - s + 1 > max_band:
            continue
        mid = (s + e) / 2.0
        if abs(mid - center) > w * 0.08:
            continue
        left = col[max(0, s - 40): max(1, s - 12)]
        right = col[min(w, e + 13): min(w, e + 41)]
        if len(left) < 8 or len(right) < 8:
            continue
        edge = max(float(left.mean()), float(right.mean()))
        # 0.10 是实测余量：真缝 0.044~0.064，单页最近的一次是 test2 的 0.118
        if edge > 0.10:
            continue
        cand = (abs(mid - center), edge, int(mid))
        if best_c is None or cand < best_c:
            best_c = cand
    if best_c is not None:
        _off, edge, seam_band = best_c
        conf = min(0.95, max(0.62, 1.0 - edge * 2))
        return True, round(conf, 3), int(seam_band / ratio)

    # 判据 B：并排版框
    frames = _frames(gray)
    for i, f1 in enumerate(frames):
        for f2 in frames[i + 1:]:
            x1, _, w1, _ = f1
            x2, _, w2, _ = f2
            if not (w * 0.30 <= w1 <= w * 0.62 and w * 0.30 <= w2 <= w * 0.62):
                continue
            gap = x2 - (x1 + w1)
            if -8 <= gap <= w * 0.10 and abs(w1 - w2) < w * 0.12:
                seam = x1 + w1 + max(0, gap) // 2
                return True, 0.62, int(seam / ratio)

    return False, 0.0, None


def split_spread(img_path: str, seam_x: int, out_dir: Path) -> tuple[str, str]:
    """按中缝拆成右半（先读）与左半。返回 (右图路径, 左图路径)。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    with Image.open(img_path) as im:
        w, h = im.size
        seam = max(1, min(w - 1, seam_x))
        right = im.crop((seam, 0, w, h))
        left = im.crop((0, 0, seam, h))
        rp = out_dir / "right.png"
        lp = out_dir / "left.png"
        right.save(rp)
        left.save(lp)
    return str(rp), str(lp)


def auto_crop(img_path: str, out_path: str | None = None) -> tuple[str, tuple[int, int, int, int]]:
    """裁掉四周黑边/装订阴影，**保留页眉页脚**。

    做法：找内容边界框（阈值化后非背景像素的外接矩形），再向外留 2% 余量，
    宁可少裁也不裁掉正文（EX-23）。
    """
    with Image.open(img_path) as im:
        gray = im.convert("L")
        w, h = gray.size
        hist = gray.histogram()
        total = w * h
        # 背景亮度取出现最多的灰度（古籍扫描通常接近 255 或偏黄）
        bg = max(range(256), key=lambda i: hist[i])
        thresh = bg - 40 if bg > 128 else bg + 40
        mask = gray.point(lambda v: 0 if abs(v - bg) < 25 else 255)
        bbox = mask.getbbox()
        if not bbox:
            box = (0, 0, w, h)
        else:
            pad_x, pad_y = int(w * 0.02), int(h * 0.02)
            box = (
                max(0, bbox[0] - pad_x),
                max(0, bbox[1] - pad_y),
                min(w, bbox[2] + pad_x),
                min(h, bbox[3] + pad_y),
            )
            # 裁得太狠（<60% 面积）说明可能误判，放弃裁切
            area_ratio = ((box[2] - box[0]) * (box[3] - box[1])) / total
            if area_ratio < 0.6:
                box = (0, 0, w, h)
        out = out_path or str(Path(img_path).with_name(Path(img_path).stem + "_crop.png"))
        im.crop(box).save(out)
    return out, box  # type: ignore[return-value]
