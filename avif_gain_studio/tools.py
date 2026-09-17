from __future__ import annotations

import locale
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Sequence

from .cancellation import CancellationToken, ConversionCancelled


LogFn = Callable[[str], None]


def app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    return Path(__file__).resolve().parent.parent


def executable_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return app_root()


def tool_path(name: str) -> Path:
    root = app_root()
    candidates: dict[str, list[Path]] = {
        "avifgainmaputil": [root / "tools/libavif/avifgainmaputil.exe"],
        "avifenc": [root / "tools/libavif/avifenc.exe"],
        "avifdec": [root / "tools/libavif/avifdec.exe"],
        "exiftool": [root / "tools/exiftool/ExifTool.exe"],
        "heif-convert": [root / "tools/libheif/heif-convert.exe"],
    }
    for candidate in candidates.get(name, []):
        if candidate.exists():
            return candidate
    found = shutil.which(name) or shutil.which(name + ".exe")
    if found:
        return Path(found)
    raise FileNotFoundError(f"找不到必需工具：{name}")


def subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    libheif = app_root() / "tools/libheif"
    libavif = app_root() / "tools/libavif"
    env["PATH"] = os.pathsep.join([str(libheif), str(libavif), env.get("PATH", "")])
    # Keep subprocess output deterministic and avoid Perl locale warnings.
    env.pop("LC_ALL", None)
    env.pop("LC_CTYPE", None)
    env.pop("LANG", None)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def quote_command(args: Sequence[str | Path]) -> str:
    return subprocess.list2cmdline([str(a) for a in args])


def decode_console_output(data: bytes) -> str:
    """Decode mixed Windows CLI output without turning Chinese into mojibake."""
    if not data:
        return ""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig", errors="replace")
    try:
        return data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        pass

    candidates: list[str] = []
    preferred = locale.getpreferredencoding(False)
    preferred_lower = preferred.lower().replace("-", "") if preferred else ""
    ordered = []
    if preferred_lower in {"gbk", "gb2312", "gb18030", "cp936", "ms936"}:
        ordered.append(preferred)
    ordered.extend(["gb18030", "cp936", preferred, "mbcs", "cp1252"])
    seen: set[str] = set()
    for encoding in ordered:
        key = encoding.lower() if encoding else ""
        if encoding and key not in seen:
            seen.add(key)
            candidates.append(encoding)
    for encoding in candidates:
        try:
            return data.decode(encoding, errors="strict")
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode(preferred or "utf-8", errors="replace")


def run_command(
    args: Sequence[str | Path],
    *,
    log: LogFn | None = None,
    cwd: Path | None = None,
    check: bool = True,
    cancel_token: CancellationToken | None = None,
) -> subprocess.CompletedProcess[str]:
    argv = [str(a) for a in args]
    if cancel_token:
        cancel_token.check()
    if log:
        log("执行命令：" + quote_command(argv))
        if cwd:
            log(f"工作目录：{cwd}")
    startupinfo = None
    creationflags = 0
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        creationflags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    started = time.perf_counter()
    proc = subprocess.Popen(
        argv,
        cwd=str(cwd) if cwd else None,
        env=subprocess_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        startupinfo=startupinfo,
        creationflags=creationflags,
    )
    if cancel_token:
        cancel_token.register_process(proc)
    output_bytes = b""
    try:
        while True:
            if cancel_token:
                cancel_token.check()
            try:
                output_bytes, _ = proc.communicate(timeout=0.10)
                break
            except subprocess.TimeoutExpired:
                continue
    except ConversionCancelled:
        try:
            proc.kill()
        except Exception:
            pass
        output_bytes, _ = proc.communicate()
        raise
    finally:
        if cancel_token:
            cancel_token.unregister_process(proc)
    elapsed = time.perf_counter() - started
    output = decode_console_output(output_bytes or b"")
    if log and output.strip():
        for line in output.rstrip().splitlines():
            log("  " + line)
    if log:
        log(f"命令结束：退出码 {proc.returncode}，耗时 {elapsed:.2f} 秒")
    completed = subprocess.CompletedProcess(argv, proc.returncode, output, None)
    if check and proc.returncode:
        tail = output[-3000:] if output else ""
        raise RuntimeError(f"命令失败（退出码 {proc.returncode}）：\n{tail}")
    return completed


def ensure_tools() -> dict[str, str]:
    versions: dict[str, str] = {}
    probes = {
        "libavif": [tool_path("avifgainmaputil"), "help"],
        "ExifTool": [tool_path("exiftool"), "-ver"],
        "libheif": [tool_path("heif-convert"), "--version"],
    }
    for label, cmd in probes.items():
        result = run_command(cmd, check=False)
        first = next((ln.strip() for ln in result.stdout.splitlines() if ln.strip()), "unknown")
        versions[label] = first
    return versions
