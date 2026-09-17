from __future__ import annotations

import math
from pathlib import Path
from typing import Callable, Iterator

import numpy as np
import png
from PIL import Image, ImageOps

from .cancellation import CancellationToken

try:
    import rawpy
except ImportError:  # pragma: no cover - reported clearly at runtime
    rawpy = None

LogFn = Callable[[str], None]
ProgressFn = Callable[[int, str], None]

_SRGB_TO_2020 = np.array(
    [
        [0.6274040, 0.3292820, 0.0433136],
        [0.0690970, 0.9195400, 0.0113612],
        [0.0163916, 0.0880132, 0.8955950],
    ],
    dtype=np.float32,
)
_LUMA = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)


def srgb_to_linear(x: np.ndarray) -> np.ndarray:
    x = x.astype(np.float32, copy=False)
    return np.where(x <= 0.04045, x / 12.92, np.power((x + 0.055) / 1.055, 2.4))


def linear_srgb_to_pq2020(x: np.ndarray, sdr_white_nits: float = 203.0) -> np.ndarray:
    """Convert linear sRGB where 1.0 is SDR white to Rec.2020 PQ."""
    rgb2020 = np.maximum(x @ _SRGB_TO_2020.T, 0.0)
    luminance = np.clip(rgb2020 * (sdr_white_nits / 10000.0), 0.0, 1.0)
    m1 = 2610.0 / 16384.0
    m2 = 2523.0 / 32.0
    c1 = 3424.0 / 4096.0
    c2 = 2413.0 / 128.0
    c3 = 2392.0 / 128.0
    p = np.power(luminance, m1)
    return np.power((c1 + c2 * p) / (1.0 + c3 * p), m2)


def _load_rgb8(path: Path) -> np.ndarray:
    with Image.open(path) as image:
        image = ImageOps.exif_transpose(image).convert("RGB")
        return np.asarray(image, dtype=np.uint8).copy()


def _fit_center(array: np.ndarray, width: int, height: int) -> np.ndarray:
    source_h, source_w = array.shape[:2]
    if source_w == width and source_h == height:
        return array
    if source_w >= width and source_h >= height:
        left = (source_w - width) // 2
        top = (source_h - height) // 2
        return array[top : top + height, left : left + width]
    channels = []
    for channel in range(array.shape[2]):
        plane = Image.fromarray(array[:, :, channel], mode="I;16")
        plane = plane.resize((width, height), Image.Resampling.LANCZOS)
        channels.append(np.asarray(plane, dtype=np.uint16))
    return np.stack(channels, axis=-1)


