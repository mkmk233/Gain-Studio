from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from pathlib import Path

import numpy as np
import png
from PIL import Image

from avif_gain_studio.batch import RawHifPair, batch_convert_raw_hif, scan_raw_hif_pairs
from avif_gain_studio.cancellation import CancellationToken, ConversionCancelled
from avif_gain_studio.gain_analyzer import analyze_gain_map
from avif_gain_studio.hdr import linear_srgb_to_pq2020, srgb_to_linear
from avif_gain_studio.heic_analyzer import analyze_heic
from avif_gain_studio.heic_encoder import encode_hdr_heic, validate_heic
from avif_gain_studio.models import ConvertRequest, EncodeOptions, InputMode, OutputFormat
from avif_gain_studio.pipeline import convert, validate_avif
from avif_gain_studio.tools import decode_console_output, run_command
from avif_gain_studio.worker import FunctionWorker


class ColorMathTests(unittest.TestCase):
    def test_srgb_endpoints(self) -> None:
        values = np.array([0.0, 0.04045, 1.0], dtype=np.float32)
        out = srgb_to_linear(values)
        self.assertAlmostEqual(float(out[0]), 0.0, places=6)
        self.assertAlmostEqual(float(out[-1]), 1.0, places=5)

    def test_pq_is_monotonic(self) -> None:
        values = np.zeros((4, 1, 3), dtype=np.float32)
        values[:, 0, :] = np.array([[0.0], [0.18], [1.0], [4.0]])
        out = linear_srgb_to_pq2020(values)[:, 0, 1]
        self.assertTrue(np.all(np.diff(out) > 0))
        self.assertTrue(np.all((out >= 0) & (out <= 1)))


