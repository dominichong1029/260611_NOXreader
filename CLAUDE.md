# PSG Viewer — 開發與打包規範

本檔由 Claude Code 每次自動載入；以下為**必須遵循**的專案規範。

## 打包成 exe（PyInstaller）

### 標準流程
```bash
python build_exe.py        # one-folder 模式，輸出 dist/PSGviewer/
```
- one-folder（非 onefile）：啟動快、相依完整、可直接複製到無 Python 的 Windows 電腦。
- `build_exe.py` 為唯一打包入口；`PSGviewer.spec` 為等價備援，二擇一即可。
- `dist/`、`build/`、`Release/`、`build_log*.txt` 皆為產物，**不納入版控**（已忽略）。

### ⚠️ 體積暴增陷阱（曾犯：2.5GB）
**症狀**：exe 從正常 ~450MB 暴增到 2GB+。
**原因**：在「同時裝有大型 ML 套件（tensorflow/torch/transformers/onnxruntime/numba…）的 Python 環境」直接打包時，PyInstaller 會把這些**與 PSG viewer 完全無關**的套件一併收進來（tensorflow 1.1G、torch 365M、llvmlite 102M…共約 1.7GB）。
**防呆（已內建於 build_exe.py）**：`--exclude-module` 明確排除這些套件，使打包結果**與環境無關**。新增第三方相依前，先確認原始碼真的有用到，否則加進排除清單。
- 本程式實際只用：PyQt6、pyqtgraph、numpy、scipy、pyedflib、openpyxl、xlrd、python-docx、imageio_ffmpeg。

### 打包後必做驗證（每次都要，勿略過）
1. **量體積**：`du -sh dist/PSGviewer` —— 基準 ~455MB（純 viewer ~346MB ＋ 音頻 ffmpeg/QtMultimedia ~110MB）。明顯超出代表又收進無關套件。
2. **確認無誤收**：`dist/PSGviewer/_internal/` 下不應有 tensorflow / torch / transformers / onnxruntime / llvmlite / pandas / matplotlib / sklearn 等。
3. **確認音頻元件在**：`_internal/imageio_ffmpeg/binaries/ffmpeg-win-*.exe` 與 `_internal/PyQt6/Qt6/plugins/multimedia/ffmpegmediaplugin.dll` 必須存在。
4. **啟動冒煙**：`Start-Process` 跑 exe，等 ~12s 確認進程存活未崩潰，再關閉。

## UI 樣式/排版變更 — 必須真實渲染驗證
醫療軟體，勿草率交付；改 UI 後**不可只靠 API 文件或 `fontMetrics` 推論**，要實際渲染成圖檢視。
- 用既有 smoke 流程：`PSGViewer()` →（`WA_DontShowOnScreen`）→ `v._load_folder(input/D18_...)` → pump 事件 → `widget.grab().save(png)` → 用眼睛看圖（必要時 `.scaled(x4, Smooth)`）。
- **字型/省略相關渲染絕不可用 `QT_QPA_PLATFORM=offscreen`**：offscreen 不附字型，`QFontMetrics` 度量失真，結果與真實畫面不符。要用原生平台（PowerShell 直接跑）取得真字型。
- 既有回歸守門 smoke：`scripts/smoke_perf_ui.py`、`scripts/smoke_audio_play_ui.py`、`scripts/smoke_channel_name_elide_ui.py` 等（皆載入真 D18）。

## 通用
- 回應一律繁體中文。
- 真實測試資料在 `input/`（D01/D02/D03/D18/D20）；D18 為主要驗證對象。
