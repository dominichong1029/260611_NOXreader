# Nox PSG EDF / Raw 檔案最佳化讀取方案

## 問題背景與資料格式分析

本專案的 `input/` 目錄下有兩個儀器檢測資料夾（D18_... 與 D20_...），均來自 Nox A1 PSG 睡眠檢測儀器。

### 實際檔案結構
- `Dxx_..._PSG edf_....edf`：原廠嘗試匯出的 EDF 檔案。
  - **無法直接使用**：header 中的 Physical Maximum / Minimum 等欄位損壞，導致 pyedflib、MNE-Python 等標準函式庫開啟時直接報錯 `not EDF(+) or BDF(+) compliant (Physical Maximum)`。
- `... raw data_.../時間戳-hash/*.ndf`：**真正的原始資料來源**（53~60 個檔案）。
  - 每一個 `.ndf` 為**單通道、無標頭**的二進位樣本檔。
  - 經分析為 little-endian `int16`，每樣本 2 bytes。檔案大小與 `SETUP.INI` 宣告的取樣率完全吻合（EEG 200Hz 約 9.7MB，7 小時錄音）。
- `SETUP.INI` / `DEVICE.INI`：通道定義、取樣率、單位、顯示名稱、儀器資訊。
- `DeviceEvents.nef`：實際上是 **SQLite 資料庫**（副檔名偽裝），內含裝置事件標記。
- `... event_....xls`：事件報告（睡眠事件、AHI 等）。此檔案為損壞的 OLE 結構，標準 xlrd / pandas 無法可靠解析（建議以人工或 Noxturnal 軟體重新匯出）。

**結論**：要「最佳化開啟讀取」，**必須以 raw .ndf + SETUP.INI 為主**，並提供機制把資料轉成標準相容格式，取代原廠壞掉的 .edf。

## 設計的核心最佳化原則

1. **極低記憶體開銷（Lazy + Memory Map）**
   - 使用 `numpy.memmap(dtype='<i2')` 開檔。
   - 開啟時幾乎不佔 RAM，只有真正 `get_data(start_sec, duration)` 時 OS 才會 page-in 需要的區塊。
   - 適合 7 小時、數十通道、總計數 GB 的 PSG 資料。

2. **以「時間（秒）」為單位的切片 API**
   - 不需要知道 sample index 或 datarecord。
   - `rec.get_data("C3", start_sec=3600, duration=300)` 直接給 1 小時後的 5 分鐘資料。

3. **通道名稱友好解析**
   - 同時支援檔名 stem（`c3`、`flow`、`spo2`）與 SETUP 裡的 display name。
   - 內建正規化比對，自動處理大小寫、空格、縮寫。

4. **多病患批次管理**
   - `NoxStudy` 自動掃描 `input/` 下所有符合 `Dxx_*PSG*` 模式的資料夾。
   - 一次 load 兩個病人，各自獨立 `NoxRecording`。

5. **修復相容性（最重要）**
   - 提供 `export_to_standard_edf()` 把任意通道子集 + 任意長度，輸出成**完全符合 EDF+ 規範**的檔案。
   - 輸出檔可直接被 EDFBrowser、Polyman、MNE、Python 其他分析工具開啟使用。
   - 這是「檔案格式最佳化」的核心價值：把儀器 proprietary / 損壞格式，轉成業界標準格式。

6. **進階使用情境支援**
   - `iter_epochs()`：30 秒 epoch 產生器，零記憶體壓力做睡眠分期或特徵工程。
   - `get_events()`：從 DeviceEvents.nef 取出裝置事件（完整臨床標記仍需依賴人工 event 檔）。
   - 快取 memmap，避免重複開檔。

## 安裝與使用

```bash
pip install -r requirements.txt
```

### 基本用法

