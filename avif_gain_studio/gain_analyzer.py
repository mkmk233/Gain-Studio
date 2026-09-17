from __future__ import annotations

import math
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np
from PIL import Image, ImageOps

from .tools import run_command, tool_path

LogFn = Callable[[str], None]
ProgressFn = Callable[[int, str], None]


@dataclass(slots=True)
class GainMapReport:
    path: Path
    summary: list[tuple[str, str]] = field(default_factory=list)
    raw_text: str = ""
    source_kind: str = "独立增益图"


def _metadata_triplet(text: str, label: str, default: float) -> np.ndarray:
    match = re.search(rf"{re.escape(label)}:\s+R\s+([-+0-9.eE]+).*?G\s+([-+0-9.eE]+).*?B\s+([-+0-9.eE]+)", text)
    if not match:
        return np.full(3, default, dtype=np.float32)
    return np.array([float(match.group(i)) for i in range(1, 4)], dtype=np.float32)


def _headroom(text: str, label: str) -> float | None:
    match = re.search(rf"{re.escape(label)}:\s+([-+0-9.eE]+)", text)
    return float(match.group(1)) if match else None


def _load_normalized_sample(path: Path, max_samples: int = 2_000_000) -> tuple[np.ndarray, tuple[int, int], str, int]:
    with Image.open(path) as image:
        image = ImageOps.exif_transpose(image)
        width, height = image.size
        mode = image.mode
        source = np.asarray(image)
    if source.ndim == 2:
        source = source[..., None]
    if source.shape[2] > 3:
        source = source[..., :3]
    step = max(1, math.ceil(math.sqrt((width * height) / max_samples)))
    source = source[::step, ::step]
    dtype = source.dtype
    array = source.astype(np.float32)
    if np.issubdtype(dtype, np.integer):
        denominator = float(np.iinfo(dtype).max)
    else:
        finite = array[np.isfinite(array)]
        max_value = float(finite.max()) if finite.size else 1.0
        denominator = max_value if max_value > 1.0 else 1.0
    return np.clip(array / max(denominator, 1.0), 0.0, 1.0), (width, height), mode, step


