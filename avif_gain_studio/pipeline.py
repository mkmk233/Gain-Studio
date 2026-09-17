from __future__ import annotations

import shutil
import tempfile
import time
from io import BytesIO
from pathlib import Path
from typing import Callable, Iterable

from PIL import Image, ImageCms

from .cancellation import CancellationToken
from .hdr import export_gain_map_from_raw, make_pq_alternate_from_gainmap, make_pq_alternate_from_raw
from .heic_encoder import encode_hdr_heic, validate_heic
from .models import ConvertRequest, InputMode, OutputFormat
from .tools import run_command, tool_path

LogFn = Callable[[str], None]
ProgressFn = Callable[[int, str], None]


def _emit(progress: ProgressFn | None, value: int, message: str) -> None:
    if progress:
        progress(value, message)


def _inject_metadata(source: Path, target: Path, log: LogFn | None, cancel_token: CancellationToken | None = None) -> None:
    command = [
        tool_path("exiftool"),
        "-overwrite_original",
        "-P",
        "-TagsFromFile",
        source,
        "-EXIF:all",
        "-XMP:all",
        "-IPTC:all",
        # Do not copy the source ICC/color-space tags blindly: the pixel data is
        # converted and the AVIF encoder writes the output CICP signaling.
        "-Orientation#=1",
        target,
    ]
    result = run_command(command, log=log, check=False, cancel_token=cancel_token)
    if result.returncode and log:
        log(f"警告：从 {source.name} 复制部分元数据失败，但图像处理可继续。")


def _inject_metadata_sources(sources: Iterable[Path], target: Path, log: LogFn | None, cancel_token: CancellationToken | None = None) -> None:
    for source in sources:
        if source.exists():
            if log:
                log(f"复制元数据：{source.name} → {target.name}")
            _inject_metadata(source, target, log, cancel_token)


def _extract_raw_jpeg(source: Path, target: Path, log: LogFn | None, cancel_token: CancellationToken | None = None) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if log:
        log(f"尝试从 RAW 提取全尺寸 JpgFromRaw：{source.name}")
    result = run_command(
        [tool_path("exiftool"), "-b", "-JpgFromRaw", "-W!", target, source],
        log=log,
        check=False,
        cancel_token=cancel_token,
    )
    if result.returncode or not target.exists() or target.stat().st_size < 1024:
        if log:
            log("未找到可用的 JpgFromRaw，回退到 PreviewImage。")
        run_command(
            [tool_path("exiftool"), "-b", "-PreviewImage", "-W!", target, source],
            log=log,
            cancel_token=cancel_token,
        )
    if not target.exists() or target.stat().st_size < 1024:
        raise RuntimeError("RAW 中没有可用的 JpgFromRaw/PreviewImage。")
    if log:
        log(f"已提取 SDR 底图：{target}（{target.stat().st_size / 1024:.1f} KiB）")


def _normalize_png_to_srgb(path: Path, log: LogFn | None, cancel_token: CancellationToken | None = None) -> None:
    """Convert an embedded ICC profile to sRGB, then remove it from the work PNG.

    libavif cannot compute a gain map when either source carries an ICC profile.
    Converting instead of merely deleting Display-P3 keeps the SDR appearance.
    """
    with Image.open(path) as image:
        icc_bytes = image.info.get("icc_profile")
        if not icc_bytes:
            if log:
                log("HIF 解码图未携带 ICC，按 sRGB 工作色彩空间继续。")
            return
        source_name = "未知 ICC"
        try:
            source_profile = ImageCms.ImageCmsProfile(BytesIO(icc_bytes))
            source_name = ImageCms.getProfileDescription(source_profile).strip() or source_name
            destination_profile = ImageCms.createProfile("sRGB")
            converted = ImageCms.profileToProfile(
                image.convert("RGB"),
                source_profile,
                destination_profile,
                outputMode="RGB",
            )
            temporary = path.with_name(path.stem + ".srgb.tmp.png")
            converted.info.pop("icc_profile", None)
            converted.save(temporary, format="PNG", compress_level=1, icc_profile=None)
            temporary.replace(path)
            if log:
                log(f"工作色彩空间转换：{source_name} → sRGB；已移除中间 PNG 的 ICC，避免 gain-map 编码冲突。")
        except Exception as exc:
            if log:
                log(f"警告：ICC 到 sRGB 转换失败（{exc}），仅移除工作文件 ICC。")
            run_command(
                [tool_path("exiftool"), "-overwrite_original", "-ICC_Profile=", path],
                log=log,
                cancel_token=cancel_token,
            )