```python
from noxreader import NoxStudy, export_to_standard_edf

# 一次掃描 input 資料夾下所有病患
study = NoxStudy("input")
print(study.patients)
# ['D18_17923057_PSG_20260511', 'D20_18075135_PSG_20260512']

rec = study.get("D18")          # 或 study.get("D20")
print(rec)                      # <NoxRecording ... duration=7.09h ...>

# 列出通道（自動過濾 impedance）
print(rec.list_channels()[:10])

# 讀取資料（核心最佳化操作）
c3, fs = rec.get_data("c3", start_sec=1800, duration=30)   # 從 30 分鐘開始取 30 秒
print(c3.shape, fs)   # (6000,) 200.0

# 也可以用完整顯示名稱或正規化名稱
spo2, _ = rec.get_data("SpO2", start_sec=0, duration=60)
flow, _ = rec.get_data("flow", start_sec=3600, duration=10)

# 產生器：適合大量 epoch 處理（ML / 睡眠分期）
for epoch_idx, t_start, data_dict in rec.iter_epochs(
        channels=["c3", "c4", "e1", "e2", "ecg", "flow"],
        epoch_sec=30,
        step_sec=30
):
    # data_dict["c3"] 就是這個 30 秒 epoch 的 numpy array
    pass

# 事件（來自 DeviceEvents.nef）
events = rec.get_events()
print(len(events))
```

### 把資料轉成標準 EDF（修復原廠壞檔）

```python
# 只匯出核心 EEG + 呼吸 + SpO2，截斷前 2 小時，產生可標準開啟的檔案
out_file = export_to_standard_edf(
    rec,
    channel_names=["c3", "c4", "o1", "o2", "e1", "e2", "ecg", "flow", "spo2", "thorax rip"],
    out_path="output/D18_standard.edf",
    max_duration_sec=2*3600
)
print("已產生標準 EDF：", out_file)
```

匯出的檔案可以用任何 EDF 工具正常開啟與分析。

## 通道對應與注意事項

- 主要高頻通道（約 200 Hz）：C3/C4/F3/F4/O1/O2、E1/E2、ECG、Leg EMG 等。
- 中頻：Nasal Pressure、RIP belts、Audio、Snore。
- 低頻（1 Hz）：SpO2、Heart Rate、Position、Light、Voltages、Impedance。
- 部分通道（如 ambient light c1）實際取樣率可能與 SETUP 宣告不同，讀取時以實際 .ndf 檔案大小推算為主。
- 原始數值為儀器 ADC 整數值，單位標示來自 SETUP（V、cmH2O、% 等）。如需轉成標準 uV，請在分析端乘以對應 gain，或在 `export_to_standard_edf` 傳入 `physical_range`。

## 為什麼這個方案是「最佳化」的？

| 項目             | 原廠 .edf                  | 本方案 (raw .ndf + memmap)          |
|------------------|----------------------------|-------------------------------------|
| 可開啟性         | 壞掉（標準 lib 拒絕）      | 100% 正常 + 可輸出標準 EDF          |
| 記憶體           | 需一次載入整個檔案         | 按需載入，切片幾乎零額外成本        |
| 通道選擇         | 全部或無                   | 任意子集 + 任意時間區段             |
| 多病人處理       | 手動逐檔                     | NoxStudy 自動管理                   |
| 後續分析相容     | 幾乎不可用                 | 可直接餵 MNE / 特徵工程 / 深度學習  |
| 事件整合         | xls 損壞                   | 內建 sqlite + 預留擴充點            |

## 後續建議擴充方向

- 增加自動 scaling（從儀器校正檔或典型 PSG 增益推導物理單位）。
- 完整解析 clinical events（可結合 docx 報告或請使用者提供乾淨的 event 表格）。
- 支援直接輸出為 MNE-Python `RawArray` / `mne.io.Raw`。
- 快取層：第一次讀取後自動 dump 成 `.npy` 或 zarr，第二次瞬間開啟。
- 增加 CLI：`python -m noxreader export --patient D18 --channels C3,SpO2 --from 0 --to 3600`。

---

本方案已針對您提供的兩個真實資料夾完整測試通過（包含成功匯出並重新用 pyedflib 開啟標準 EDF）。

如有特定需求（scaling、更多事件來源、輸出格式、整合 MNE 等），歡迎繼續討論，我們可以快速迭代！

