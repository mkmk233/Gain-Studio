from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .tools import run_command, tool_path

LogFn = Callable[[str], None]


@dataclass(slots=True)
class HeicReport:
    path: Path
    summary: list[tuple[str, str]] = field(default_factory=list)
    auxiliary_types: list[str] = field(default_factory=list)
    raw_text: str = ""

    @property
    def has_gain_map(self) -> bool:
        return any("gainmap" in item.lower() or "gain map" in item.lower() for item in self.auxiliary_types)


def _values(text: str, tag: str) -> list[str]:
    pattern = re.compile(rf"\]\s+{re.escape(tag)}\s*:\s*(.*)$", re.MULTILINE)
    return [match.strip() for match in pattern.findall(text)]


def analyze_heic(path: Path, log: LogFn | None = None) -> HeicReport:
    result = run_command([tool_path("exiftool"), "-G1", "-a", "-s", "-u", path], log=None)
    text = result.stdout
    report = HeicReport(path=path, raw_text=text)
    aux = _values(text, "AuxiliaryImageType")
    report.auxiliary_types = list(dict.fromkeys(aux))

    dimensions = _values(text, "ImageSpatialExtent")
    depths = _values(text, "ImagePixelDepth")
    profiles = _values(text, "ProfileDescription")
    compatible = _values(text, "CompatibleBrands")
    fields = [
        ("文件", path.name),
        ("大小", f"{path.stat().st_size / (1024 * 1024):.2f} MiB"),
        ("主图尺寸", (_values(text, "ImageSize") or ["未知"])[-1]),
        ("容器品牌", ", ".join(compatible) if compatible else "未知"),
        ("图像项目尺寸", "、".join(dimensions) if dimensions else "未知"),
        ("像素深度", "、".join(depths) if depths else "未知"),
        ("增益图", "存在" if report.has_gain_map else "未发现"),
        ("HDR 增益图版本", ", ".join(_values(text, "HDRGainMapVersion")) or "未标注"),
        ("ICC 配置", "；".join(dict.fromkeys(profiles)) if profiles else "未发现"),
        ("拍摄时间", (_values(text, "DateTimeOriginal") or ["未知"])[0]),
        ("设备", " ".join((_values(text, "Make") + _values(text, "Model"))[:2]) or "未知"),
        ("镜头", (_values(text, "LensID") or _values(text, "LensModel") or ["未知"])[-1]),
        ("GPS", (_values(text, "GPSPosition") or ["无/未解析"])[-1]),
    ]
    report.summary.extend(fields)
    if log:
        log(f"分析完成：发现 {len(report.auxiliary_types)} 种辅助图像。")
    return report


def extract_heic_assets(path: Path, output_dir: Path, log: LogFn | None = None) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    before = set(output_dir.iterdir())
    output = output_dir / f"{path.stem}_base.png"
    run_command(
        [
            tool_path("heif-convert"),
            "--with-aux",
            "--with-exif",
            "--with-xmp",
            "--no-colons",
            path,
            output,
        ],
        log=log,
    )
    created = sorted(set(output_dir.iterdir()) - before)
    if log:
        log(f"已提取 {len(created)} 个文件到：{output_dir}")
    return created