def _decode_hif_to_png(source: Path, target: Path, jobs: int, log: LogFn | None, cancel_token: CancellationToken | None = None) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    codec_threads = 1 if jobs > 1 else 0
    if log:
        log(f"将 HIF/HEIF 解码为 SDR PNG：{source.name}；解码线程={codec_threads or '自动'}")
    run_command(
        [
            tool_path("heif-convert"),
            "--auto-correct",
            "--codec-threads",
            str(codec_threads),
            "--png-compression-level",
            "1",
            source,
            target,
        ],
        log=log,
        cancel_token=cancel_token,
    )
    decoded: Path | None = None
    if target.exists() and target.stat().st_size >= 1024:
        decoded = target
    else:
        candidates = sorted(
            target.parent.glob(target.stem + "*.png"),
            key=lambda p: p.stat().st_size,
            reverse=True,
        )
        if candidates:
            if candidates[0] != target:
                shutil.move(str(candidates[0]), str(target))
            decoded = target
    if decoded is None:
        raise RuntimeError(f"HIF/HEIF 解码后没有生成有效图像：{source}")
    _normalize_png_to_srgb(decoded, log, cancel_token)
    return decoded


def _combine_command(base: Path, alternate: Path, output: Path, request: ConvertRequest) -> list[str | Path]:
    opt = request.options
    command: list[str | Path] = [
        tool_path("avifgainmaputil"),
        "combine",
        base,
        alternate,
        output,
        "--downscaling",
        str(opt.gain_downscale),
        "--qgain-map",
        str(opt.gain_quality),
        "--depth-gain-map",
        str(opt.gain_depth),
        "--yuv-gain-map",
        opt.gain_yuv,
        "--max-headroom",
        str(opt.max_headroom),
        "--speed",
        str(opt.speed),
        "--qcolor",
        str(opt.color_quality),
        "--yuv",
        opt.yuv,
        "--depth",
        str(opt.depth),
    ]
    if request.mode in (InputMode.ARW_AUTO, InputMode.RAW_HIF, InputMode.SDR_GAIN):
        command.extend(["--cicp-base", "1/13/6", "--cicp-alternate", "9/16/9"])
    return command


def validate_avif(path: Path, log: LogFn | None = None, cancel_token: CancellationToken | None = None) -> str:
    if not path.exists() or path.stat().st_size < 128:
        raise RuntimeError("输出文件没有生成。")
    info = run_command([tool_path("avifdec"), "--info", path], log=log, cancel_token=cancel_token).stdout
    metadata = run_command([tool_path("avifgainmaputil"), "printmetadata", path], log=log, cancel_token=cancel_token).stdout
    lowered = metadata.lower()
    if "gain map" not in lowered and "gainmap" not in lowered and "gain_map" not in lowered:
        raise RuntimeError("输出 AVIF 未通过增益图验证。")
    return info + "\n" + metadata


def _encode_base_and_alternate(
    base: Path,
    alternate: Path,
    output: Path,
    request: ConvertRequest,
    log: LogFn | None,
    progress: ProgressFn | None,
    cancel_token: CancellationToken | None,
) -> None:
    if request.options.output_format == OutputFormat.AVIF:
        _emit(progress, 68, "编码 SDR + 增益图 AVIF…")
        run_command(_combine_command(base, alternate, output, request), log=log, cancel_token=cancel_token)
        return
    if alternate.suffix.lower() != ".png":
        raise ValueError("HEIC HDR 输出目前要求 HDR 参考图为 PNG；RAW 与指定增益图模式会自动生成兼容的 16-bit PNG。")
    encode_hdr_heic(
        alternate,
        output,
        quality=request.options.color_quality,
        depth=request.options.depth,
        chroma=request.options.yuv,
        speed=request.options.speed,
        log=log,
        progress=progress,
        cancel_token=cancel_token,
    )


