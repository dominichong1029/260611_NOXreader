# Nox PSG EDF / Raw 檔案讀取方案

### 視覺化界面特點（NoxPSG Viewer）
執行 `python noxpsg_viewer.py` 即可啟動（已自動嘗試載入 input/）。

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
- 如果 `dist\NoxPSGViewer\NoxPSGViewer.exe` 已存在 → 直接啟動獨立版（**完全不需要 Python**）。
- 如果沒有預先打包的 exe → 自動偵測 Python、安裝所有依賴（包含 PyQt6 + pyqtgraph），然後從原始碼啟動 `noxpsg_viewer.py`。

`start.bat` 已經把原本分散的安裝與啟動邏輯整合成單一檔案，雙擊即可使用。
- 重開後：上次勾選通道、position 樣式、時間視窗、scale 自動恢復。

所有變更嚴謹遵循設計文件，醫療資料保真（raw clusters 可調、不發明標籤）、完整瀏覽性優先。5 PR 全部完成，應用已就緒供使用者測試與 release。

（詳細設計與各 PR 執行總結見 design/ 目錄：grok-design-doc-2319f73b.md 及 exec-pr*-summary-*.md）
