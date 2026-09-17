from __future__ import annotations

from pathlib import Path
from typing import Callable

from PySide6.QtCore import QDateTime, QSettings, Qt, QThread, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont, QIcon, QPainter, QPen, QPixmap, QTextCursor
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDoubleSpinBox,
    QFileDialog, QFrame, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QListWidget, QMainWindow, QMessageBox, QProgressBar,
    QPushButton, QScrollArea, QSpinBox, QSplitter, QTabWidget, QTextEdit,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from . import __version__
from .batch import BatchResult, ScanResult, batch_convert_raw_hif, scan_raw_hif_pairs
from .cancellation import CancellationToken
from .gain_analyzer import GainMapReport, analyze_gain_map
from .heic_analyzer import HeicReport, analyze_heic, extract_heic_assets
from .models import ConvertRequest, EncodeOptions, InputMode, OutputFormat
from .pipeline import convert
from .tools import ensure_tools
from .worker import FunctionWorker


class DropPathEdit(QLineEdit):
    pathDropped = Signal(str)

    def __init__(self, placeholder: str = "") -> None:
        super().__init__()
        self.setPlaceholderText(placeholder)
        self.setAcceptDrops(True)
        self.setClearButtonEnabled(True)

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dropEvent(self, event) -> None:
        urls = event.mimeData().urls()
        if urls:
            path = urls[0].toLocalFile()
            self.setText(path)
            self.pathDropped.emit(path)
            event.acceptProposedAction()


class PathRow(QWidget):
    changed = Signal(str)

    def __init__(self, label: str, placeholder: str, filters: str, save: bool = False, directory: bool = False) -> None:
        super().__init__()
        self.filters = filters
        self.save = save
        self.directory = directory
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        self.label = QLabel(label)
        self.label.setObjectName("fieldLabel")
        self.label.setFixedWidth(92)
        self.edit = DropPathEdit(placeholder)
        self.button = QPushButton("浏览…")
        self.button.setObjectName("secondaryButton")
        self.button.setFixedWidth(82)
        layout.addWidget(self.label)
        layout.addWidget(self.edit, 1)
        layout.addWidget(self.button)
        self.button.clicked.connect(self.browse)
        self.edit.textChanged.connect(self.changed)

    def browse(self) -> None:
        initial = self.edit.text().strip() or str(Path.home())
        if self.directory:
            path = QFileDialog.getExistingDirectory(self, "选择目录", initial)
        elif self.save:
            path, _ = QFileDialog.getSaveFileName(self, "选择输出文件", initial, self.filters)
        else:
            path, _ = QFileDialog.getOpenFileName(self, "选择输入文件", initial, self.filters)
        if path:
            self.edit.setText(path)

    def text(self) -> str:
        return self.edit.text().strip()

    def setText(self, value: str) -> None:
        self.edit.setText(value)