---

## 新增：專業視覺化瀏覽器 + 可執行檔打包 (2026-06)

### 調研參考
市面評價最高的 EDF/PSG 瀏覽器：
- **EDFbrowser** (teuniz.net/edfbrowser)：免費開源金標準。支援大檔、精確 crosshair 測量、矩形/滾輪縮放、annotations 列表跳轉、montage、多檔同時顯示。單一執行檔。
- **Polyman**：適合手動睡眠評分 + 影片同步。
- **Noxturnal** (官方 Nox 軟體)：可自訂 Signal Sheets / Workspace Layouts、自動分析即時顯示、熱鍵、事件整合、報告產生。我們的 UI 設計大量參考這兩個工具的交互模式與「結構性臨床資料展示」。

### 視覺化界面特點（PSG Viewer）
執行 `python psg_viewer.py` 即可啟動（已自動嘗試載入 input/）。

**結構性、嚴謹、完整展示**：
- **研究概覽區**：病患 ID、總時長、開始時間、裝置序號 (來自 DEVICE.INI)、通道數、資料路徑。
- **左側控制面板**：
  - 通道表格（名稱、fs、單位、樣本數）+ 勾選框即時顯示/隱藏。
  - 預設組合按鈕：「標準 PSG」(EEG+EOG+ECG+核心呼吸+SpO2)、「呼吸重點」、「心肺」、「全選/清除」。
  - 全域振幅縮放。
- **時間導航（參考 EDFbrowser）**：
  - 水平滑桿 + 精確 spinbox (開始秒數 + 視窗長度)。
  - 快速跳鈕 (-5m / -1m / -30s / +30s / +1m / +5m)。
  - 「Fit 全長」一鍵顯示整個錄音。
  - 狀態列即時顯示「視窗: HH:MM:SS - HH:MM:SS / 總長 HH:MM:SS」。
- **中央專業波形檢視（pyqtgraph 高性能實現）**：
  - 多通道垂直堆疊（與 Noxturnal Signal Sheet / EDFbrowser 類似）。
  - 共享時間軸、拖曳平移、滾輪縮放、框選縮放。
  - 頂部小型「概觀軸」+ LinearRegionItem 快速定位。
  - 事件垂直標記線。
- **右側事件面板**：
  - 表格顯示從 DeviceEvents.nef 解析的事件 (時間、類型、位置、備註)。
  - 點擊「跳轉」或雙擊表格列 → 視窗立即置中該時間點。
  - 內建說明文字提醒：完整臨床事件在 .xls/.docx（因格式損壞，建議搭配官方 Noxturnal）。
- **一鍵匯出**：按鈕直接呼叫我們之前實作的 `export_to_standard_edf`，產生可在任何 EDF 工具開啟的標準檔案。
- **鍵盤快速鍵**：方向鍵平移、Ctrl+方向鍵大步、F = Fit 全長、Ctrl+E = 匯出。

**完整臨床資料結構展示**：
- 自動解析 raw 資料夾、SETUP.INI、DEVICE.INI、事件 DB。
- 支援直接開啟「input/」根目錄（自動發現兩個病患）或單一病患資料夾。
- 患者切換下拉即時切換錄音。
- 輔助檔案與報告提示區（未來可擴充預覽 .docx 文字）。

### 打包成可執行檔案 (.exe)
**最簡單啟動方式（推薦所有使用者）：**

直接在檔案總管**雙擊 `start.bat`** 即可：
- 如果 `dist\PSGviewer\PSGviewer.exe` 已存在 → 直接啟動獨立版（**完全不需要 Python**）。
- 如果沒有預先打包的 exe → 自動偵測 Python、安裝所有依賴（包含 PyQt6 + pyqtgraph），然後從原始碼啟動 `psg_viewer.py`。

`start.bat` 已經把原本分散的安裝與啟動邏輯整合成單一檔案，雙擊即可使用。

---

**重新打包 / 產生 exe 的方式：**

1. 安裝 PyInstaller（只需一次）：
   ```powershell
   pip install pyinstaller
   ```
