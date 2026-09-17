from __future__ import annotations

import multiprocessing
import queue
import time
from pathlib import Path
from typing import Callable

import numpy as np
import png

from .cancellation import CancellationToken, ConversionCancelled

LogFn = Callable[[str], None]
ProgressFn = Callable[[int, str], None]

_PRESETS = ("placebo", "veryslow", "slower", "slow", "medium", "medium", "fast", "faster", "veryfast", "superfast", "ultrafast")


def _read_png_rgb16(path: Path) -> tuple[int, int, np.ndarray]:
    reader = png.Reader(filename=str(path))
    width, height, rows, info = reader.asDirect()
    planes = int(info.get("planes", 3))
    bitdepth = int(info.get("bitdepth", 8))
    greyscale = bool(info.get("greyscale", False))
    alpha = bool(info.get("alpha", False))
    image = np.empty((height, width, 3), dtype=np.uint16)
    maximum = (1 << bitdepth) - 1
    for y, row in enumerate(rows):
        values = np.asarray(row, dtype=np.uint16).reshape(width, planes)
        if greyscale:
            rgb = np.repeat(values[:, :1], 3, axis=1)
        else:
            rgb = values[:, :3] if not alpha else values[:, :3]
        if bitdepth != 16:
            rgb = np.rint(rgb.astype(np.float32) * (65535.0 / maximum)).astype(np.uint16)
        image[y] = rgb
    return width, height, image


def _encode_worker(
    source_png: str,
    output_heic: str,
    quality: int,
    depth: int,
    chroma: str,
    speed: int,
    result_queue,
) -> None:
    try:
        import pillow_heif

        width, height, rgb16 = _read_png_rgb16(Path(source_png))
        actual_depth = 12 if depth == 12 else 10
        shift = 16 - actual_depth
        packed = np.right_shift(rgb16, shift).astype("<u2", copy=False)
        heif = pillow_heif.from_bytes(f"RGB;{actual_depth}", (width, height), packed.tobytes())
        heif.save(
            output_heic,
            quality=max(0, min(int(quality), 100)),
            chroma=chroma if chroma in {"420", "422", "444"} else "420",
            enc_params={"preset": _PRESETS[max(0, min(int(speed), 10))]},
            save_nclx_profile=True,
            color_primaries=9,
            transfer_characteristics=16,
            matrix_coefficients=9,
            full_range_flag=0,
        )
        result_queue.put((True, ""))
    except BaseException as exc:
        result_queue.put((False, f"{type(exc).__name__}: {exc}"))


def encode_hdr_heic(
    source_png: Path,
    output_heic: Path,
    *,
    quality: int,
    depth: int,
    chroma: str,
    speed: int,
    log: LogFn | None = None,
    progress: ProgressFn | None = None,
    cancel_token: CancellationToken | None = None,
) -> None:
    """Encode a Rec.2020/PQ 16-bit PNG as a 10/12-bit HDR HEIC.

    HEIC HDR is encoded as a PQ primary image. Unlike the AVIF path, this does
    not embed a backward-compatible SDR gain-map base image.
    """
    if cancel_token:
        cancel_token.check()
    output_heic.parent.mkdir(parents=True, exist_ok=True)
    actual_depth = 12 if depth == 12 else 10
    if log:
        log(
            f"编码 HEIC HDR：{actual_depth}-bit HEVC，Rec.2020/PQ，YUV{chroma}，"
            f"质量={quality}，速度={speed}。"
        )
        if depth == 8:
            log("HEIC HDR 至少需要 10-bit；已将主图位深从 8-bit 自动提升为 10-bit。")
    if progress:
        progress(70, "编码 10/12-bit HDR HEIC…")

    context = multiprocessing.get_context("spawn")
    result_queue = context.Queue()
    process = context.Process(
        target=_encode_worker,
        args=(str(source_png), str(output_heic), quality, actual_depth, chroma, speed, result_queue),
        name="heic-hdr-encoder",
    )
    process.start()
    if cancel_token:
        cancel_token.register_process(process)
    try:
        while process.is_alive():
            if cancel_token:
                cancel_token.check()
            process.join(0.10)
        process.join()
        try:
            ok, detail = result_queue.get_nowait()
        except queue.Empty:
            ok, detail = False, f"HEIC 编码进程异常退出（退出码 {process.exitcode}）。"
        if not ok or process.exitcode:
            raise RuntimeError(detail or f"HEIC 编码失败（退出码 {process.exitcode}）。")
    except ConversionCancelled:
        if process.is_alive():
            process.terminate()
            process.join(3)
        raise
    finally:
        if cancel_token:
            cancel_token.unregister_process(process)
        result_queue.close()
    if not output_heic.exists() or output_heic.stat().st_size < 128:
        raise RuntimeError("HEIC 编码器没有生成有效输出文件。")


def validate_heic(path: Path, log: LogFn | None = None) -> dict[str, object]:
    import pillow_heif

    if not path.exists() or path.stat().st_size < 128:
        raise RuntimeError("输出 HEIC 文件没有生成。")
    image = pillow_heif.open_heif(path, convert_hdr_to_8bit=False)[0]
    info = image.info
    depth = int(info.get("bit_depth", 0))
    nclx = info.get("nclx_profile") or {}
    primaries = int(nclx.get("color_primaries", -1))
    transfer = int(nclx.get("transfer_characteristics", -1))
    matrix = int(nclx.get("matrix_coefficients", -1))
    if depth < 10 or primaries != 9 or transfer != 16:
        raise RuntimeError(
            f"HEIC HDR 验证失败：位深={depth}，CICP={primaries}/{transfer}/{matrix}。"
        )
    if log:
        log(f"HEIC HDR 验证通过：{image.size[0]}×{image.size[1]}，{depth}-bit，CICP={primaries}/{transfer}/{matrix}。")
    return {"size": image.size, "bit_depth": depth, "nclx": nclx}
