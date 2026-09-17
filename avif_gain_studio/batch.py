from __future__ import annotations

import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable

from .cancellation import CancellationToken, ConversionCancelled
from .models import ConvertRequest, EncodeOptions, InputMode, OutputFormat
from .pipeline import convert

LogFn = Callable[[str], None]
ProgressFn = Callable[[int, str], None]

RAW_EXTENSIONS = {".arw", ".dng", ".nef", ".cr2", ".cr3", ".raf", ".rw2", ".orf", ".pef"}
HIF_EXTENSIONS = {".hif", ".heif", ".heic"}


@dataclass(frozen=True, slots=True)
class RawHifPair:
    raw: Path
    hif: Path
    relative_parent: Path


@dataclass(slots=True)
class ScanResult:
    pairs: list[RawHifPair] = field(default_factory=list)
    unmatched_raw: list[Path] = field(default_factory=list)
    unmatched_hif: list[Path] = field(default_factory=list)


@dataclass(slots=True)
class BatchResult:
    succeeded: list[Path] = field(default_factory=list)
    failed: list[tuple[Path, str]] = field(default_factory=list)
    elapsed_seconds: float = 0.0
    cancelled: bool = False


def scan_raw_hif_pairs(directory: Path, recursive: bool = True) -> ScanResult:
    if not directory.is_dir():
        raise NotADirectoryError(f"目录不存在：{directory}")
    iterator = directory.rglob("*") if recursive else directory.glob("*")
    files = [path for path in iterator if path.is_file()]
    by_key: dict[tuple[str, str], list[Path]] = {}
    for path in files:
        suffix = path.suffix.lower()
        if suffix not in RAW_EXTENSIONS | HIF_EXTENSIONS:
            continue
        relative_parent = str(path.parent.relative_to(directory)).casefold()
        key = (relative_parent, path.stem.casefold())
        by_key.setdefault(key, []).append(path)

    result = ScanResult()
    for paths in by_key.values():
        raws = sorted((p for p in paths if p.suffix.lower() in RAW_EXTENSIONS), key=lambda p: p.name.casefold())
        hifs = sorted((p for p in paths if p.suffix.lower() in HIF_EXTENSIONS), key=lambda p: p.name.casefold())
        count = min(len(raws), len(hifs))
        for index in range(count):
            result.pairs.append(RawHifPair(raws[index], hifs[index], raws[index].parent.relative_to(directory)))
        result.unmatched_raw.extend(raws[count:])
        result.unmatched_hif.extend(hifs[count:])
    result.pairs.sort(key=lambda pair: str(pair.raw).casefold())
    result.unmatched_raw.sort(key=lambda path: str(path).casefold())
    result.unmatched_hif.sort(key=lambda path: str(path).casefold())
    return result


def batch_convert_raw_hif(
    pairs: list[RawHifPair],
    output_dir: Path,
    options: EncodeOptions,
    workers: int = 2,
    log: LogFn | None = None,
    progress: ProgressFn | None = None,
    cancel_token: CancellationToken | None = None,
) -> BatchResult:
    if not pairs:
        raise ValueError("批量队列为空，请先扫描 RAW+HIF 文件。")
    workers = max(1, min(int(workers), 32, len(pairs)))
    if cancel_token:
        cancel_token.check()
    output_dir.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    result = BatchResult()
    lock = threading.Lock()
    item_progress = {index: 0 for index in range(len(pairs))}

    def emit_overall(index: int, value: int, message: str) -> None:
        with lock:
            item_progress[index] = value
            overall = int(sum(item_progress.values()) / len(item_progress))
        if progress:
            progress(overall, f"[{index + 1}/{len(pairs)}] {pairs[index].raw.name}：{message}")

    def run_one(index: int, pair: RawHifPair) -> Path:
        if cancel_token:
            cancel_token.check()
        prefix = f"[{index + 1}/{len(pairs)} {pair.raw.stem}]"
        item_log = (lambda text: log(f"{prefix} {text}")) if log else None
        target_dir = output_dir / pair.relative_parent
        suffix = ".gain.heic" if options.output_format == OutputFormat.HEIC else ".gain.avif"
        target = target_dir / f"{pair.raw.stem}{suffix}"
        local_options = replace(options, jobs=workers)
        request = ConvertRequest(InputMode.RAW_HIF, pair.raw, pair.hif, target, local_options)
        if item_log:
            item_log(f"开始：RAW={pair.raw.name}，HIF={pair.hif.name}")
        return convert(
            request,
            log=item_log,
            progress=lambda value, message: emit_overall(index, value, message),
            cancel_token=cancel_token,
        )

    if log:
        log(
            f"批量处理开始：共 {len(pairs)} 对 RAW+HIF，并行任务数 {workers}，"
            f"输出格式 {options.output_format.value.upper()}，输出目录 {output_dir}"
        )
        if workers > 2:
            log("提示：每个 RAW 任务可能占用数百 MB 内存；高并发时请留意系统内存和页面文件。")

    executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="raw-hif")
    futures = {executor.submit(run_one, index, pair): (index, pair) for index, pair in enumerate(pairs)}
    try:
        for future in as_completed(futures):
            index, pair = futures[future]
            try:
                output = future.result()
                result.succeeded.append(output)
                with lock:
                    item_progress[index] = 100
                if log:
                    log(f"[{index + 1}/{len(pairs)} {pair.raw.stem}] 成功：{output}")
            except ConversionCancelled:
                result.cancelled = True
                if log:
                    log(f"[{index + 1}/{len(pairs)} {pair.raw.stem}] 已停止。")
            except Exception as exc:
                summary = f"{type(exc).__name__}: {exc}"
                detail = traceback.format_exc().rstrip()
                result.failed.append((pair.raw, summary))
                with lock:
                    item_progress[index] = 100
                if log:
                    log(f"[{index + 1}/{len(pairs)} {pair.raw.stem}] 失败：{summary}\n{detail}")
            if cancel_token and cancel_token.cancelled:
                result.cancelled = True
                for pending in futures:
                    pending.cancel()
                break
            if progress:
                with lock:
                    overall = int(sum(item_progress.values()) / len(item_progress))
                progress(overall, f"已完成 {len(result.succeeded) + len(result.failed)}/{len(pairs)}")
    finally:
        executor.shutdown(wait=True, cancel_futures=True)

    result.succeeded.sort(key=lambda path: str(path).casefold())
    result.failed.sort(key=lambda item: str(item[0]).casefold())
    result.elapsed_seconds = time.perf_counter() - started
    if log:
        state = "已停止" if result.cancelled else "结束"
        log(
            f"批量处理{state}：成功 {len(result.succeeded)}，失败 {len(result.failed)}，"
            f"总耗时 {result.elapsed_seconds:.2f} 秒"
        )
    if progress:
        if result.cancelled:
            progress(0, f"已停止：已完成 {len(result.succeeded)}，失败 {len(result.failed)}")
        else:
            progress(100, f"批量完成：成功 {len(result.succeeded)}，失败 {len(result.failed)}")
    return result