class MetricCard(QFrame):
    def __init__(self, title: str, value: str) -> None:
        super().__init__()
        self.setObjectName("metricCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 11, 14, 11)
        title_label = QLabel(title)
        title_label.setObjectName("muted")
        self.value = QLabel(value)
        self.value.setObjectName("metricValue")
        layout.addWidget(title_label)
        layout.addWidget(self.value)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.settings = QSettings("OpenAI", "Gain Studio")
        legacy_settings = QSettings("OpenAI", "AVIF Gain Studio")
        if not self.settings.value("legacy_settings_imported", False, type=bool):
            for key in legacy_settings.allKeys():
                if not self.settings.contains(key):
                    self.settings.setValue(key, legacy_settings.value(key))
            self.settings.setValue("legacy_settings_imported", True)
        self._thread: QThread | None = None
        self._worker: FunctionWorker | None = None
        self._cancel_token: CancellationToken | None = None
        self._last_report: HeicReport | None = None
        self._last_gain_report: GainMapReport | None = None
        self._batch_scan: ScanResult | None = None
        self.setWindowTitle(f"Gain Studio {__version__}")
        self.setMinimumSize(1040, 760)
        self.resize(1220, 860)
        self.setWindowIcon(self._make_icon())
        self._build_ui()
        self._restore_settings()
        self._check_tools()

    @staticmethod
    def _make_icon() -> QIcon:
        pixmap = QPixmap(64, 64)
        pixmap.fill(Qt.GlobalColor.transparent)
        p = QPainter(pixmap)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(QColor("#6d5dfc"))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(4, 4, 56, 56, 15, 15)
        p.setPen(QPen(QColor("#ffffff"), 5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawArc(16, 17, 32, 28, 25 * 16, 285 * 16)
        p.drawLine(21, 43, 44, 20)
        p.end()
        return QIcon(pixmap)

    def _build_ui(self) -> None:
        root = QWidget()
        outer = QVBoxLayout(root)
        outer.setContentsMargins(22, 18, 22, 18)
        outer.setSpacing(14)
        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel("Gain Studio")
        title.setObjectName("appTitle")
        subtitle = QLabel("RAW / SDR / HDR · AVIF 增益图与 HEIC HDR 输出 · Apple HEIC 分析")
        subtitle.setObjectName("subtitle")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box)
        header.addStretch()
        self.tool_status = QLabel("正在检查组件…")
        self.tool_status.setObjectName("statusPill")
        header.addWidget(self.tool_status)
        self.stop_button = QPushButton("停止所有转换")
        self.stop_button.setObjectName("stopButton")
        self.stop_button.setEnabled(False)
        self.stop_button.setToolTip("取消等待队列，并强制结束正在运行的编码器子进程。")
        header.addWidget(self.stop_button)
        outer.addLayout(header)
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.addTab(self._build_convert_tab(), "照片转换")
        self.tabs.addTab(self._build_batch_tab(), "RAW+HIF 批量")
        self.tabs.addTab(self._build_gain_analyze_tab(), "增益图分析")
        self.tabs.addTab(self._build_analyze_tab(), "Apple HEIC 分析")
        self.tabs.addTab(self._build_about_tab(), "说明")
        outer.addWidget(self.tabs, 1)
        self.setCentralWidget(root)

    def _build_convert_tab(self) -> QWidget:
        page = QWidget()
        main = QHBoxLayout(page)
        main.setContentsMargins(0, 16, 0, 0)
        main.setSpacing(16)
        left_scroll = QScrollArea()
        left_scroll.setWidgetResizable(True)
        left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        left_scroll.setFrameShape(QFrame.Shape.NoFrame)
        left = QWidget()
        form = QVBoxLayout(left)
        form.setContentsMargins(0, 0, 8, 0)
        form.setSpacing(14)

        source_box = QGroupBox("1  输入与输出")
        source_layout = QVBoxLayout(source_box)
        mode_row = QHBoxLayout()
        mode_label = QLabel("工作模式")
        mode_label.setObjectName("fieldLabel")
        mode_label.setFixedWidth(92)
        self.mode = QComboBox()
        self.mode.addItem("RAW 自动（内嵌 JPEG + RAW 高光）", InputMode.ARW_AUTO)
        self.mode.addItem("RAW + 同名 HIF/HEIF（HIF 作 SDR 底图）", InputMode.RAW_HIF)
        self.mode.addItem("RAW 单独导出 16-bit 增益图", InputMode.RAW_GAIN_EXPORT)
        self.mode.addItem("指定 SDR 底图 + 指定增益图", InputMode.SDR_GAIN)
        self.mode.addItem("指定 SDR 底图 + HDR 参考图", InputMode.SDR_HDR)
        self.mode.addItem("Ultra HDR JPEG 转换", InputMode.JPEG_GAIN)
        mode_row.addWidget(mode_label)
        mode_row.addWidget(self.mode, 1)
        source_layout.addLayout(mode_row)
        format_row = QHBoxLayout()
        format_label = QLabel("输出格式")
        format_label.setObjectName("fieldLabel")
        format_label.setFixedWidth(92)
        self.output_format = QComboBox()
        self.output_format.addItem("AVIF（SDR + HDR 增益图）", OutputFormat.AVIF)
        self.output_format.addItem("HEIC（10/12-bit Rec.2020 PQ HDR）", OutputFormat.HEIC)
        self.output_format.setToolTip("AVIF 含兼容 SDR 底图和 HDR 增益图；HEIC 为单层 PQ HDR，兼容性取决于设备的 HDR HEIC 支持。")
        format_row.addWidget(format_label)
        format_row.addWidget(self.output_format, 1)
        source_layout.addLayout(format_row)
        self.primary = PathRow("RAW 文件", "拖入 RAW 或点击浏览", "RAW/图像 (*.arw *.dng *.nef *.cr2 *.cr3 *.raf *.rw2 *.orf *.pef *.jpg *.jpeg *.png *.avif);;所有文件 (*)")
        self.secondary = PathRow("辅助图", "选择增益图、HIF 或 SDR 底图", "图像 (*.hif *.heif *.heic *.png *.jpg *.jpeg *.tif *.tiff *.avif);;所有文件 (*)")
        self.output = PathRow("输出 AVIF", "输出文件路径", "AVIF 图像 (*.avif)", save=True)
        source_layout.addWidget(self.primary)
        source_layout.addWidget(self.secondary)
        source_layout.addWidget(self.output)
        self.mode_hint = QLabel()
        self.mode_hint.setWordWrap(True)
        self.mode_hint.setObjectName("callout")
        source_layout.addWidget(self.mode_hint)
        form.addWidget(source_box)

        quality_box = QGroupBox("2  编码质量")
        quality_grid = QGridLayout(quality_box)
        quality_grid.setHorizontalSpacing(18)
        quality_grid.setVerticalSpacing(11)
        self.color_quality = self._spin(0, 100, 85, "%")
        self.gain_quality = self._spin(0, 100, 80, "%")
        self.depth = self._combo(["8", "10", "12"], "10")
        self.gain_depth = self._combo(["8", "10", "12"], "8")
        self.yuv = self._combo(["420", "422", "444"], "420")
        self.gain_yuv = self._combo(["400", "420", "422", "444"], "400")
        self.downscale = self._combo(["1", "2", "4", "8"], "2")
        self.speed = self._spin(0, 10, 6)
        pairs = [
            ("SDR/主图质量", self.color_quality, 0, 0), ("HDR/增益图质量", self.gain_quality, 0, 2),
            ("主图位深", self.depth, 1, 0), ("增益图位深", self.gain_depth, 1, 2),
            ("主图色度采样", self.yuv, 2, 0), ("增益图采样", self.gain_yuv, 2, 2),
            ("增益图缩小倍数", self.downscale, 3, 0), ("编码速度 0慢–10快", self.speed, 3, 2),
        ]
        for label, widget, row, col in pairs:
            quality_grid.addWidget(QLabel(label), row, col)
            quality_grid.addWidget(widget, row, col + 1)
        quality_grid.setColumnStretch(1, 1)
        quality_grid.setColumnStretch(3, 1)
        form.addWidget(quality_box)

        hdr_box = QGroupBox("3  HDR 与元数据")
        hdr_grid = QGridLayout(hdr_box)
        self.headroom = self._double_spin(0.5, 8.0, 4.0, 0.25, " stops")
        self.exposure = self._double_spin(-3.0, 3.0, 0.0, 0.1, " EV")
        self.gain_gamma = self._double_spin(0.1, 4.0, 1.0, 0.1, "")
        self.min_gain = self._double_spin(-4.0, 4.0, 0.0, 0.25, " stops")
        hdr_grid.addWidget(QLabel("最大 HDR 余量"), 0, 0)
        hdr_grid.addWidget(self.headroom, 0, 1)
        hdr_grid.addWidget(QLabel("RAW 曝光微调"), 0, 2)
        hdr_grid.addWidget(self.exposure, 0, 3)
        hdr_grid.addWidget(QLabel("外部增益图 Gamma"), 1, 0)
        hdr_grid.addWidget(self.gain_gamma, 1, 1)
        hdr_grid.addWidget(QLabel("最小增益"), 1, 2)
        hdr_grid.addWidget(self.min_gain, 1, 3)
        self.preserve = QCheckBox("保留 EXIF / XMP / IPTC / ICC 等元数据")
        self.preserve.setChecked(True)
        self.keep_temp = QCheckBox("保留 SDR、HDR 参考图等中间文件（便于校验）")
        hdr_grid.addWidget(self.preserve, 2, 0, 1, 4)
        hdr_grid.addWidget(self.keep_temp, 3, 0, 1, 4)
        form.addWidget(hdr_box)

        action_row = QHBoxLayout()
        self.convert_button = QPushButton("开始转换为 AVIF")
        self.convert_button.setObjectName("primaryButton")
        self.convert_button.setMinimumHeight(44)
        self.open_output_button = QPushButton("打开输出目录")
        self.open_output_button.setObjectName("secondaryButton")
        self.open_output_button.setMinimumHeight(44)
        action_row.addWidget(self.convert_button, 1)
        action_row.addWidget(self.open_output_button)
        form.addLayout(action_row)
        form.addStretch()
        left_scroll.setWidget(left)

        right = QFrame()
        right.setObjectName("sidePanel")
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(16, 16, 16, 16)
        status_title = QLabel("任务状态")
        status_title.setObjectName("sectionTitle")
        right_layout.addWidget(status_title)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.progress_label = QLabel("等待任务")
        self.progress_label.setObjectName("muted")
        right_layout.addWidget(self.progress)
        right_layout.addWidget(self.progress_label)
        log_label = QLabel("运行日志")
        log_label.setObjectName("sectionTitle")
        right_layout.addSpacing(8)
        right_layout.addWidget(log_label)
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setAcceptRichText(False)
        self.log_view.document().setMaximumBlockCount(20000)
        self.log_view.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.log_view.setPlaceholderText("编码命令、RAW 分析和验证结果会显示在这里。")
        right_layout.addWidget(self.log_view, 1)
        self.clear_log = QPushButton("清空日志")
        self.clear_log.setObjectName("ghostButton")
        right_layout.addWidget(self.clear_log)
        main.addWidget(left_scroll, 3)
        main.addWidget(right, 2)

        self.mode.currentIndexChanged.connect(self._update_mode)
        self.output_format.currentIndexChanged.connect(self._update_mode)
        self.primary.changed.connect(self._primary_changed)
        self.convert_button.clicked.connect(self._start_convert)
        self.open_output_button.clicked.connect(self._open_output)
        self.clear_log.clicked.connect(self.log_view.clear)
        self.stop_button.clicked.connect(self._stop_all_tasks)
        self._update_mode()
        return page

    def _build_batch_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 16, 0, 0)
        layout.setSpacing(14)

        source_box = QGroupBox("扫描同名 RAW + HIF/HEIF")
        source_layout = QVBoxLayout(source_box)
        self.batch_input = PathRow("输入目录", "包含 RAW+HIF 的目录", "", directory=True)
        self.batch_output = PathRow("输出目录", "批量输出目录", "", directory=True)
        source_layout.addWidget(self.batch_input)
        source_layout.addWidget(self.batch_output)
        format_row = QHBoxLayout()
        format_row.addWidget(QLabel("输出格式"))
        self.batch_output_format = QComboBox()
        self.batch_output_format.addItem("AVIF（SDR + HDR 增益图）", OutputFormat.AVIF)
        self.batch_output_format.addItem("HEIC（10/12-bit Rec.2020 PQ HDR）", OutputFormat.HEIC)
        format_row.addWidget(self.batch_output_format, 1)
        source_layout.addLayout(format_row)
        option_row = QHBoxLayout()
        self.batch_recursive = QCheckBox("扫描子目录并保留目录结构")
        self.batch_recursive.setChecked(True)
        self.batch_workers = self._spin(1, 32, 2, " 个并行任务")
        self.batch_workers.setToolTip("RAW 解码内存占用较高。32 并行可能占用 10–16 GB 以上内存，仅建议高核心数、大内存设备。")
        option_row.addWidget(self.batch_recursive)
        option_row.addStretch()
        option_row.addWidget(QLabel("并行数"))
        option_row.addWidget(self.batch_workers)
        source_layout.addLayout(option_row)
        button_row = QHBoxLayout()
        self.batch_scan_button = QPushButton("扫描并匹配")
        self.batch_scan_button.setObjectName("secondaryButton")
        self.batch_start_button = QPushButton("开始多线程批量合成")
        self.batch_start_button.setObjectName("primaryButton")
        self.batch_start_button.setEnabled(False)
        button_row.addWidget(self.batch_scan_button)
        button_row.addWidget(self.batch_start_button)
        button_row.addStretch()
        source_layout.addLayout(button_row)
        layout.addWidget(source_box)

        self.batch_summary = QLabel("尚未扫描。只匹配同一目录内、主文件名完全相同（不区分大小写）的 RAW 与 HIF/HEIF。")
        self.batch_summary.setObjectName("callout")
        self.batch_summary.setWordWrap(True)
        layout.addWidget(self.batch_summary)
        self.batch_tree = QTreeWidget()
        self.batch_tree.setHeaderLabels(["状态", "RAW", "HIF/HEIF", "相对目录"])
        self.batch_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.batch_tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.batch_tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.batch_tree.header().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.batch_tree.setAlternatingRowColors(True)
        layout.addWidget(self.batch_tree, 1)
        note = QLabel("批量任务使用多个独立 RAW 解码/编码线程。单个 RAW 可能占用 300–500 MB 内存；32 并行可能超过 10–16 GB。编码参数沿用“照片转换”页，输出格式在本页单独选择。")
        note.setObjectName("muted")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.batch_scan_button.clicked.connect(self._start_batch_scan)
        self.batch_start_button.clicked.connect(self._start_batch_convert)
        return page

    def _build_gain_analyze_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 16, 0, 0)
        layout.setSpacing(14)
        input_box = QGroupBox("增益图 / AVIF")
        input_layout = QVBoxLayout(input_box)
        self.gain_analyze_path = PathRow(
            "输入文件", "独立 PNG/JPEG 增益图，或带增益图的 AVIF",
            "增益图/AVIF (*.png *.jpg *.jpeg *.tif *.tiff *.avif);;所有文件 (*)"
        )
        input_layout.addWidget(self.gain_analyze_path)
        map_row = QHBoxLayout()
        self.analysis_min_gain = self._double_spin(-8.0, 8.0, 0.0, 0.25, " stops")
        self.analysis_max_gain = self._double_spin(-8.0, 16.0, 4.0, 0.25, " stops")
        self.analysis_gamma = self._double_spin(0.1, 8.0, 1.0, 0.1, "")
        map_row.addWidget(QLabel("独立图黑场映射"))
        map_row.addWidget(self.analysis_min_gain)
        map_row.addWidget(QLabel("白场映射"))
        map_row.addWidget(self.analysis_max_gain)
        map_row.addWidget(QLabel("Gamma"))
        map_row.addWidget(self.analysis_gamma)
        self.gain_analyze_button = QPushButton("分析亮度倍数")
        self.gain_analyze_button.setObjectName("primaryButton")
        map_row.addWidget(self.gain_analyze_button)
        input_layout.addLayout(map_row)
        hint = QLabel("分析 AVIF 时自动读取容器内 Gain Map Min/Max/Gamma；分析独立灰度图时使用上面的映射。结果中的 1 stop=2× SDR，2 stops=4×，3 stops=8×。")
        hint.setObjectName("callout")
        hint.setWordWrap(True)
        input_layout.addWidget(hint)
        layout.addWidget(input_box)

        cards = QHBoxLayout()
        self.gain_card_median = MetricCard("中位亮度倍数", "—")
        self.gain_card_p95 = MetricCard("P95 亮度倍数", "—")
        self.gain_card_max = MetricCard("最大亮度倍数", "—")
        cards.addWidget(self.gain_card_median)
        cards.addWidget(self.gain_card_p95)
        cards.addWidget(self.gain_card_max)
        layout.addLayout(cards)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        summary_box = QGroupBox("增益统计")
        summary_layout = QVBoxLayout(summary_box)
        self.gain_summary_tree = QTreeWidget()
        self.gain_summary_tree.setHeaderLabels(["项目", "值"])
        self.gain_summary_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.gain_summary_tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.gain_summary_tree.setAlternatingRowColors(True)
        summary_layout.addWidget(self.gain_summary_tree)
        detail_box = QGroupBox("容器元数据与分布")
        detail_layout = QVBoxLayout(detail_box)
        self.gain_detail = QTextEdit()
        self.gain_detail.setReadOnly(True)
        self.gain_detail.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        detail_layout.addWidget(self.gain_detail)
        splitter.addWidget(summary_box)
        splitter.addWidget(detail_box)
        splitter.setSizes([560, 480])
        layout.addWidget(splitter, 1)
        self.gain_analyze_button.clicked.connect(self._start_gain_analysis)
        return page

    def _build_analyze_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 16, 0, 0)
        layout.setSpacing(14)
        input_box = QGroupBox("Apple HEIC 文件")
        input_layout = QVBoxLayout(input_box)
        self.heic_path = PathRow("HEIC 文件", "拖入 iPhone 拍摄的 .HEIC", "HEIC 图像 (*.heic *.heif);;所有文件 (*)")
        input_layout.addWidget(self.heic_path)
        buttons = QHBoxLayout()
        self.analyze_button = QPushButton("分析照片信息")
        self.analyze_button.setObjectName("primaryButton")
        self.extract_button = QPushButton("提取主图、增益图与辅助图")
        self.extract_button.setObjectName("secondaryButton")
        self.extract_button.setEnabled(False)
        buttons.addWidget(self.analyze_button)
        buttons.addWidget(self.extract_button)
        buttons.addStretch()
        input_layout.addLayout(buttons)
        layout.addWidget(input_box)
        cards = QHBoxLayout()
        self.card_gain = MetricCard("HDR 增益图", "—")
        self.card_aux = MetricCard("辅助图类型", "—")
        self.card_depth = MetricCard("像素深度", "—")
        cards.addWidget(self.card_gain)
        cards.addWidget(self.card_aux)
        cards.addWidget(self.card_depth)
        layout.addLayout(cards)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        summary_box = QGroupBox("摘要")
        summary_layout = QVBoxLayout(summary_box)
        self.summary_tree = QTreeWidget()
        self.summary_tree.setHeaderLabels(["项目", "值"])
        self.summary_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.summary_tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.summary_tree.setAlternatingRowColors(True)
        summary_layout.addWidget(self.summary_tree)
        detail_tabs = QTabWidget()
        aux_page = QWidget()
        aux_layout = QVBoxLayout(aux_page)
        self.aux_list = QListWidget()
        self.aux_list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        aux_layout.addWidget(self.aux_list)
        detail_tabs.addTab(aux_page, "辅助图像")
        self.raw_metadata = QTextEdit()
        self.raw_metadata.setReadOnly(True)
        self.raw_metadata.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        detail_tabs.addTab(self.raw_metadata, "完整元数据")
        splitter.addWidget(summary_box)
        splitter.addWidget(detail_tabs)
        splitter.setSizes([480, 560])
        layout.addWidget(splitter, 1)
        self.analyze_button.clicked.connect(self._start_analyze)
        self.extract_button.clicked.connect(self._start_extract)
        return page

    def _build_about_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(28, 28, 28, 28)
        text = QLabel(
            "<h2>工作方式</h2>"
            "<p><b>ARW 自动模式</b>会提取相机写入 RAW 的全尺寸 JPEG，并从 Bayer RAW 的线性高光数据生成 "
            "Rec.2020/PQ HDR 参考图，再由 libavif 计算并封装标准 AVIF 增益图。</p>"
            "<p>给定的 Sony 样片中包含全尺寸 <code>JpgFromRaw</code>，但<b>没有独立的增益图项目</b>。因此程序不是简单“抠出”"
            "一张现成增益图，而是以 RAW 和相机 JPEG 的差异重建增益图。</p>"
            "<p><b>RAW + HIF 模式</b>用同目录、同主文件名的 HIF/HEIF 作为相机渲染的 SDR 底图，"
            "再用 RAW 线性高光重建 HDR。批量页可递归扫描并以 1–32 个工作线程并发处理，同时保留相对目录结构。</p>"
            "<p><b>RAW 增益图导出</b>生成 16-bit 灰度 PNG：黑色代表 0 stops/1× SDR，白色代表设置的最大 HDR 余量。"
            "可使用 RAW 内嵌 JPEG、指定 HIF/HEIF 或普通 SDR 图片作为对比底图。</p>"
            "<p><b>指定 SDR + 增益图</b>模式把灰度增益图的 0–1 映射到“最小增益–最大 HDR 余量”，先生成 HDR 参考图，"
            "再重新计算标准增益图。增益分析页会显示 stops、相对 SDR 亮度倍数、百分位和高增益覆盖率；1 stop=2×，2 stops=4×。</p>"
            "<p><b>输出格式：</b>AVIF 使用 SDR 底图 + HDR 增益图，旧软件仍可显示 SDR；HEIC 输出为 10/12-bit "
            "Rec.2020/PQ 单层 HDR，不包含 SDR gain-map 回退，显示效果依赖手机或系统的 HDR HEIC 与色调映射支持。</p>"
            "<p><b>Apple HEIC 分析：</b>可识别 <code>hdrgainmap</code>、sky matte、portrait matte、style delta map 等项目。</p>"
            "<p><b>性能：</b>单个 RAW 解码任务会短时占用约 300–500 MB 内存。建议 16 GB 内存使用 2 个并行任务，"
            "32 GB 以上使用 3–4 个；更高并发会明显提高 CPU 与内存峰值。增益图缩小 2–4 倍、400 灰度采样可降低体积和耗时。</p>"
        )
        text.setWordWrap(True)
        text.setTextFormat(Qt.TextFormat.RichText)
        text.setAlignment(Qt.AlignmentFlag.AlignTop)
        layout.addWidget(text)
        layout.addStretch()
        return page

    @staticmethod
    def _spin(minimum: int, maximum: int, value: int, suffix: str = "") -> QSpinBox:
        widget = QSpinBox()
        widget.setRange(minimum, maximum)
        widget.setValue(value)
        widget.setSuffix(suffix)
        return widget

    @staticmethod
    def _double_spin(minimum: float, maximum: float, value: float, step: float, suffix: str) -> QDoubleSpinBox:
        widget = QDoubleSpinBox()
        widget.setRange(minimum, maximum)
        widget.setValue(value)
        widget.setSingleStep(step)
        widget.setDecimals(2)
        widget.setSuffix(suffix)
        return widget

    @staticmethod
    def _combo(values: list[str], current: str) -> QComboBox:
        widget = QComboBox()
        widget.addItems(values)
        widget.setCurrentText(current)
        return widget

    def _check_tools(self) -> None:
        try:
            versions = ensure_tools()
            self.tool_status.setText("组件就绪 · libavif 1.4.1")
            self.tool_status.setToolTip("\n".join(f"{k}: {v}" for k, v in versions.items()))
        except Exception as exc:
            self.tool_status.setText("组件缺失")
            self.tool_status.setObjectName("statusError")
            self.tool_status.style().unpolish(self.tool_status)
            self.tool_status.style().polish(self.tool_status)
            QMessageBox.critical(self, "组件检查失败", str(exc))

    def _mode_value(self) -> InputMode:
        return InputMode(self.mode.currentData())

    def _output_format_value(self) -> OutputFormat:
        return OutputFormat(self.output_format.currentData())

    def _batch_output_format_value(self) -> OutputFormat:
        return OutputFormat(self.batch_output_format.currentData())

    @staticmethod
    def _select_combo_data(combo: QComboBox, value: object) -> None:
        wanted = getattr(value, "value", value)
        for index in range(combo.count()):
            current = combo.itemData(index)
            if getattr(current, "value", current) == wanted:
                combo.setCurrentIndex(index)
                return

    def _update_mode(self) -> None:
        mode = self._mode_value()
        output_format = self._output_format_value()
        configs = {
            InputMode.ARW_AUTO: ("RAW 文件", False, "", "从 RAW 提取相机 JPEG，并以 RAW 线性高光重建 HDR。"),
            InputMode.RAW_HIF: ("RAW 文件", True, "同名 HIF", "使用同名 HIF/HEIF 作为相机渲染的 SDR 底图，以 RAW 线性高光重建 HDR。选择 RAW 后会自动查找同目录同名 HIF。"),
            InputMode.RAW_GAIN_EXPORT: ("RAW 文件", True, "可选 SDR/HIF", "单独导出 16-bit 灰度增益图。辅助图留空时使用 RAW 内嵌 JPEG；也可指定 HIF/HEIF/JPEG/PNG 底图。黑=1×，白=设定的最大 HDR 余量。"),
            InputMode.SDR_GAIN: ("SDR 底图", True, "增益图", "增益图黑色=最小增益，白色=最大 HDR 余量；支持不同分辨率，自动双三次对齐。"),
            InputMode.SDR_HDR: ("SDR 底图", True, "HDR 参考图", "HDR 参考图应带正确 ICC/CICP。HEIC 输出目前要求 HDR 参考图为 16-bit Rec.2020/PQ PNG。"),
            InputMode.JPEG_GAIN: ("Ultra HDR JPEG", False, "", "转换已有 JPEG gain map（例如 Android Ultra HDR）。"),
        }
        primary, show_secondary, secondary, hint = configs[mode]
        self.primary.label.setText(primary)
        self.secondary.setVisible(show_secondary)
        if show_secondary:
            self.secondary.label.setText(secondary)
        raw_mode = mode in (InputMode.ARW_AUTO, InputMode.RAW_HIF, InputMode.RAW_GAIN_EXPORT)
        self.exposure.setEnabled(raw_mode)
        self.gain_gamma.setEnabled(mode == InputMode.SDR_GAIN)
        self.min_gain.setEnabled(mode == InputMode.SDR_GAIN)
        export_only = mode == InputMode.RAW_GAIN_EXPORT
        self.output_format.setEnabled(not export_only)
        gain_controls_enabled = not export_only and output_format == OutputFormat.AVIF
        for widget in (self.gain_quality, self.gain_depth, self.gain_yuv, self.downscale):
            widget.setEnabled(gain_controls_enabled)
        if export_only:
            desired = ".png"
            self.output.label.setText("输出增益图")
            self.output.filters = "PNG 增益图 (*.png)"
            self.convert_button.setText("导出 RAW 增益图")
        else:
            desired = ".heic" if output_format == OutputFormat.HEIC else ".avif"
            name = output_format.value.upper()
            self.output.label.setText(f"输出 {name}")
            self.output.filters = "HEIC 图像 (*.heic)" if output_format == OutputFormat.HEIC else "AVIF 图像 (*.avif)"
            self.convert_button.setText(f"开始转换为 {name}")
            if output_format == OutputFormat.HEIC:
                hint += " HEIC 输出为 10/12-bit Rec.2020/PQ 单层 HDR，不包含 SDR 增益图回退；显示效果依赖设备 HDR HEIC 与色调映射支持。"
            else:
                hint += " AVIF 输出包含 SDR 底图和 HDR 增益图，旧设备可回退显示 SDR。"
        self.mode_hint.setText(hint)
        current_output = self.output.text()
        if current_output and Path(current_output).suffix.lower() != desired:
            self.output.setText(str(Path(current_output).with_suffix(desired)))
        if mode == InputMode.RAW_HIF and self.primary.text():
            self._find_matching_hif(Path(self.primary.text()))

    def _find_matching_hif(self, raw_path: Path) -> None:
        if not raw_path.is_file():
            return
        for candidate in raw_path.parent.iterdir():
            if candidate.is_file() and candidate.stem.casefold() == raw_path.stem.casefold() and candidate.suffix.lower() in {".hif", ".heif", ".heic"}:
                self.secondary.setText(str(candidate))
                self._append_log(f"已自动匹配同名 HIF/HEIF：{candidate}")
                return

    def _primary_changed(self, value: str) -> None:
        path = Path(value)
        if value and not self.output.text():
            if self._mode_value() == InputMode.RAW_GAIN_EXPORT:
                suffix = ".gain.png"
            else:
                suffix = ".gain.heic" if self._output_format_value() == OutputFormat.HEIC else ".gain.avif"
            self.output.setText(str(path.with_suffix(suffix)))
        if value and self._mode_value() == InputMode.RAW_HIF:
            self._find_matching_hif(path)

    def _encode_options(
        self,
        jobs: int | None = None,
        output_format: OutputFormat | None = None,
    ) -> EncodeOptions:
        return EncodeOptions(
            color_quality=self.color_quality.value(), gain_quality=self.gain_quality.value(),
            speed=self.speed.value(), depth=int(self.depth.currentText()), yuv=self.yuv.currentText(),
            gain_depth=int(self.gain_depth.currentText()), gain_yuv=self.gain_yuv.currentText(),
            gain_downscale=int(self.downscale.currentText()), max_headroom=self.headroom.value(),
            exposure_comp=self.exposure.value(), gain_gamma=self.gain_gamma.value(),
            min_gain_stops=self.min_gain.value(), preserve_metadata=self.preserve.isChecked(),
            keep_intermediates=self.keep_temp.isChecked(),
            jobs=jobs if jobs is not None else 1,
            output_format=output_format or self._output_format_value(),
        )

    def _request(self) -> ConvertRequest:
        primary = Path(self.primary.text())
        if not primary.is_file():
            raise ValueError("请选择有效的输入文件。")
        mode = self._mode_value()
        secondary = None
        secondary_text = self.secondary.text()
        if mode in (InputMode.SDR_GAIN, InputMode.SDR_HDR, InputMode.RAW_HIF):
            secondary = Path(secondary_text)
            if not secondary.is_file():
                raise ValueError("请选择有效的辅助图/HIF 文件。")
        elif mode == InputMode.RAW_GAIN_EXPORT and secondary_text:
            secondary = Path(secondary_text)
            if not secondary.is_file():
                raise ValueError("指定的 SDR/HIF 底图不存在。")
        export_only = mode == InputMode.RAW_GAIN_EXPORT
        output_format = self._output_format_value()
        desired_suffix = ".png" if export_only else (".heic" if output_format == OutputFormat.HEIC else ".avif")
        output_text = self.output.text()
        if not output_text:
            default_suffix = ".gain.png" if export_only else f".gain{desired_suffix}"
            output_text = str(primary.with_suffix(default_suffix))
            self.output.setText(output_text)
        output = Path(output_text)
        if output.suffix.lower() != desired_suffix:
            output = output.with_suffix(desired_suffix)
            self.output.setText(str(output))
        return ConvertRequest(mode, primary, secondary, output, self._encode_options(output_format=output_format))

    def _start_convert(self) -> None:
        try:
            request = self._request()
        except Exception as exc:
            QMessageBox.warning(self, "输入不完整", str(exc))
            return
        if request.output.exists():
            answer = QMessageBox.question(self, "覆盖文件？", f"输出已存在：\n{request.output}\n\n是否覆盖？")
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.log_view.clear()
        self._append_log(f"模式：{self.mode.currentText()}")
        self._append_log(f"输出：{request.output}")
        if request.mode != InputMode.RAW_GAIN_EXPORT:
            self._append_log(f"输出格式：{request.options.output_format.value.upper()}")
        self.progress.setValue(1)
        self.progress_label.setText("启动任务…")
        self._set_busy(True)
        self._run_worker(convert, request, done=self._convert_done, cancellable=True)

    def _start_batch_scan(self) -> None:
        directory = Path(self.batch_input.text())
        if not directory.is_dir():
            QMessageBox.warning(self, "请选择目录", "请选择包含 RAW+HIF/HEIF 的有效目录。")
            return
        if not self.batch_output.text():
            self.batch_output.setText(str(directory / "Gain_Studio_Output"))
        self._set_busy(True)
        self.batch_tree.clear()
        self.batch_summary.setText("正在扫描并按同目录、同主文件名匹配…")
        self._append_log(f"开始扫描 RAW+HIF：{directory}；递归={'是' if self.batch_recursive.isChecked() else '否'}")
        self._run_worker(scan_raw_hif_pairs, directory, self.batch_recursive.isChecked(), done=self._batch_scan_done)

    def _batch_scan_done(self, result: object) -> None:
        self._set_busy(False)
        if not isinstance(result, ScanResult):
            return
        self._batch_scan = result
        self.batch_tree.clear()
        for pair in result.pairs:
            self.batch_tree.addTopLevelItem(QTreeWidgetItem(["待处理", pair.raw.name, pair.hif.name, str(pair.relative_parent)]))
        for raw in result.unmatched_raw:
            self.batch_tree.addTopLevelItem(QTreeWidgetItem(["缺少 HIF", raw.name, "—", str(raw.parent)]))
        for hif in result.unmatched_hif:
            self.batch_tree.addTopLevelItem(QTreeWidgetItem(["缺少 RAW", "—", hif.name, str(hif.parent)]))
        self.batch_summary.setText(
            f"匹配成功 {len(result.pairs)} 对；未匹配 RAW {len(result.unmatched_raw)}；"
            f"未匹配 HIF/HEIF {len(result.unmatched_hif)}。"
        )
        self.batch_start_button.setEnabled(bool(result.pairs))
        self._append_log(self.batch_summary.text())

    def _start_batch_convert(self) -> None:
        if not self._batch_scan or not self._batch_scan.pairs:
            QMessageBox.warning(self, "队列为空", "请先扫描并匹配 RAW+HIF。")
            return
        output_dir = Path(self.batch_output.text())
        if not self.batch_output.text():
            QMessageBox.warning(self, "请选择目录", "请选择批量输出目录。")
            return
        workers = self.batch_workers.value()
        if workers > 4:
            answer = QMessageBox.question(
                self, "高内存占用确认",
                f"将同时处理 {workers} 个 RAW。按每个任务约 300–500 MB 估算，"
                f"峰值可能达到 {workers * 300 / 1024:.1f}–{workers * 500 / 1024:.1f} GB 或更高，并使系统响应变慢。是否继续？"
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        for index in range(self.batch_tree.topLevelItemCount()):
            item = self.batch_tree.topLevelItem(index)
            if item.text(0) == "待处理":
                item.setText(0, "处理中")
        self.batch_scan_button.setEnabled(False)
        self.batch_start_button.setEnabled(False)
        self._set_busy(True)
        self.progress.setValue(1)
        self.progress_label.setText("启动多线程批量任务…")
        output_format = self._batch_output_format_value()
        self._append_log(
            f"批量输出目录：{output_dir}；输出格式：{output_format.value.upper()}；并行任务数：{workers}"
        )
        self._run_worker(
            batch_convert_raw_hif,
            self._batch_scan.pairs,
            output_dir,
            self._encode_options(jobs=workers, output_format=output_format),
            workers,
            done=self._batch_convert_done,
            cancellable=True,
        )

    def _batch_convert_done(self, result: object) -> None:
        self._set_busy(False)
        self.batch_scan_button.setEnabled(True)
        self.batch_start_button.setEnabled(bool(self._batch_scan and self._batch_scan.pairs))
        if not isinstance(result, BatchResult):
            return
        failed_names = {path.stem.casefold() for path, _error in result.failed}
        succeeded_names = {path.name.split(".gain.", 1)[0].casefold() for path in result.succeeded}
        for index in range(self.batch_tree.topLevelItemCount()):
            item = self.batch_tree.topLevelItem(index)
            if item.text(0) == "处理中":
                stem = Path(item.text(1)).stem.casefold()
                if stem in failed_names:
                    item.setText(0, "失败")
                elif stem in succeeded_names:
                    item.setText(0, "成功")
                else:
                    item.setText(0, "已停止")
        details = "\n".join(f"{path.name}: {error}" for path, error in result.failed[:10])
        if result.cancelled:
            self.progress.setValue(0)
            self.progress_label.setText(
                f"已停止：成功 {len(result.succeeded)}，失败 {len(result.failed)}，其余已取消"
            )
            message = (
                f"批量转换已停止。\n已成功：{len(result.succeeded)}\n失败：{len(result.failed)}\n"
                f"耗时：{result.elapsed_seconds:.1f} 秒"
            )
            title = "批量转换已停止"
        else:
            self.progress.setValue(100)
            self.progress_label.setText(f"批量完成：成功 {len(result.succeeded)}，失败 {len(result.failed)}")
            message = (
                f"批量处理完成。\n成功：{len(result.succeeded)}\n失败：{len(result.failed)}\n"
                f"耗时：{result.elapsed_seconds:.1f} 秒"
            )
            title = "批量处理完成"
        if details:
            message += "\n\n失败详情：\n" + details
        QMessageBox.information(self, title, message)
        if result.succeeded and not result.cancelled:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(result.succeeded[0].parent)))

    def _start_gain_analysis(self) -> None:
        path = Path(self.gain_analyze_path.text())
        if not path.is_file():
            QMessageBox.warning(self, "请选择文件", "请选择独立增益图或带增益图的 AVIF。")
            return
        if self.analysis_max_gain.value() <= self.analysis_min_gain.value() and path.suffix.lower() != ".avif":
            QMessageBox.warning(self, "映射无效", "独立增益图的白场映射必须大于黑场映射。")
            return
        self._set_busy(True)
        self.gain_summary_tree.clear()
        self.gain_detail.setPlainText("正在分析像素分布与亮度倍数…")
        self._append_log(f"开始分析增益图：{path}")
        self._run_worker(
            analyze_gain_map,
            path,
            self.analysis_min_gain.value(),
            self.analysis_max_gain.value(),
            self.analysis_gamma.value(),
            done=self._gain_analysis_done,
        )

    def _gain_analysis_done(self, result: object) -> None:
        self._set_busy(False)
        if not isinstance(result, GainMapReport):
            return
        self._last_gain_report = result
        self.gain_summary_tree.clear()
        values = {}
        for key, value in result.summary:
            values[key] = value
            self.gain_summary_tree.addTopLevelItem(QTreeWidgetItem([key, value]))
        self.gain_detail.setPlainText(result.raw_text)
        def multiplier(key: str) -> str:
            value = values.get(key, "—")
            if "；" in value:
                return value.split("；", 1)[1].replace(" SDR", "")
            return value
        self.gain_card_median.value.setText(multiplier("中位增益 P50"))
        self.gain_card_p95.value.setText(multiplier("高亮增益 P95"))
        self.gain_card_max.value.setText(multiplier("最大增益"))

    def _start_analyze(self) -> None:
        path = Path(self.heic_path.text())
        if not path.is_file():
            QMessageBox.warning(self, "请选择文件", "请先选择有效的 HEIC/HEIF 文件。")
            return
        self._set_busy(True)
        self.summary_tree.clear()
        self.aux_list.clear()
        self.raw_metadata.setPlainText("正在分析…")
        self._run_worker(analyze_heic, path, done=self._analyze_done)

    def _start_extract(self) -> None:
        if not self._last_report:
            return
        target = QFileDialog.getExistingDirectory(self, "选择提取目录", str(self._last_report.path.parent))
        if not target:
            return
        self._set_busy(True)
        self._run_worker(extract_heic_assets, self._last_report.path, Path(target), done=self._extract_done)

    def _run_worker(
        self,
        function: Callable,
        *args,
        done: Callable,
        cancellable: bool = False,
    ) -> None:
        if self._thread is not None:
            QMessageBox.information(self, "任务正在运行", "请等待当前任务完成，或先停止转换。")
            return
        thread = QThread(self)
        self._cancel_token = CancellationToken() if cancellable else None
        worker = FunctionWorker(function, *args, cancel_token=self._cancel_token)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.log.connect(self._append_log)
        worker.progress.connect(self._on_progress)
        worker.finished.connect(done)
        worker.finished.connect(thread.quit)
        worker.cancelled.connect(self._worker_cancelled)
        worker.cancelled.connect(thread.quit)
        worker.failed.connect(self._worker_failed)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._thread_cleared)
        self._thread = thread
        self._worker = worker
        self.stop_button.setEnabled(cancellable)
        thread.start()

    def _stop_all_tasks(self) -> None:
        token = self._cancel_token
        if token is None or token.cancelled:
            return
        self.stop_button.setEnabled(False)
        self.progress_label.setText("正在停止所有转换任务…")
        self._append_log("收到停止请求：取消等待队列，并强制结束所有正在运行的编码器子进程。")
        token.cancel()

    def _thread_cleared(self) -> None:
        self._thread = None
        self._worker = None
        self._cancel_token = None
        self.stop_button.setEnabled(False)

    def _worker_cancelled(self) -> None:
        self._append_log("转换任务已停止。")
        self._set_busy(False)
        self.progress.setValue(0)
        self.progress_label.setText("已停止")

    def _worker_failed(self, trace: str) -> None:
        self._append_log(trace)
        self._set_busy(False)
        self.progress_label.setText("失败")
        QMessageBox.critical(self, "任务失败", trace.splitlines()[-1] if trace.splitlines() else trace)

    def _convert_done(self, result: object) -> None:
        self._set_busy(False)
        self.progress.setValue(100)
        self.progress_label.setText("完成")
        output = Path(str(result))
        if output.suffix.lower() == ".png":
            title = "导出完成"
            message = f"RAW 16-bit 增益图已导出：\n{output}"
        elif output.suffix.lower() == ".heic":
            title = "转换完成"
            message = f"HEIC HDR 已生成，并通过 10/12-bit Rec.2020/PQ 验证：\n{output}"
        else:
            title = "合成完成"
            message = f"AVIF 已生成并通过增益图验证：\n{output}"
        QMessageBox.information(self, title, message)

    def _analyze_done(self, result: object) -> None:
        self._set_busy(False)
        if not isinstance(result, HeicReport):
            return
        report = result
        self._last_report = report
        self.summary_tree.clear()
        for key, value in report.summary:
            self.summary_tree.addTopLevelItem(QTreeWidgetItem([key, value]))
        self.aux_list.clear()
        self.aux_list.addItems(report.auxiliary_types or ["未发现辅助图像"])
        self.raw_metadata.setPlainText(report.raw_text)
        self.card_gain.value.setText("存在" if report.has_gain_map else "未发现")
        self.card_gain.value.setStyleSheet("color: #64d98b;" if report.has_gain_map else "color: #ffb86b;")
        self.card_aux.value.setText(str(len(report.auxiliary_types)))
        depth = next((v for k, v in report.summary if k == "像素深度"), "—")
        self.card_depth.value.setText(depth.split("、")[0])
        self.analyze_button.setEnabled(True)
        self.extract_button.setEnabled(True)

    def _extract_done(self, result: object) -> None:
        self._set_busy(False)
        paths = result if isinstance(result, list) else []
        QMessageBox.information(self, "提取完成", f"已提取 {len(paths)} 个文件。")
        if paths:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(paths[0]).parent)))

    def _on_progress(self, value: int, message: str) -> None:
        self.progress.setValue(value)
        self.progress_label.setText(message)

    def _append_log(self, text: str) -> None:
        timestamp = QDateTime.currentDateTime().toString("HH:mm:ss.zzz")
        lines = str(text).splitlines() or [""]
        cursor = self.log_view.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText("\n".join(f"[{timestamp}] {line}" for line in lines) + "\n")
        self.log_view.setTextCursor(cursor)
        self.log_view.ensureCursorVisible()

    def _set_busy(self, busy: bool) -> None:
        self.convert_button.setEnabled(not busy)
        self.mode.setEnabled(not busy)
        self.output_format.setEnabled(not busy and self._mode_value() != InputMode.RAW_GAIN_EXPORT)
        self.batch_output_format.setEnabled(not busy)
        self.batch_workers.setEnabled(not busy)
        self.batch_scan_button.setEnabled(not busy)
        self.batch_start_button.setEnabled(not busy and bool(self._batch_scan and self._batch_scan.pairs))
        self.gain_analyze_button.setEnabled(not busy)
        self.analyze_button.setEnabled(not busy)
        self.extract_button.setEnabled(not busy and self._last_report is not None)
        if busy:
            self.convert_button.setText("正在处理…")
        else:
            self.stop_button.setEnabled(False)
            self._update_mode()

    def _open_output(self) -> None:
        text = self.output.text()
        path = Path(text).parent if text else Path.cwd()
        path.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _restore_settings(self) -> None:
        geometry = self.settings.value("geometry")
        if geometry:
            self.restoreGeometry(geometry)
        self.color_quality.setValue(int(self.settings.value("color_quality", 85)))
        self.gain_quality.setValue(int(self.settings.value("gain_quality", 80)))
        self.headroom.setValue(float(self.settings.value("headroom", 4.0)))
        self.speed.setValue(int(self.settings.value("speed", 6)))
        self.batch_workers.setValue(min(32, max(1, int(self.settings.value("batch_workers", 2)))))
        self._select_combo_data(self.output_format, self.settings.value("output_format", OutputFormat.AVIF.value))
        self._select_combo_data(self.batch_output_format, self.settings.value("batch_output_format", OutputFormat.AVIF.value))
        self._update_mode()
        recursive_value = self.settings.value("batch_recursive", True)
        self.batch_recursive.setChecked(str(recursive_value).lower() not in {"false", "0", "no"})
        self.batch_input.setText(str(self.settings.value("batch_input", "")))
        self.batch_output.setText(str(self.settings.value("batch_output", "")))

    def closeEvent(self, event) -> None:
        if self._thread is not None:
            answer = QMessageBox.question(
                self, "任务仍在运行", "任务尚未完成。退出前将停止所有转换，确定继续吗？"
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            if self._cancel_token is None:
                QMessageBox.information(self, "无法立即退出", "当前是扫描或分析任务，请等待它完成后再退出。")
                event.ignore()
                return
            self._stop_all_tasks()
            if not self._thread.wait(10000):
                QMessageBox.warning(self, "仍在停止", "RAW 解码尚未退出，请稍后再次关闭窗口。")
                event.ignore()
                return
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("color_quality", self.color_quality.value())
        self.settings.setValue("gain_quality", self.gain_quality.value())
        self.settings.setValue("headroom", self.headroom.value())
        self.settings.setValue("speed", self.speed.value())
        self.settings.setValue("batch_workers", self.batch_workers.value())
        self.settings.setValue("output_format", self._output_format_value().value)
        self.settings.setValue("batch_output_format", self._batch_output_format_value().value)
        self.settings.setValue("batch_recursive", self.batch_recursive.isChecked())
        self.settings.setValue("batch_input", self.batch_input.text())
        self.settings.setValue("batch_output", self.batch_output.text())
        super().closeEvent(event)


def apply_style(app: QApplication) -> None:
    app.setStyle("Fusion")
    app.setFont(QFont("Noto Sans SC", 10))
    app.setStyleSheet(
        """
        QWidget { background: #11131a; color: #e9ebf3; }
        QMainWindow, QScrollArea, QScrollArea > QWidget > QWidget { background: #11131a; }
        QLabel#appTitle { font-size: 26px; font-weight: 700; color: #ffffff; }
        QLabel#subtitle, QLabel#muted { color: #9399aa; }
        QLabel#sectionTitle { font-size: 14px; font-weight: 650; color: #ffffff; }
        QLabel#fieldLabel { color: #b9bdca; font-weight: 600; }
        QLabel#statusPill { background: #193528; color: #73dda0; border: 1px solid #28583e; border-radius: 13px; padding: 5px 11px; }
        QLabel#statusError { background: #402126; color: #ff8c96; border: 1px solid #63323a; border-radius: 13px; padding: 5px 11px; }
        QLabel#callout { background: #1a1d29; color: #aeb4c7; border-left: 3px solid #7568ff; border-radius: 5px; padding: 10px 12px; }
        QLabel#metricValue { font-size: 21px; font-weight: 700; color: #f5f6fa; }
        QGroupBox { background: #171a23; border: 1px solid #292d3a; border-radius: 10px; margin-top: 14px; padding: 15px 14px 13px 14px; font-weight: 650; }
        QGroupBox::title { subcontrol-origin: margin; left: 14px; padding: 0 6px; color: #dfe2eb; }
        QFrame#sidePanel, QFrame#metricCard { background: #171a23; border: 1px solid #292d3a; border-radius: 10px; }
        QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QTextEdit, QTreeWidget, QListWidget { background: #0e1016; border: 1px solid #303544; border-radius: 7px; padding: 7px 9px; selection-background-color: #6657e8; }
        QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus, QTextEdit:focus { border: 1px solid #7568ff; }
        QComboBox::drop-down { border: none; width: 24px; }
        QPushButton { background: #272b38; border: 1px solid #383d4c; border-radius: 7px; padding: 8px 14px; font-weight: 600; }
        QPushButton:hover { background: #303545; border-color: #4a5062; }
        QPushButton:disabled { color: #626777; background: #1c1f28; border-color: #282b35; }
        QPushButton#primaryButton { background: #6959e8; border-color: #7b6df2; color: white; }
        QPushButton#primaryButton:hover { background: #7869ef; }
        QPushButton#secondaryButton { background: #20232e; }
        QPushButton#ghostButton { background: transparent; border-color: #2b2f3a; color: #a7acba; }
        QTabWidget::pane { border: none; }
        QTabBar::tab { background: transparent; color: #9096a8; padding: 10px 16px; border-bottom: 2px solid transparent; }
        QTabBar::tab:selected { color: #ffffff; border-bottom-color: #7568ff; }
        QTabBar::tab:hover { color: #d6d9e2; }
        QProgressBar { background: #0e1016; border: 1px solid #2b2f3b; border-radius: 5px; height: 9px; }
        QProgressBar::chunk { background: #7568ff; border-radius: 4px; }
        QHeaderView::section { background: #1d202a; color: #aeb3c1; border: none; border-bottom: 1px solid #323643; padding: 7px; }
        QTreeWidget, QListWidget { alternate-background-color: #141720; }
        QCheckBox { spacing: 8px; }
        QCheckBox::indicator { width: 17px; height: 17px; border: 1px solid #454a5b; border-radius: 4px; background: #0f1117; }
        QCheckBox::indicator:checked { background: #7568ff; border-color: #887cff; }
        QScrollBar:vertical { background: transparent; width: 11px; margin: 2px; }
        QScrollBar::handle:vertical { background: #373b49; border-radius: 5px; min-height: 30px; }
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
        QSplitter::handle { background: #242834; width: 1px; }
        """
    )


