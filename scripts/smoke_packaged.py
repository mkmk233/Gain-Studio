from __future__ import annotations

import ctypes
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes
from pathlib import Path

import png


def windows_for_pid(pid: int) -> list[tuple[bool, str]]:
    user32 = ctypes.windll.user32
    enum_proc_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    found: list[tuple[bool, str]] = []

    def callback(hwnd: int, _lparam: int) -> bool:
        window_pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(window_pid))
        if window_pid.value == pid:
            length = user32.GetWindowTextLengthW(hwnd)
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            found.append((bool(user32.IsWindowVisible(hwnd)), buffer.value))
        return True

    user32.EnumWindows(enum_proc_type(callback), 0)
    return found


def smoke_packaged_heic(exe: Path) -> int:
    with tempfile.TemporaryDirectory(prefix="gain-studio-packaged-") as folder:
        root = Path(folder)
        source = root / "pq16.png"
        output = root / "hdr.heic"
        width, height = 64, 32
        row: list[int] = []
        for x in range(width):
            value = int(65535 * x / (width - 1))
            row.extend((value, value, value))
        with source.open("wb") as stream:
            png.Writer(width, height, greyscale=False, bitdepth=16).write(
                stream, [row for _ in range(height)]
            )
        completed = subprocess.run(
            [str(exe), "--packaged-heic-smoke", str(source), str(output)],
            timeout=60,
            check=False,
        )
        if completed.returncode != 0 or not output.exists() or output.stat().st_size < 128:
            print(
                f"Packaged HEIC smoke test failed (code={completed.returncode}, output={output.exists()})",
                file=sys.stderr,
            )
            return 1
        import pillow_heif

        image = pillow_heif.open_heif(output, convert_hdr_to_8bit=False)[0]
        nclx = image.info.get("nclx_profile") or {}
        if image.info.get("bit_depth", 0) < 10 or nclx.get("color_primaries") != 9 or nclx.get("transfer_characteristics") != 16:
            print(f"Packaged HEIC validation failed: {image.info}", file=sys.stderr)
            return 1
        print(f"Packaged HEIC smoke test passed: {output.stat().st_size} bytes, {image.info.get('bit_depth')}-bit")
        return 0


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    exe = root / "dist" / "Gain Studio" / "Gain Studio.exe"
    if not exe.exists():
        print(f"Packaged executable not found: {exe}", file=sys.stderr)
        return 2

    process = subprocess.Popen([str(exe)])
    deadline = time.monotonic() + 15.0
    observed: list[tuple[bool, str]] = []
    try:
        while time.monotonic() < deadline:
            return_code = process.poll()
            observed = windows_for_pid(process.pid)
            if any(visible and title.startswith("Gain Studio") for visible, title in observed):
                print(f"GUI smoke test passed (PID {process.pid}): {observed}")
                return smoke_packaged_heic(exe)
            if return_code is not None:
                print(
                    f"Packaged app exited before its main window appeared "
                    f"(code {return_code}, windows={observed})",
                    file=sys.stderr,
                )
                return 1
            time.sleep(0.25)

        print(
            f"Timed out waiting for the packaged main window (windows={observed})",
            file=sys.stderr,
        )
        return 1
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