def analyze_gain_map(
    path: Path,
    min_gain_stops: float = 0.0,
    max_gain_stops: float = 4.0,
    gain_gamma: float = 1.0,
    log: LogFn | None = None,
    progress: ProgressFn | None = None,
) -> GainMapReport:
    if not path.is_file():
        raise FileNotFoundError(f"增益图文件不存在：{path}")
    if progress:
        progress(5, "读取增益图元数据…")
    metadata_text = ""
    source_kind = "独立增益图"
    analysis_path = path
    temp: tempfile.TemporaryDirectory[str] | None = None
    gain_min = np.full(3, min_gain_stops, dtype=np.float32)
    gain_max = np.full(3, max_gain_stops, dtype=np.float32)
    gamma = np.full(3, gain_gamma, dtype=np.float32)
    base_headroom = None
    alternate_headroom = None
    try:
        if path.suffix.lower() == ".avif":
            source_kind = "AVIF 内嵌增益图"
            metadata_text = run_command(
                [tool_path("avifgainmaputil"), "printmetadata", path], log=log
            ).stdout
            gain_min = _metadata_triplet(metadata_text, "Gain Map Min", min_gain_stops)
            gain_max = _metadata_triplet(metadata_text, "Gain Map Max", max_gain_stops)
            gamma = _metadata_triplet(metadata_text, "Gain Map Gamma", gain_gamma)
            base_headroom = _headroom(metadata_text, "Base headroom")
            alternate_headroom = _headroom(metadata_text, "Alternate headroom")
            temp = tempfile.TemporaryDirectory(prefix="gain-analysis-")
            analysis_path = Path(temp.name) / "gain_map.png"
            run_command(
                [tool_path("avifgainmaputil"), "extractgainmap", path, analysis_path, "--qcolor", "100"],
                log=log,
            )
        if progress:
            progress(35, "采样增益图像素…")
        normalized, (width, height), mode, sample_step = _load_normalized_sample(analysis_path)
        if log:
            log(
                f"增益图像素：{width}×{height}，模式={mode}，采样步长={sample_step}，"
                f"实际采样={normalized.shape[0] * normalized.shape[1]:,} 像素"
            )
        if normalized.shape[2] == 1:
            normalized = np.repeat(normalized, 3, axis=2)
        elif normalized.shape[2] == 2:
            normalized = np.repeat(normalized[:, :, :1], 3, axis=2)
        gamma = np.maximum(gamma, 1e-6)
        stops_rgb = gain_min + np.power(normalized[:, :, :3], gamma) * (gain_max - gain_min)
        # Luminance-equivalent brightness multiplier, easier to interpret than raw map code values.
        multiplier_rgb = np.exp2(stops_rgb)
        multiplier = multiplier_rgb @ np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)
        effective_stops = np.log2(np.maximum(multiplier, 1e-12))
        flat_stops = effective_stops.reshape(-1)
        flat_multiplier = multiplier.reshape(-1)
        if progress:
            progress(75, "计算亮度倍数与分布…")

        percentiles = {p: float(np.percentile(flat_stops, p)) for p in (0, 1, 5, 50, 90, 95, 99, 100)}
        mean_stops = float(np.mean(flat_stops))
        mean_multiplier = float(np.mean(flat_multiplier))
        thresholds = [1.0, 2.0, 4.0, 8.0, 16.0]
        coverage = {value: float(np.mean(flat_multiplier >= value) * 100.0) for value in thresholds}
        report = GainMapReport(path=path, source_kind=source_kind)
        report.summary.extend(
            [
                ("来源类型", source_kind),
                ("文件", path.name),
                ("图像尺寸", f"{width}×{height}"),
                ("像素模式", mode),
                ("分析采样数", f"{flat_stops.size:,}"),
                ("增益映射最小值", " / ".join(f"{v:.4f}" for v in gain_min) + " stops (R/G/B)"),
                ("增益映射最大值", " / ".join(f"{v:.4f}" for v in gain_max) + " stops (R/G/B)"),
                ("Gamma", " / ".join(f"{v:.4f}" for v in gamma) + " (R/G/B)"),
                ("平均增益", f"{mean_stops:.3f} stops；平均亮度倍数 {mean_multiplier:.3f}×"),
                ("中位增益 P50", f"{percentiles[50]:.3f} stops；{2.0 ** percentiles[50]:.3f}× SDR"),
                ("高亮增益 P90", f"{percentiles[90]:.3f} stops；{2.0 ** percentiles[90]:.3f}× SDR"),
                ("高亮增益 P95", f"{percentiles[95]:.3f} stops；{2.0 ** percentiles[95]:.3f}× SDR"),
                ("高亮增益 P99", f"{percentiles[99]:.3f} stops；{2.0 ** percentiles[99]:.3f}× SDR"),
                ("最大增益", f"{percentiles[100]:.3f} stops；{2.0 ** percentiles[100]:.3f}× SDR"),
            ]
        )
        if base_headroom is not None:
            report.summary.append(("Base headroom", f"{base_headroom:.4f} stops；{2.0 ** base_headroom:.3f}×"))
        if alternate_headroom is not None:
            report.summary.append(("Alternate headroom", f"{alternate_headroom:.4f} stops；{2.0 ** alternate_headroom:.3f}×"))
        for threshold in thresholds:
            report.summary.append((f"≥ {threshold:g}× SDR 的像素", f"{coverage[threshold]:.3f}%"))

        histogram_lines = ["\n亮度倍数覆盖率："]
        histogram_lines.extend(f"  ≥ {threshold:g}× SDR: {coverage[threshold]:.3f}%" for threshold in thresholds)
        histogram_lines.append("\nStops 百分位：")
        histogram_lines.extend(
            f"  P{p:02d}: {percentiles[p]:.4f} stops = {2.0 ** percentiles[p]:.4f}× SDR"
            for p in (0, 1, 5, 50, 90, 95, 99, 100)
        )
        report.raw_text = (metadata_text.rstrip() + "\n" if metadata_text else "") + "\n".join(histogram_lines)
        if log:
            log(
                f"增益图分析完成：P50={percentiles[50]:.3f} stops/{2.0 ** percentiles[50]:.3f}×，"
                f"P95={percentiles[95]:.3f} stops/{2.0 ** percentiles[95]:.3f}×，"
                f"最大={percentiles[100]:.3f} stops/{2.0 ** percentiles[100]:.3f}×"
            )
        if progress:
            progress(100, "增益图分析完成")
        return report
    finally:
        if temp is not None:
            temp.cleanup()
