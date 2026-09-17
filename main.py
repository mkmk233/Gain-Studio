from __future__ import annotations

import multiprocessing
import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication

from avif_gain_studio.tools import app_root
from avif_gain_studio.ui import MainWindow, apply_style


def main() -> int:
    if len(sys.argv) == 4 and sys.argv[1] == "--packaged-heic-smoke":
        from pathlib import Path

        from avif_gain_studio.heic_encoder import encode_hdr_heic, validate_heic

        source = Path(sys.argv[2])
        output = Path(sys.argv[3])
        encode_hdr_heic(source, output, quality=40, depth=10, chroma="420", speed=10)
        validate_heic(output)
        return 0

    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setApplicationName("Gain Studio")
    app.setOrganizationName("OpenAI")
    font_path = app_root() / "assets/fonts/NotoSansSC-VF.ttf"
    if font_path.exists():
        QFontDatabase.addApplicationFont(str(font_path))
    apply_style(app)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
