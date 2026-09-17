# Gain Studio 0.3.0

Gain Studio 是 Windows 桌面 GUI，用于把 RAW、HIF/HEIF、SDR/HDR 图像或自定义增益图转换为 HDR 照片，并分析 Apple HEIC 与增益图信息。

## 输出格式

### AVIF（推荐用于完整 gain map 工作流）

- 使用 **SDR 底图 + HDR 增益图** 的标准 AVIF gain map 结构。
- 支持主图与增益图独立设置质量、位深、色度采样和增益图缩小倍数。
- 不支持 HDR gain map 的旧软件仍可回退显示 SDR 底图。
- 由 libavif 负责计算、封装和验证 gain map 元数据。

### HEIC（用于不兼容 AVIF 的手机）

- 输出 **10/12-bit HEVC、Rec.2020 + PQ、CICP 9/16/9** 的单层 HDR HEIC。
- 支持质量、10/12-bit 位深、420/422/444 色度采样和编码速度设置；选择 8-bit 时会自动提升到 10-bit。
- HEIC 输出保留可复制的 EXIF、XMP、IPTC、MakerNotes、拍摄时间、设备、镜头和 GPS 等元数据。
- 当前 HEIC 输出**不是**“SDR 底图 + gain map”结构，因此没有 SDR gain-map 回退；实际显示效果取决于手机/系统的 HDR HEIC 解码与色调映射能力。
- Apple 原生 gain-map HEIC 使用 Apple 辅助图类型和部分私有语义，目前公开的 libheif/pillow-heif 写入接口不能可靠生成相同结构，因此没有伪装成 Apple gain-map HEIC。

## 输入与转换模式

- **RAW 自动**：从 RAW 提取全尺寸 `JpgFromRaw`/预览作为 SDR 底图，以 RAW 线性高光重建 HDR。
- **RAW + 同名 HIF/HEIF**：使用相机 RAW+HIF 双格式中同名 HIF 作为 SDR 底图，RAW 提供高光信息。
- **RAW 单独导出增益图**：导出 16-bit 灰度 PNG；黑色代表 0 stops/1× SDR，白色代表设置的最大 HDR 余量。
- **指定 SDR 底图 + 指定增益图**：将灰度/RGB 增益图映射到指定 stops 范围，自动缩放对齐并重建 HDR。
- **指定 SDR 底图 + HDR 参考图**：为 AVIF 计算 gain map；HEIC 输出目前要求 HDR 参考图为 16-bit Rec.2020/PQ PNG。
- **Ultra HDR JPEG 转换**：可转为 AVIF gain map，或先恢复 PQ HDR 后编码为 HEIC。

Sony ARW 等 RAW 通常没有一张可直接“抠出”的独立 gain map。本程序采用可执行的重建流程：以相机 JPEG/HIF 作为 SDR 渲染参考，以 RAW 线性数据作为高光来源，再计算标准 AVIF gain map、编码 PQ HEIC，或导出独立增益图。

## 批量处理与停止

- 扫描目录及子目录中同目录、同主文件名的 RAW + HIF/HEIF 组合。
- 批量输出可独立选择 AVIF 或 HEIC，并保留相对目录结构。
- 并行任务数范围为 **1–32**。
- 顶部“停止所有转换”按钮会立即取消未开始的队列，通知所有转换工作线程退出，并强制结束正在运行的外部编码器及其子进程。
- RAW 解码库的单次 `postprocess()` 不能从内部安全强杀；若停止发生在该步骤，线程会在当前 RAW 解码调用返回后立即退出。程序不会把未验证的临时文件覆盖到正式输出。

### 性能建议

- 单个高像素 RAW 任务通常短时占用约 300–500 MB，实际峰值还受分辨率、位深和中间数组影响。
- 16 GB 内存建议并行数 2；32 GB 以上建议 3–4。
- 32 并行理论上可能超过 10–16 GB 内存，并造成明显 CPU、磁盘和页面文件压力，只建议高核心数、大内存设备使用。
- 批量时每个 HIF 解码器限制为单线程，避免外层并发与编解码器内部并发叠加。

## 增益图分析

- 可分析独立 PNG/JPEG/TIFF 增益图，也可直接分析带 gain map 的 AVIF。
- AVIF 会读取容器中的 Gain Map Min/Max/Gamma、Base headroom 与 Alternate headroom，并提取内嵌增益图进行统计。
- 显示平均值、P0/P1/P5/P50/P90/P95/P99/P100、相对 SDR 亮度倍数，以及 `≥1×/2×/4×/8×/16× SDR` 的像素覆盖率。
- 大图最多采样约 200 万像素，以控制分析时的内存和 CPU 占用。

亮度换算关系：`亮度倍数 = 2^stops`。例如 1 stop=2×、2 stops=4×、3 stops=8×。

## Apple HEIC 信息分析

- 显示尺寸、位深、容器品牌、ICC、HDR gain map 版本、设备、镜头和 GPS。
- 列出并提取 `hdrgainmap`、sky matte、portrait effects matte、skin matte、style delta map、linear thumbnail 等辅助图。
- 此功能用于分析 iPhone/Apple HEIC，也可检查 Gain Studio 输出的 HEIC 基本结构和元数据。

## 质量参数

- SDR/主图质量、HDR/增益图质量。
- 主图 8/10/12-bit；HEIC HDR 最低为 10-bit。
- 主图 `420/422/444` 色度采样；AVIF 增益图 `400/420/422/444` 采样。
- AVIF 增益图 1/2/4/8 倍缩小。
- HDR headroom、RAW EV 微调、外部增益图 gamma/最小增益、编码速度。
- 中间文件默认自动清理，也可保留用于检查。

## 元数据与日志

- 尽力保留 EXIF、XMP、IPTC、MakerNotes、拍摄时间、设备、镜头、GPS 等元数据。
- 源 ICC/ColorSpace 标签不会在像素发生色彩转换后被盲目复制；输出使用编码器写入的正确 CICP 色彩信令。
- HIF 中的 Display P3/其他 ICC 会先转换为 sRGB 工作空间，再去除中间文件 ICC，避免 gain-map 计算冲突。
- 日志支持 UTF-8、GB18030/CP936 和 Windows 本地代码页的外部工具输出，并记录时间戳、命令、退出码、耗时、输入输出、编码参数、RAW 分析和验证结果。

## 运行源码

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run.ps1
```

或：

```powershell
.\.venv\Scripts\python.exe main.py
```

## 运行测试

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\test.ps1
```

## 构建 Windows 程序

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\build.ps1
```

产物：

- `dist\Gain Studio\Gain Studio.exe`
- `dist\Gain-Studio-Windows-x64.zip`

采用 PyInstaller onedir，首次启动无需临时解包大型运行库。构建脚本会隔离可能与 Qt 冲突的第三方 ICU DLL，并启动真实 GUI 窗口做 smoke test，避免 `QtCore: DLL load failed` 回归。

## 组件

- libavif 1.4.1：AVIF gain-map 编码与验证。
- pillow-heif/libheif：10/12-bit HEVC HEIC 编码、HEIC/HIF 解码与辅助图读取。
- ExifTool 13.59：元数据读取、提取与复制。
- rawpy/libraw：RAW 解码。
- Pillow/ImageCms：图像与 ICC 色彩转换。
- PySide6：GUI。

第三方许可证见 `licenses`、`tools/exiftool/exiftool_files/LICENSE` 和 `THIRD_PARTY_NOTICES.md`。
