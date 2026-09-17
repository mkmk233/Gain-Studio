from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class OutputFormat(str, Enum):
    AVIF = "avif"
    HEIC = "heic"


class InputMode(str, Enum):
    ARW_AUTO = "arw_auto"
    RAW_HIF = "raw_hif"
    RAW_GAIN_EXPORT = "raw_gain_export"
    SDR_HDR = "sdr_hdr"
    SDR_GAIN = "sdr_gain"
    JPEG_GAIN = "jpeg_gain"


@dataclass(slots=True)
class EncodeOptions:
    color_quality: int = 85
    gain_quality: int = 80
    speed: int = 6
    depth: int = 10
    yuv: str = "420"
    gain_depth: int = 8
    gain_yuv: str = "400"
    gain_downscale: int = 2
    max_headroom: float = 4.0
    exposure_comp: float = 0.0
    gain_gamma: float = 1.0
    min_gain_stops: float = 0.0
    jobs: int = 2
    preserve_metadata: bool = True
    keep_intermediates: bool = False
    output_format: OutputFormat = OutputFormat.AVIF

    def __post_init__(self) -> None:
        self.output_format = OutputFormat(self.output_format)


@dataclass(slots=True)
class ConvertRequest:
    mode: InputMode
    primary: Path
    secondary: Path | None
    output: Path
    options: EncodeOptions

    def __post_init__(self) -> None:
        # PySide may unwrap ``str, Enum`` values stored in QComboBox item data
        # back to plain strings. Normalize at the model boundary so every
        # caller sees a real InputMode and can safely use ``.value``.
        self.mode = InputMode(self.mode)