def _describe_request(request: ConvertRequest, log: LogFn | None) -> None:
    if not log:
        return
    opt = request.options
    log("=" * 72)
    log(f"任务模式：{request.mode.value}")
    log(f"主输入：{request.primary}")
    if request.secondary:
        log(f"辅助输入：{request.secondary}")
    log(f"输出文件：{request.output}")
    log(f"输出格式：{request.options.output_format.value.upper()}")
    log(
        "编码参数："
        f"主图质量={opt.color_quality}，增益图质量={opt.gain_quality}，速度={opt.speed}，"
        f"主图={opt.depth}-bit YUV{opt.yuv}，增益图={opt.gain_depth}-bit YUV{opt.gain_yuv}，"
        f"增益图缩小={opt.gain_downscale}×，HDR 上限={opt.max_headroom:.2f} stops，"
        f"曝光微调={opt.exposure_comp:+.2f} EV，保留元数据={'是' if opt.preserve_metadata else '否'}"
    )


def convert(
    request: ConvertRequest,
    log: LogFn | None = None,
    progress: ProgressFn | None = None,
    cancel_token: CancellationToken | None = None,
) -> Path:
    started = time.perf_counter()
    if cancel_token:
        cancel_token.check()
    request.output.parent.mkdir(parents=True, exist_ok=True)
    opt = request.options
    _describe_request(request, log)
    temp: tempfile.TemporaryDirectory[str] | None = None
    if opt.keep_intermediates:
        work = request.output.parent / f"{request.output.stem}_intermediates"
        work.mkdir(parents=True, exist_ok=True)
        cleanup = False
    else:
        temp = tempfile.TemporaryDirectory(prefix="avif-gain-")
        work = Path(temp.name)
        cleanup = True

    export_only = request.mode == InputMode.RAW_GAIN_EXPORT
    container_suffix = ".heic" if opt.output_format == OutputFormat.HEIC else ".avif"
    temp_output = work / (request.output.stem + (".pending.png" if export_only else f".pending{container_suffix}"))
    metadata_sources: list[Path] = [request.primary]
    try:
        _emit(progress, 5, "准备输入…")
        if request.mode == InputMode.ARW_AUTO:
            base = work / "sdr_embedded.jpg"
            _extract_raw_jpeg(request.primary, base, log, cancel_token)
            alternate = work / "hdr_reference_pq.png"
            make_pq_alternate_from_raw(
                request.primary,
                base,
                alternate,
                max_headroom_stops=opt.max_headroom,
                exposure_comp=opt.exposure_comp,
                log=log,
                progress=progress,
                cancel_token=cancel_token,
            )
            _encode_base_and_alternate(base, alternate, temp_output, request, log, progress, cancel_token)

        elif request.mode == InputMode.RAW_HIF:
            if request.secondary is None:
                raise ValueError("请选择与 RAW 同名的 HIF/HEIF 文件。")
            base = work / "sdr_from_hif.png"
            _decode_hif_to_png(request.secondary, base, opt.jobs, log, cancel_token)
            metadata_sources = [request.secondary, request.primary]
            alternate = work / "hdr_reference_pq.png"
            make_pq_alternate_from_raw(
                request.primary,
                base,
                alternate,
                max_headroom_stops=opt.max_headroom,
                exposure_comp=opt.exposure_comp,
                log=log,
                progress=progress,
                cancel_token=cancel_token,
            )
            _encode_base_and_alternate(base, alternate, temp_output, request, log, progress, cancel_token)

        elif request.mode == InputMode.RAW_GAIN_EXPORT:
            if request.secondary is None:
                base = work / "sdr_embedded.jpg"
                _extract_raw_jpeg(request.primary, base, log, cancel_token)
            elif request.secondary.suffix.lower() in {".hif", ".heif", ".heic"}:
                base = work / "sdr_from_hif.png"
                _decode_hif_to_png(request.secondary, base, opt.jobs, log, cancel_token)
                metadata_sources = [request.secondary, request.primary]
            else:
                base = request.secondary
                metadata_sources = [request.secondary, request.primary]
            export_gain_map_from_raw(
                request.primary,
                base,
                temp_output,
                max_headroom_stops=opt.max_headroom,
                exposure_comp=opt.exposure_comp,
                log=log,
                progress=progress,
                cancel_token=cancel_token,
            )

        elif request.mode == InputMode.SDR_GAIN:
            if request.secondary is None:
                raise ValueError("请选择增益图。")
            base = request.primary
            alternate = work / "hdr_reference_pq.png"
            make_pq_alternate_from_gainmap(
                base,
                request.secondary,
                alternate,
                min_gain_stops=opt.min_gain_stops,
                max_gain_stops=opt.max_headroom,
                gain_gamma=opt.gain_gamma,
                log=log,
                progress=progress,
                cancel_token=cancel_token,
            )
            _encode_base_and_alternate(base, alternate, temp_output, request, log, progress, cancel_token)

        elif request.mode == InputMode.SDR_HDR:
            if request.secondary is None:
                raise ValueError("请选择 HDR 参考图。")
            _emit(progress, 20, "由 SDR/HDR 图像生成输出…")
            _encode_base_and_alternate(request.primary, request.secondary, temp_output, request, log, progress, cancel_token)

        elif request.mode == InputMode.JPEG_GAIN:
            command: list[str | Path] = [
                tool_path("avifgainmaputil"),
                "convert",
                request.primary,
                temp_output,
                "--qgain-map",
                str(opt.gain_quality),
                "--speed",
                str(opt.speed),
                "--qcolor",
                str(opt.color_quality),
                "--yuv",
                opt.yuv,
                "--depth",
                str(opt.depth),
            ]
            _emit(progress, 20, "转换 JPEG 增益图…")
            if opt.output_format == OutputFormat.AVIF:
                run_command(command, log=log, cancel_token=cancel_token)
            else:
                intermediate_avif = work / "ultrahdr_intermediate.avif"
                command[2] = intermediate_avif
                run_command(command, log=log, cancel_token=cancel_token)
                alternate = work / "hdr_reference_pq.png"
                run_command(
                    [
                        tool_path("avifgainmaputil"), "tonemap", intermediate_avif, alternate,
                        "--headroom", str(opt.max_headroom), "--cicp-output", "9/16/9",
                    ],
                    log=log,
                    cancel_token=cancel_token,
                )
                encode_hdr_heic(
                    alternate, temp_output, quality=opt.color_quality, depth=opt.depth,
                    chroma=opt.yuv, speed=opt.speed, log=log, progress=progress,
                    cancel_token=cancel_token,
                )
        else:  # pragma: no cover
            raise ValueError(f"不支持的模式：{request.mode}")

        if opt.keep_intermediates and log:
            log(f"已保留中间文件：{work}")

        _emit(progress, 88, "写入元数据…")
        if opt.preserve_metadata:
            _inject_metadata_sources(metadata_sources, temp_output, log, cancel_token)

        if not export_only and opt.output_format == OutputFormat.AVIF:
            _emit(progress, 93, "验证 AVIF 与增益图元数据…")
            validate_avif(temp_output, log=log, cancel_token=cancel_token)
        elif not export_only:
            _emit(progress, 93, "验证 HEIC HDR 位深与色彩信令…")
            validate_heic(temp_output, log=log)
        elif not temp_output.exists() or temp_output.stat().st_size < 128:
            raise RuntimeError("RAW 增益图没有生成。")

        if cancel_token:
            cancel_token.check()
        if request.output.exists():
            request.output.unlink()
        shutil.move(str(temp_output), str(request.output))
        _emit(progress, 100, "完成")
        if log:
            elapsed = time.perf_counter() - started
            size_mb = request.output.stat().st_size / (1024 * 1024)
            kind = "RAW 增益图" if export_only else opt.output_format.value.upper()
            log(f"{kind} 完成：{request.output}（{size_mb:.2f} MiB，总耗时 {elapsed:.2f} 秒）")
            log("=" * 72)
        return request.output
    finally:
        if cleanup and temp is not None:
            temp.cleanup()