2. 執行：
   ```powershell
   python build_exe.py
   ```

   這個腳本會自動：
   - 清理舊的 `build/` 與 `dist/`
   - 使用 PyInstaller 完整打包（one-folder 模式，推薦）
   - 打包後自動刪除 `build/` 暫存資料夾
   - 在 `dist/PSGviewer/` 產生「使用前必讀.txt」

3. 打包後的使用：
   - 把整個 `dist\PSGviewer\` 資料夾複製到任何 Windows 電腦。
   - **只雙擊 `PSGviewer.exe`**（絕對不要執行 build 資料夾裡的 exe）。
   - 之後只要雙擊專案根目錄的 `start.bat` 就能一鍵啟動。

**打包注意**：
- 第一次打包較久（需分析所有 numpy / PyQt6 / pyqtgraph 相依）。
- 建議在乾淨 venv 中打包以減小體積。
- 如需單一 .exe（--onefile），請編輯 build_exe.py 取消註解該行（但 one-folder 通常更穩定）。
- build 資料夾是暫存檔，執行它會出現 "Failed to load Python DLL" 錯誤，build_exe.py 會自動清理。

### 快速開始（推薦）
**只要雙擊專案根目錄的 `start.bat` 即可**：
- 有打包好的 exe 時：直接啟動獨立 GUI（無需任何安裝）。
- 沒有 exe 時：自動安裝依賴後啟動（只需 Python 在 PATH）。

傳統指令方式（開發者）：
```powershell
# 直接啟動（會自動處理依賴）
python psg_viewer.py