def _write_rgb16_png(path: Path, width: int, height: int, rows: Iterator[np.ndarray]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = png.Writer(width=width, height=height, greyscale=False, alpha=False, bitdepth=16)

    def flattened() -> Iterator[list[int]]:
        for row in rows:
            yield row.reshape(-1).tolist()

    with path.open("wb") as handle:
        writer.write(handle, flattened())


def _write_gray16_png(path: Path, width: int, height: int, rows: Iterator[np.ndarray]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = png.Writer(width=width, height=height, greyscale=True, alpha=False, bitdepth=16)
    with path.open("wb") as handle:
        writer.write(handle, (row.reshape(-1).tolist() for row in rows))


def _estimate_raw_scale(raw_linear: np.ndarray, base_rgb8: np.ndarray) -> float:
    step = max(8, min(raw_linear.shape[0], raw_linear.shape[1]) // 300)
    raw_sample = raw_linear[::step, ::step].astype(np.float32) / 65535.0
    base_sample = base_rgb8[::step, ::step].astype(np.float32) / 255.0
    base_linear = srgb_to_linear(base_sample)
    raw_y = raw_sample @ _LUMA
    base_y = base_linear @ _LUMA
    mask = (base_y > 0.03) & (base_y < 0.35) & (raw_y > 1e-5) & (raw_y < 0.8)
    if np.count_nonzero(mask) < 256:
        mask = (base_y > 0.01) & (base_y < 0.65) & (raw_y > 1e-5)
    ratios = base_y[mask] / raw_y[mask]
    if ratios.size == 0:
        return 8.0
    return float(np.clip(np.median(ratios), 0.25, 64.0))


def _decode_raw_context(
    raw_path: Path,
    base_path: Path,
    exposure_comp: float,
    log: LogFn | None,
    progress: ProgressFn | None,
    cancel_token: CancellationToken | None = None,
) -> tuple[np.ndarray, np.ndarray, float, float]:
    if cancel_token:
        cancel_token.check()
    if rawpy is None:
        raise RuntimeError("缺少 rawpy，无法解码 RAW。请重新安装完整程序。")
    base = _load_rgb8(base_path)
    height, width = base.shape[:2]
    if progress:
        progress(20, "解码 RAW 线性数据…")
    if log:
        log(f"SDR 底图：{base_path}，尺寸 {width}×{height}")
        log("正在解码 RAW；高像素相机会短时占用约 300–500 MB 内存。")
    with rawpy.imread(str(raw_path)) as raw:
        decoded = raw.postprocess(
            use_camera_wb=True,
            no_auto_bright=True,
            output_bps=16,
            gamma=(1.0, 1.0),
            output_color=rawpy.ColorSpace.sRGB,
            highlight_mode=rawpy.HighlightMode.Blend,
            demosaic_algorithm=rawpy.DemosaicAlgorithm.AHD,
        )
    original_h, original_w = decoded.shape[:2]
    if cancel_token:
        cancel_token.check()
    decoded = _fit_center(decoded, width, height)
    scale = _estimate_raw_scale(decoded, base) * math.pow(2.0, exposure_comp)
    sample_y = (decoded[::32, ::32].astype(np.float32) / 65535.0 * scale) @ _LUMA
    detected_stops = math.log2(max(1.0, float(np.percentile(sample_y, 99.99))))
    if log:
        log(f"RAW 解码尺寸：{original_w}×{original_h}；对齐后：{width}×{height}")
        log(f"RAW/SDR 中间调匹配系数：{scale:.4f}（曝光微调 {exposure_comp:+.2f} EV）")
        log(f"估计 RAW 有效高光余量：{detected_stops:.3f} stops，约 {2.0 ** detected_stops:.2f}× SDR 白")
    return base, decoded, scale, detected_stops


def _raw_chunk(
    base: np.ndarray,
    decoded: np.ndarray,
    scale: float,
    y0: int,
    y1: int,
) -> tuple[np.ndarray, np.ndarray]:
    base_linear = srgb_to_linear(base[y0:y1].astype(np.float32) / 255.0)
    raw_linear = decoded[y0:y1].astype(np.float32) * (scale / 65535.0)
    base_y = base_linear @ _LUMA
    raw_y = raw_linear @ _LUMA
    signal = np.maximum(base_y, raw_y)
    weight = np.clip((signal - 0.45) / 0.65, 0.0, 1.0)
    weight = weight * weight * (3.0 - 2.0 * weight)
    target_y = base_y + weight * np.maximum(raw_y - base_y, 0.0)
    ratio = target_y / np.maximum(base_y, 1e-4)
    stops = np.maximum(np.log2(np.maximum(ratio, 1.0)), 0.0)
    return base_linear, stops


def make_pq_alternate_from_raw(
    raw_path: Path,
    base_path: Path,
    output_png: Path,
    *,
    max_headroom_stops: float,
    exposure_comp: float = 0.0,
    log: LogFn | None = None,
    progress: ProgressFn | None = None,
    cancel_token: CancellationToken | None = None,
) -> dict[str, float | int]:
    base, decoded, scale, detected_stops = _decode_raw_context(
        raw_path, base_path, exposure_comp, log, progress, cancel_token
    )
    height, width = base.shape[:2]
    max_relative = math.pow(2.0, max_headroom_stops)
    if log:
        log(f"HDR 输出上限：{max_headroom_stops:.2f} stops（{max_relative:.2f}× SDR）")
    if progress:
        progress(45, "生成 Rec.2020 PQ HDR 参考图…")

    def rows() -> Iterator[np.ndarray]:
        chunk = 32
        last_progress = -1
        for y0 in range(0, height, chunk):
            if cancel_token:
                cancel_token.check()
            y1 = min(height, y0 + chunk)
            base_linear, stops = _raw_chunk(base, decoded, scale, y0, y1)
            stops = np.minimum(stops, max_headroom_stops)
            linear = base_linear * np.exp2(stops)[..., None]
            np.clip(linear, 0.0, max_relative, out=linear)
            pq = linear_srgb_to_pq2020(linear)
            encoded = np.clip(np.rint(pq * 65535.0), 0, 65535).astype(np.uint16)
            yield from encoded
            current = 45 + int(20 * y1 / height)
            if progress and current != last_progress:
                last_progress = current
                progress(current, "写入 16-bit HDR 临时图…")

    _write_rgb16_png(output_png, width, height, rows())
    return {"width": width, "height": height, "scale": scale, "detected_stops": detected_stops}


def export_gain_map_from_raw(
    raw_path: Path,
    base_path: Path,
    output_png: Path,
    *,
    max_headroom_stops: float,
    exposure_comp: float = 0.0,
    log: LogFn | None = None,
    progress: ProgressFn | None = None,
    cancel_token: CancellationToken | None = None,
) -> dict[str, float | int]:
    """Export a 16-bit grayscale map where 0..65535 maps to 0..max stops."""
    base, decoded, scale, detected_stops = _decode_raw_context(
        raw_path, base_path, exposure_comp, log, progress, cancel_token
    )
    height, width = base.shape[:2]
    histogram = np.zeros(4096, dtype=np.int64)
    total = 0
    if log:
        log(
            f"导出规则：黑色=0 stops（1.00×），白色={max_headroom_stops:.2f} stops"
            f"（{2.0 ** max_headroom_stops:.2f}×）"
        )
    if progress:
        progress(45, "计算并写入 16-bit RAW 增益图…")

    def rows() -> Iterator[np.ndarray]:
        nonlocal total
        chunk = 32
        for y0 in range(0, height, chunk):
            if cancel_token:
                cancel_token.check()
            y1 = min(height, y0 + chunk)
            _base_linear, stops = _raw_chunk(base, decoded, scale, y0, y1)
            stops = np.clip(stops, 0.0, max_headroom_stops)
            normalized = stops / max(max_headroom_stops, 1e-6)
            bins = np.minimum((normalized * (len(histogram) - 1)).astype(np.int32), len(histogram) - 1)
            histogram[:] += np.bincount(bins.ravel(), minlength=len(histogram))
            total += normalized.size
            encoded = np.clip(np.rint(normalized * 65535.0), 0, 65535).astype(np.uint16)
            yield from encoded
            if progress:
                progress(45 + int(45 * y1 / height), "写入 16-bit 灰度增益图…")

    _write_gray16_png(output_png, width, height, rows())

    cumulative = np.cumsum(histogram)
    def percentile(p: float) -> float:
        if total <= 0:
            return 0.0
        index = int(np.searchsorted(cumulative, total * p / 100.0, side="left"))
        return index / (len(histogram) - 1) * max_headroom_stops

    stats: dict[str, float | int] = {
        "width": width,
        "height": height,
        "scale": scale,
        "detected_stops": detected_stops,
        "p50_stops": percentile(50),
        "p95_stops": percentile(95),
        "p99_stops": percentile(99),
        "max_stops": percentile(100),
    }
    if log:
        log(
            "增益图统计："
            f"中位 {stats['p50_stops']:.3f} stops/{2.0 ** float(stats['p50_stops']):.2f}×；"
            f"P95 {stats['p95_stops']:.3f} stops/{2.0 ** float(stats['p95_stops']):.2f}×；"
            f"P99 {stats['p99_stops']:.3f} stops/{2.0 ** float(stats['p99_stops']):.2f}×；"
            f"最大 {stats['max_stops']:.3f} stops/{2.0 ** float(stats['max_stops']):.2f}×"
        )
    return stats


def _load_gain(path: Path, size: tuple[int, int], gamma: float) -> np.ndarray:
    with Image.open(path) as image:
        image = ImageOps.exif_transpose(image)
        if image.mode not in ("L", "I;16", "I", "F"):
            image = image.convert("RGB")
        if image.size != size:
            image = image.resize(size, Image.Resampling.BICUBIC)
        source = np.asarray(image)
    source_dtype = source.dtype
    if source.ndim == 3:
        arr = source[..., :3].astype(np.float32)
        arr = arr @ np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)
    else:
        arr = source.astype(np.float32)
    if np.issubdtype(source_dtype, np.integer):
        max_value = float(np.iinfo(source_dtype).max)
    else:
        finite_max = float(np.nanmax(arr))
        max_value = finite_max if finite_max > 1.0 else 1.0
    gain = np.clip(arr / max(max_value, 1.0), 0.0, 1.0)
    if gamma != 1.0:
        gain = np.power(gain, gamma)
    return gain


def make_pq_alternate_from_gainmap(
    base_path: Path,
    gain_path: Path,
    output_png: Path,
    *,
    min_gain_stops: float,
    max_gain_stops: float,
    gain_gamma: float,
    log: LogFn | None = None,
    progress: ProgressFn | None = None,
    cancel_token: CancellationToken | None = None,
) -> dict[str, int]:
    if cancel_token:
        cancel_token.check()
    base = _load_rgb8(base_path)
    height, width = base.shape[:2]
    if progress:
        progress(25, "读取并对齐增益图…")
    gain = _load_gain(gain_path, (width, height), gain_gamma)
    stops_all = min_gain_stops + gain * (max_gain_stops - min_gain_stops)
    if log:
        log(
            f"增益图归一化范围：{float(gain.min()):.4f}–{float(gain.max()):.4f}；"
            f"映射 {min_gain_stops:.2f}–{max_gain_stops:.2f} stops"
        )
        for p in (50, 95, 99, 100):
            stops = float(np.percentile(stops_all, p))
            log(f"增益 P{p:02d}：{stops:.3f} stops，相比 SDR 亮 {2.0 ** stops:.3f}×")

    def rows() -> Iterator[np.ndarray]:
        chunk = 32
        for y0 in range(0, height, chunk):
            if cancel_token:
                cancel_token.check()
            y1 = min(height, y0 + chunk)
            rgb = base[y0:y1].astype(np.float32) / 255.0
            linear = srgb_to_linear(rgb)
            stops = stops_all[y0:y1, :, None]
            linear *= np.exp2(stops)
            pq = linear_srgb_to_pq2020(linear)
            encoded = np.clip(np.rint(pq * 65535.0), 0, 65535).astype(np.uint16)
            yield from encoded
            if progress:
                progress(35 + int(30 * y1 / height), "生成 Rec.2020 PQ HDR 参考图…")

    _write_rgb16_png(output_png, width, height, rows())
    return {"width": width, "height": height}