class UtilityTests(unittest.TestCase):
    def test_console_output_decoding(self) -> None:
        text = "中文日志：任务完成"
        self.assertEqual(decode_console_output(text.encode("utf-8")), text)
        self.assertEqual(decode_console_output(text.encode("gb18030")), text)

    def test_convert_request_normalizes_string_mode(self) -> None:
        request = ConvertRequest(
            "raw_hif", Path("photo.ARW"), Path("photo.HIF"), Path("photo.avif"), EncodeOptions()
        )
        self.assertIs(request.mode, InputMode.RAW_HIF)
        self.assertEqual(request.mode.value, "raw_hif")

    def test_worker_only_injects_supported_callbacks(self) -> None:
        received = []
        worker = FunctionWorker(lambda value: value * 2, 21)
        worker.finished.connect(received.append)
        worker.run()
        self.assertEqual(received, [42])

    def test_encode_options_normalizes_output_format(self) -> None:
        options = EncodeOptions(output_format="heic")
        self.assertIs(options.output_format, OutputFormat.HEIC)

    def test_worker_injects_and_reports_cancellation(self) -> None:
        token = CancellationToken()
        token.cancel()
        cancelled = []

        def operation(cancel_token: CancellationToken) -> None:
            cancel_token.check()

        worker = FunctionWorker(operation, cancel_token=token)
        worker.cancelled.connect(lambda: cancelled.append(True))
        worker.run()
        self.assertEqual(cancelled, [True])

    def test_run_command_can_be_cancelled_quickly(self) -> None:
        token = CancellationToken()
        timer = threading.Timer(0.25, token.cancel)
        timer.start()
        started = time.perf_counter()
        try:
            with self.assertRaises(ConversionCancelled):
                run_command(
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                    cancel_token=token,
                )
        finally:
            timer.cancel()
        self.assertLess(time.perf_counter() - started, 5.0)

    def test_raw_hif_pair_scan(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "A.ARW").write_bytes(b"raw")
            (root / "a.HIF").write_bytes(b"hif")
            (root / "missing.ARW").write_bytes(b"raw")
            sub = root / "sub"
            sub.mkdir()
            (sub / "B.dng").write_bytes(b"raw")
            (sub / "b.HEIF").write_bytes(b"hif")

            flat = scan_raw_hif_pairs(root, recursive=False)
            self.assertEqual(len(flat.pairs), 1)
            self.assertEqual(len(flat.unmatched_raw), 1)

            recursive = scan_raw_hif_pairs(root, recursive=True)
            self.assertEqual(len(recursive.pairs), 2)
            self.assertEqual(recursive.pairs[1].relative_parent, Path("sub"))

    def test_batch_worker_count_is_capped_at_32(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            pairs = [
                RawHifPair(root / f"P{i:02}.ARW", root / f"P{i:02}.HIF", Path("."))
                for i in range(40)
            ]
            captured_workers = []

            def executor_factory(*args, **kwargs):
                captured_workers.append(kwargs.get("max_workers", args[0] if args else None))
                return ThreadPoolExecutor(*args, **kwargs)

            def fake_convert(request, **_kwargs):
                return request.output

            with patch("avif_gain_studio.batch.ThreadPoolExecutor", side_effect=executor_factory), patch(
                "avif_gain_studio.batch.convert", side_effect=fake_convert
            ):
                result = batch_convert_raw_hif(
                    pairs, root / "out", EncodeOptions(output_format="heic"), workers=99
                )
            self.assertEqual(captured_workers, [32])
            self.assertEqual(len(result.succeeded), 40)
            self.assertTrue(all(path.suffix == ".heic" for path in result.succeeded))

    def test_gain_map_analysis_ramp(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "gain16.png"
            ramp = np.tile(np.linspace(0, 65535, 1024, dtype=np.uint16), (32, 1))
            Image.fromarray(ramp).save(path)
            report = analyze_gain_map(path, min_gain_stops=0.0, max_gain_stops=4.0, gain_gamma=1.0)
            values = dict(report.summary)
            p50 = float(values["中位增益 P50"].split()[0])
            self.assertAlmostEqual(p50, 2.0, delta=0.02)
            self.assertIn("4.000", values["中位增益 P50"])


class IntegrationTests(unittest.TestCase):
    def test_small_sdr_gain_encode(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            width, height = 160, 96
            ramp = np.tile(np.linspace(0, 255, width, dtype=np.uint8), (height, 1))
            base = np.stack([ramp, ramp, ramp], axis=-1)
            gain = np.zeros((height, width), dtype=np.uint8)
            gain[:, width // 2 :] = 255
            Image.fromarray(base).save(root / "base.png")
            Image.fromarray(gain).save(root / "gain.png")
            output = root / "output.avif"
            request = ConvertRequest(
                InputMode.SDR_GAIN,
                root / "base.png",
                root / "gain.png",
                output,
                EncodeOptions(speed=10, color_quality=50, gain_quality=50, gain_downscale=2, preserve_metadata=False),
            )
            convert(request)
            self.assertTrue(output.exists())
            info = validate_avif(output)
            self.assertIn("Gain map", info)

    def test_small_pq_png_to_hdr_heic(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "pq.png"
            output = root / "hdr.heic"
            width, height = 64, 32
            row = []
            for x in range(width):
                value = int(65535 * x / (width - 1))
                row.extend((value, value, value))
            with source.open("wb") as stream:
                writer = png.Writer(width, height, greyscale=False, bitdepth=16)
                writer.write(stream, [row for _ in range(height)])
            encode_hdr_heic(source, output, quality=50, depth=10, chroma="420", speed=10)
            report = validate_heic(output)
            self.assertGreaterEqual(report["bit_depth"], 10)
            self.assertEqual(report["nclx"]["color_primaries"], 9)
            self.assertEqual(report["nclx"]["transfer_characteristics"], 16)

    def test_supplied_apple_heic(self) -> None:
        sample = Path(__file__).resolve().parent.parent / "IMG_0150.HEIC"
        if not sample.exists():
            self.skipTest("sample HEIC not present")
        report = analyze_heic(sample)
        self.assertTrue(report.has_gain_map)
        self.assertGreaterEqual(len(report.auxiliary_types), 1)


if __name__ == "__main__":
    unittest.main()