# 或重新打包成獨立 exe
pip install pyinstaller
python build_exe.py
```

此視覺化方案完整保留了之前「最佳化讀取」的優勢（memmap 切片只載入目前視窗資料），並提供專業級交互體驗，適合臨床醫師、研究人員、技術員直接瀏覽與切換資料。

所有原始碼與打包腳本均已放入專案根目錄，隨時可修改擴充（例如加入基本濾波、hypnogram 概觀、影片同步等）。歡迎提出進一步需求！

---

## 新功能：PR1~PR5 完整 UX/醫療瀏覽性改版 (2026-06, design 2319f73b)

本 Viewer 經 5 個增量 PR 完成重大改版，**完整解決原始使用者需求**：
- Position（體位）類別訊號渲染與參考專業軟體（Noxturnal Summary Graph）一致 + 可自訂 + 持久化記憶。
- 病患基礎 + 臨床資訊優先排版（banner 置頂，RDI/Supine position breakdown 為醫療重點，非僅技術 meta）。

### 主要新功能（PR3 banner + PR2 renderer + PR4 persistence + PR5 polish）

**1. PatientInfoBanner + Clinical Summary（頂部與右側，來自 word_*.docx 報告）**
- Prominent 顯示：MR#、年齡/性別、BMI（超重/肥胖用橙/紅 accent）、RDI 21.3/hr（高風險紅色）、Supine 210.4min (RDI 19.7) | Left ... 等 position breakdown。
- 次要：研究日期/總錄時間/效率、裝置 A1 SN:972903069（PR1 修復 DEVICE.INI 解析）。
- Fallback：「報告未找到，僅顯示技術資訊」+ rec.name proxy。
- **完整臨床 tooltips**：滑鼠停留各欄位顯示「仰臥位 RDI 最高，增加 OSA 風險」等醫學意義說明。
- ClinicalSummary dock：RDI、睡眠效率、position 列表 + Supine 風險 progress bar（視覺強調）。

**2. Position 特殊渲染 + Style Dialog（中央波形區）**
- 自動偵測 "position" channel（PR1 is_position_channel）。
- 預設 **step_fill**：離散水平 step 線 + 彩色背景 bands（LinearRegionItem）+ 標籤文字（Supine/Left/Right/Prone/Upright 臨床英文）。
- Y 軸 ticks 直接顯示姿勢標籤；只重建目前視窗資料（效能佳）。
- **右鍵 context menu 完整整合**：position 提供「編輯 position 樣式...」；**任一 plot 皆提供「Fit 此通道 Y 軸」**（快速醫療檢視該通道振幅）。
- PositionStyleDialog：可選 render mode (line/step/step_fill)、thresholds、labels、colors（逗號分隔編輯）。
- 預設 palette 文件化：Supine 紅色風險 accent (#d62728) 與 banner 一致（Open Q3 已決策保留）。
- 低頻 channel（position/spo2 等）表格列有額外 tooltip 說明 header strip + inferred_fs 修正（PR1）。

**3. 30s epoch 強調 + 醫療 UX polish (PR5)**
- 導航按鈕明確標示 "-30s (epoch)" / "+30s (epoch)" + 新增 **「Fit 30s epoch」按鈕**（一鍵設 30s 臨床標準 epoch 視窗）。
- Hint 區強化提示右鍵功能與 epoch。
- 時間控制群組 tooltip 說明「30s 為標準臨床 epoch」。
- 更多 clinical tooltips 遍佈 banner、position plot、channel table、nav。
- 輕量 medical accent stylesheet（banner/clinical 使用高對比淺色背景 + 風險色；保留 Fusion 風格）。
- overview 增加小 note 說明 position 詳細渲染位於中央。

**4. 持久化 (PR4)**
- 使用 QSettings（Win registry / 跨平台）記住：
  - per-rec：visible channels、last time window、position render mode/thresholds/labels/colors。
  - global：scale。
- 重開應用或切換病患後自動恢復上次狀態。
- 工具選單 → 「清除偏好設定」：立即回 clinical defaults（常見 PSG 通道 + position step_fill）。
- 初次無 prefs 即用合理臨床預設。

**5. 基礎改進 (PR1)**
- 低頻 header strip（position 等開頭 XML junk 自動移除）。
- fs inference（position ~19.5Hz，正確 30s 視窗 ~586 samples）。
- get_patient_info() 可靠解析 docx（demographics + BODY POSITION SUMMARY）。
- device_info 修復（DEVICE.INI 手動 section 解析）。

### 使用方式
1. `python psg_viewer.py`（或雙擊 start.bat / dist 內 exe）。
2. 自動載入 `input/` （或「開啟資料夾」選單一/整個 input）。
3. 左側「Position + Resp」預設即顯示 position（step 彩色 bands + 標籤）+ flow/spo2。
4. 右鍵 position 波形 → 「編輯 ... 樣式」自訂（accept 後立即生效並記住）。
5. 點擊「Fit 30s epoch」或 ±30s(epoch) 按鈕快速檢視臨床 epoch。
6. Banner 永遠可見頂部；右側 Clinical Summary 顯示 Supine RDI 重點。
7. 調整任何通道/時間/樣式後，關閉重開即恢復（prefs 跨 session）。
8. 匯出 EDF 仍正常（使用 strip 後乾淨資料）。

**截圖描述（建議使用者自行截圖更新）**：
- 頂部：Banner 顯示 "MR#: 17923057-S0261223   年齡: 50  性別: Male   BMI: 29.6 (超重)   RDI: 21.3/hr   Supine 210.4min (RDI 19.7) | ..."
- 中央：position 波形為階梯式彩色區塊（Supine 紅色長段）+ 左側 Y ticks "Supine / Left / ..."，非藍色連續線。
- 右鍵 menu：任一 plot 出現 "Fit 此通道 Y 軸"；position 多 "編輯 ... 樣式..."。
- 左側 channel table：position 列第6欄為「樣式」按鈕；低頻列有 tooltip。
- 時間區：有「Fit 30s epoch」按鈕 + epoch 標示。
- 重開後：上次勾選通道、position 樣式、時間視窗、scale 自動恢復。

所有變更嚴謹遵循設計文件，醫療資料保真（raw clusters 可調、不發明標籤）、完整瀏覽性優先。5 PR 全部完成，應用已就緒供使用者測試與 release。

（詳細設計與各 PR 執行總結見 design/ 目錄：grok-design-doc-2319f73b.md 及 exec-pr*-summary-*.md）