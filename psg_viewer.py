#!/usr/bin/env python3
"""
PSG Viewer
專業級 PSG / Nox A1 臨床資料視覺化瀏覽器
- 參考 EDFbrowser (gold standard 免費開源) 與 Noxturnal (官方 Nox 軟體) 的交互設計
- 結構化、嚴謹展示整個資料夾的臨床資料
- 使用 PyQt6 + pyqtgraph 實現高性能多通道波形檢視
- 支援資料夾選擇、患者切換、通道控制、時間導航、事件跳轉
- 一鍵匯出標準相容 EDF (修復原廠壞檔)
- 可打包為獨立 Windows .exe

執行方式：
  python psg_viewer.py
"""

import sys
import os
from pathlib import Path
from datetime import datetime, timedelta
import numpy as np
import time
from typing import Optional

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QSplitter,
    QTableWidget, QTableWidgetItem, QTableView, QHeaderView, QPushButton, QToolTip,
    QLabel, QComboBox, QSpinBox, QDoubleSpinBox, QSlider, QToolBar, QMenuBar,
    QFileDialog, QMessageBox, QTextEdit, QGroupBox, QFormLayout, QCheckBox,
    QProgressBar, QStatusBar, QAbstractItemView, QFrame, QGridLayout, QScrollArea, QSizePolicy, QLineEdit,
    QDialog, QListWidget, QListWidgetItem, QMenu, QStyle, QStyleOptionViewItem,
)
from PyQt6.QtCore import (
    Qt, QTimer, pyqtSignal, QSettings, QSize, QEvent, QRect,
    QAbstractTableModel, QModelIndex, QSortFilterProxyModel,
)
from PyQt6.QtGui import QAction, QActionGroup, QKeySequence, QFont, QColor, QGuiApplication, QCursor

import pyqtgraph as pg

# 我們的優化後端
from noxreader import NoxStudy, NoxRecording, NoxEdfRecording, export_to_standard_edf

# PR2: special position renderer + VizSettings + dialog (minimal)
from viz.position_renderer import PositionStepRenderer, VizSettings


def format_hms(seconds: float, include_ms: bool = False) -> str:
    """將秒數格式化為 HH:MM:SS（或含毫秒 HH:MM:SS.mmm）。無小時時仍顯示 00:，避免與 MM:SS 混淆。"""
    if seconds < 0:
        seconds = 0
    if include_ms:
        total_ms = int(round(seconds * 1000))
        h = total_ms // 3600000
        m = (total_ms % 3600000) // 60000
        s = (total_ms % 60000) // 1000
        ms = total_ms % 1000
        return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"
    total_sec = int(seconds)
    h = total_sec // 3600
    m = (total_sec % 3600) // 60
    s = total_sec % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def format_time_label(seconds: float) -> str:
    return format_hms(seconds)


CLINICAL_DEFAULT_CHANNELS = ["snore", "spo2", "nasal pressure", "thermistor"]


def _normalize_channel_key(name: str) -> str:
    return name.lower().replace(" ", "")


def channel_matches_preset(channel_name: str, preset_names: list[str]) -> bool:
    """比對實際通道名與預設列表（忽略大小寫與空白）。"""
    if channel_name in preset_names:
        return True
    norm = _normalize_channel_key(channel_name)
    return norm in {_normalize_channel_key(p) for p in preset_names}


class _EventChannelTable(QTableWidget):
    """事件篩選表：點擊名稱文字區域也可切換勾選（Qt 預設僅 checkbox 小方塊可點）。"""

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            item = self.itemAt(event.pos())
            if (
                item is not None
                and item.column() == 0
                and item.flags() & Qt.ItemFlag.ItemIsUserCheckable
            ):
                opt = QStyleOptionViewItem()
                opt.rect = self.visualItemRect(item)
                opt.features = QStyleOptionViewItem.ViewItemFeature.HasCheckIndicator
                opt.checkState = item.checkState()
                check_rect = self.style().subElementRect(
                    QStyle.SubElement.SE_ItemViewItemCheckIndicator,
                    opt,
                    self,
                )
                if not check_rect.contains(event.pos()):
                    item.setCheckState(
                        Qt.CheckState.Unchecked
                        if item.checkState() == Qt.CheckState.Checked
                        else Qt.CheckState.Checked
                    )
        super().mousePressEvent(event)


class _NumericCountItem(QTableWidgetItem):
    """專用於事件篩選表格「數量」欄的 item，讓排序使用數值（UserRole）而非字串詞法比較。
    解決「1, 10, 1013, 118」這種低級字串排序問題，支援真正多到少 / 少到多。
    """
    def __lt__(self, other: QTableWidgetItem) -> bool:
        if isinstance(other, QTableWidgetItem):
            my_val = self.data(Qt.ItemDataRole.UserRole)
            other_val = other.data(Qt.ItemDataRole.UserRole)
            if my_val is not None and other_val is not None:
                try:
                    return int(my_val) < int(other_val)
                except (ValueError, TypeError):
                    pass
        return super().__lt__(other)


class _EventTableModel(QAbstractTableModel):
    """事件表的虛擬化資料模型（QTableView 用）。
    只保存純資料 list，畫面只渲染可見列 → 規模無關、上萬列瞬開、不需分批/不凍結。
    每列預先算好顯示字串、排序鍵、背景色與 tooltip（_build_event_row_dict 產生）。
    時間欄排序鍵用 rel_s（相對全域原點秒數，跨午夜單調）而非顯示字串，升/降序皆正確。
    """
    COLUMNS = ["跳轉", "開始時間", "結束時間", "持續時間", "名稱", "備註"]
    SORT_ROLE = Qt.ItemDataRole.UserRole + 1
    RIGHT_COLS = {1, 2, 3}

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: list = []      # 每筆: {disp:[6], sort:[6], rel_start, bg:QColor|None, tip:str}
        self._message = None       # 佔位訊息（未載入/無資料時顯示單列）

    def set_rows(self, rows):
        self.beginResetModel()
        self._rows = rows
        self._message = None
        self.endResetModel()

    def set_message(self, msg):
        self.beginResetModel()
        self._rows = []
        self._message = msg
        self.endResetModel()

    def rel_start_at(self, source_row):
        if 0 <= source_row < len(self._rows):
            return self._rows[source_row].get('rel_start')
        return None

    def rowCount(self, parent=QModelIndex()):
        if parent.isValid():
            return 0
        if self._message is not None:
            return 1
        return len(self._rows)

    def columnCount(self, parent=QModelIndex()):
        if parent.isValid():
            return 0
        return len(self.COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            if 0 <= section < len(self.COLUMNS):
                return self.COLUMNS[section]
            return None
        # 垂直表頭：1-based 列號（與舊 QTableWidget 一致，跟隨排序後的視覺順序）
        return section + 1

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        r, c = index.row(), index.column()
        if self._message is not None:
            if role == Qt.ItemDataRole.DisplayRole and c == 0:
                return self._message
            return None
        if r < 0 or r >= len(self._rows):
            return None
        row = self._rows[r]
        if role == Qt.ItemDataRole.DisplayRole:
            return row['disp'][c]
        if role == self.SORT_ROLE:
            return row['sort'][c]
        if role == Qt.ItemDataRole.ToolTipRole:
            return row['tip']
        if role == Qt.ItemDataRole.BackgroundRole:
            return row['bg']
        if role == Qt.ItemDataRole.TextAlignmentRole and c in self.RIGHT_COLS:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return None


class TimeAxisItem(pg.AxisItem):
    """自定義 X 軸，保持預設 tick 間隔，只改變標籤顯示格式 (時分秒/秒數 + 是否毫秒)。"""
    def __init__(self, viewer, *args, **kwargs):
        pg.AxisItem.__init__(self, *args, **kwargs)
        self.viewer = viewer

    def _origin(self):
        """取得目前錄音的全域時間原點（start_datetime）；無則 None。"""
        if not getattr(self.viewer, 'x_axis_absolute_time', False):
            return None
        rec = getattr(self.viewer, 'current_rec', None)
        return getattr(rec, 'start_datetime', None) if rec is not None else None

    def tickStrings(self, values, scale, spacing):
        if not values:
            return []
        include_ms = self.viewer.x_axis_show_milliseconds
        origin = self._origin()
        strings = []
        for v in values:
            if self.viewer.x_axis_time_format == 'seconds':
                # 秒數模式：絕對時鐘無意義，恆顯示自原點起的相對秒
                if include_ms:
                    s = f"{v:.3f}"
                else:
                    s = f"{v:.1f}" if spacing < 2 else f"{int(round(v))}"
            elif origin is not None:
                # 絕對牆鐘時間 = origin + 相對秒（v 為相對全域原點的秒數）。跨午夜由 datetime 自然進位。
                t = origin + timedelta(seconds=float(v))
                s = t.strftime('%H:%M:%S')
                if include_ms:
                    s += f".{int(t.microsecond / 1000):03d}"
            else:
                s = format_hms(v, include_ms=include_ms)
            strings.append(s)
        return strings


# ==================== PR3: PatientInfoBanner + ClinicalSummary mini widgets (smallest, inside main file) ====================
class PatientInfoBanner(QWidget):
    """PR3: 頂部資料來源臨床資訊 Banner。使用 QGrid + 色塊 accents + tooltips。
    從 rec.get_patient_info() 填入，處理 fallback。Prominent 顯示 demographics + key clinical。
    保持簡潔高度，可見；次要技術資訊併入。
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("PatientInfoBanner")
        self.setStyleSheet(
            "QWidget#PatientInfoBanner { background: #f8f9fa; border: 1px solid #dee2e6; border-radius: 4px; }"
            "QLabel.prominent { font-size: 13pt; font-weight: bold; color: #212529; }"
            "QLabel.accent { color: #c0392b; font-weight: bold; }"  # high risk red-ish (Supine RDI risk accent, keep current per design Open Q3)
            "QLabel.warning { color: #d35400; font-weight: bold; }"
            "QLabel { font-family: 'Microsoft JhengHei', 'Segoe UI', sans-serif; }"  # PR5: medical readable light accent
        )
        self.setMaximumHeight(125)

        outer = QHBoxLayout(self)
        outer.setContentsMargins(6, 4, 6, 4)
        outer.setSpacing(8)

        frame = QFrame()
        frame.setObjectName("bannerFrame")
        grid = QGridLayout(frame)
        grid.setContentsMargins(4, 2, 4, 2)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(2)

        # Row 0: prominent ID + Age/Gender 
        self.lbl_id = QLabel()
        self.lbl_id.setProperty("class", "prominent")
        grid.addWidget(self.lbl_id, 0, 0)

        self.lbl_age_gender = QLabel()
        self.lbl_age_gender.setProperty("class", "prominent")
        grid.addWidget(self.lbl_age_gender, 0, 1)

        # Row 1: Study date/dur + device (secondary)
        self.lbl_study = QLabel()
        grid.addWidget(self.lbl_study, 1, 0, 1, 2)

        self.lbl_device = QLabel()
        grid.addWidget(self.lbl_device, 1, 2)

        outer.addWidget(frame, 1)

        # Tooltips (clinical meaning per design) + PR5 更多臨床 tooltips
        self.lbl_id.setToolTip("資料來源識別碼")
        self.lbl_age_gender.setToolTip("年齡與性別")
        self.lbl_study.setToolTip("研究日期與睡眠摘要")
        self.lbl_device.setToolTip("裝置資訊")

    def set_patient_info(self, demo: dict):
        """填入 demographics，處理 mask/fallback。版面與有報告時相同兩行結構，提示文字不擠壓主欄。"""
        mrn = str(demo.get("mrn", "—"))
        if " " in mrn and len(mrn) > 24:
            mrn = mrn.split()[0]

        age = demo.get("age")
        gender = demo.get("gender")
        age_disp = age if age not in (None, "", "?") else "—"
        gender_disp = gender if gender not in (None, "", "?") else "—"
        h = demo.get("height_cm")
        w = demo.get("weight_kg")
        date = demo.get("study_date", "")

        self.lbl_id.setText(f"MR#: {mrn}")
        self.lbl_age_gender.setText(f"年齡: {age_disp}  性別: {gender_disp}")

        study_parts = []
        if date:
            study_parts.append(f"日期: {date}")
        if h and w:
            study_parts.append(f"H/W: {h:.0f}cm/{w:.1f}kg")
        self.lbl_study.setText("  ".join(study_parts))

        note = demo.get("note", "")
        if note:
            self.lbl_device.setText(note)
            self.lbl_device.setStyleSheet("color:#666; font-size:9pt;")
        else:
            self.lbl_device.setText("")
            self.lbl_device.setStyleSheet("")

    def set_from_rec(self, rec):
        """便利：直接從 recording 取 get_patient_info() 填入。"""
        if not rec:
            return
        info = rec.get_patient_info()
        demo = info.get("demographics", {})
        self.set_patient_info(demo)
        # 臨床完整資訊改由中間按鈕的獨立彈窗顯示，banner 不再顯示 RDI/BMI 等（已簡化）


# ==================== PR4: PrefsManager (QSettings 持久化，global + per-rec overrides) ====================
class PrefsManager:
    """PR4: 使用 QSettings 存取使用者偏好（跨平台、registry/ini 使用者隔離）。
    支援 global defaults + per-rec overrides（以 rec.name 作為 key）。
    複雜結構（visible list、time、viz styles thresholds/labels 等）用 json 序列化。
    初次無 prefs 則回退 clinical defaults（硬編碼 DeepNYX 可見通道 + 每通道高度 200px + position step_fill 由 VizSettings 提供）。
    關鍵儲存點：channel check、time controls、closeEvent、patient switch。（已取消 style/scale UI）
    """
    def __init__(self):
        # 預設使用 QSettings("org", "app") → Win registry (HKEY_CURRENT_USER\\Software\\...)，跨平台。
        # 清除用 settings.clear() 即可（測試/rollback 容易）；也可透過 regedit 或對應 ini 清除。
        self.settings = QSettings("PSG", "PSGviewer")

    def _sync(self):
        self.settings.sync()

    # 已取消全域振幅縮放（固定使用 1.0）

    def save_visible(self, rec_name: str, channels: list[str]):
        if not rec_name:
            return
        import json
        self.settings.setValue(f"patient/{rec_name}/visible", json.dumps(list(channels)))
        self._sync()

    def load_visible(self, rec_name: str, default: list[str] | None = None) -> list[str] | None:
        if not rec_name:
            return default
        import json
        key = f"patient/{rec_name}/visible"
        if self.settings.contains(key):
            try:
                val = self.settings.value(key)
                return json.loads(val) if isinstance(val, str) else default
            except Exception:
                pass
        return default

    def save_time_window(self, rec_name: str, start: float, duration: float):
        if not rec_name:
            return
        import json
        self.settings.setValue(f"patient/{rec_name}/time", json.dumps({"start": float(start), "duration": float(duration)}))
        self._sync()

    def load_time_window(self, rec_name: str, max_dur: float = 0.0) -> tuple[float, float]:
        if not rec_name:
            return 0.0, min(60.0, max_dur or 60.0)
        import json
        key = f"patient/{rec_name}/time"
        if self.settings.contains(key):
            try:
                d = json.loads(self.settings.value(key))
                s = float(d.get("start", 0.0))
                dur = float(d.get("duration", 60.0))
                return max(0.0, s), max(1.0, dur)
            except Exception:
                pass
        return 0.0, min(60.0, max_dur or 60.0)

    def save_viz_settings(self, rec_name: str, viz: "VizSettings"):
        if not rec_name or viz is None:
            return
        import json
        # 已取消樣式編輯功能，僅保存 render_modes 等位置相關（若有）
        data = {
            "render_modes": getattr(viz, "render_modes", {}),
            "pos_thresholds": getattr(viz, "pos_thresholds", {}),
            "pos_labels": getattr(viz, "pos_labels", {}),
            "pos_colors": getattr(viz, "pos_colors", {}),
        }
        self.settings.setValue(f"patient/{rec_name}/viz", json.dumps(data))
        self._sync()

    def load_viz_settings(self, rec_name: str, viz: "VizSettings"):
        """Restore into existing viz object (per-rec override)。若無 key 則不動，保留 VizSettings/renderer 內建 clinical defaults (step_fill)。"""
        if not rec_name or viz is None:
            return
        import json
        key = f"patient/{rec_name}/viz"
        if not self.settings.contains(key):
            return
        try:
            raw = self.settings.value(key)
            data = json.loads(raw) if isinstance(raw, str) else {}
            if isinstance(data, dict):
                for k in ("render_modes", "pos_thresholds", "pos_labels", "pos_colors"):
                    if k in data and hasattr(viz, k):
                        d = getattr(viz, k)
                        d.clear()
                        d.update(data[k] or {})
        except Exception:
            pass

    def clear_all(self):
        self.settings.clear()
        self._sync()

    def has_any_prefs(self) -> bool:
        keys = self.settings.allKeys()
        return any("patient/" in str(k) for k in keys)

    # 已取消樣式編輯功能，移除 general_style load/save（使用預設樣式）

    def save_channel_height(self, val: int):
        self.settings.setValue("global/channel_height", int(val))
        self._sync()

    def load_channel_height(self, default: int = 200) -> int:
        v = self.settings.value("global/channel_height", default)
        try:
            return int(v)
        except Exception:
            return default

    def save_show_events(self, val: bool):
        self.settings.setValue("global/show_events", bool(val))
        self._sync()

    def load_show_events(self, default: bool = False) -> bool:
        v = self.settings.value("global/show_events", default)
        if isinstance(v, str):
            return v.lower() in ("true", "1", "yes")
        try:
            return bool(v)
        except Exception:
            return default


# 保留此 key 僅為相容舊狀態清除（實際已不再使用，無法匹配 event 由獨立的「在所有通道顯示」checkbox 控制，不再 group 在 channel 列表中）
UNMATCHED_SPECIAL = "__unmatched__"


class PSGViewer(QMainWindow):
    """主視窗：結構化展示 + 專業互動式信號瀏覽器"""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("PSG Viewer - 專業 PSG 臨床資料瀏覽器 (基於優化 raw .ndf 讀取)")
        self.resize(1400, 900)
        # 設定合理的視窗最小尺寸，避免 MINMAXINFO 報過大的 mintrack（1393x 之類），導致移到較小解析度螢幕或多螢幕時 setGeometry 報 Unable to set geometry。
        # 使用者仍可拖曳 dock 邊界自由調整左右欄寬度（本程式已提供完整支援）。
        self.setMinimumSize(820, 480)

        self.study: NoxStudy | None = None
        self.current_rec: NoxRecording | None = None
        self.visible_channels: list[str] = []
        self.time_start: float = 0.0
        self.time_duration: float = 60.0   # 目前視窗秒數（預設啟動僅載入 +60s 範圍，保證開啟大型錄音快速）
        self.max_duration: float = 0.0

        # 效能快取：供 channel 切換時的差異載入（snapshot 當前顯示資料，避免重複 get_data）
        self._channel_data_cache: dict = {}

        # pyqtgraph 物件
        self.overview_pg: pg.GraphicsLayoutWidget | None = None
        self.signals_pg: pg.GraphicsLayoutWidget | None = None
        self.plot_items: list[pg.PlotItem] = []
        self.curves: list[pg.PlotDataItem] = []
        self.event_lines: list[pg.InfiniteLine] = []  # legacy (placeholder center)，改用 parsed event markers 支援 by-channel
        self.event_marker_items: list = []
        self.overview_plot: pg.PlotItem | None = None
        self.plot_red_lines: list = []  # 各詳細 plot 上的細紅虛線 (與 overview 紅線同步位置，供波形對照)
        # 舊的 self.pg_layout 已拆分為 overview_pg (固定) + signals_pg (可捲動)

        self._updating_view = False   # 避免回呼迴圈
        self._setting_overview_x = False  # 鎖定概觀軸 x 範圍時防迴圈

        # 效能：對高頻 range change (wheel/drag) 做 debounce，避免每次微小移動都全量 _update (50個channel重讀+重繪)
        self._update_debounce_timer = QTimer(self)
        self._update_debounce_timer.setSingleShot(True)
        self._update_debounce_timer.timeout.connect(self._perform_update_view)

        self.parsed_events: list[dict] = []
        self._events_loaded: bool = False  # 預設 False：只有使用者主動「載入事件資料」後才解讀所有標記，勾選時才繪製
        self._custom_xls_path: Optional[str] = None  # 用於「替換XLS/XLSX」功能，替換後的 Excel 分析事件路徑（.xls 或 .xlsx）
        self.desired_channel_height: int = 200
        self.selected_event_channels: set = set()  # 使用者選取要顯示事件的 channel，多選
        self.event_overlay_regions: list = []

        # 例外匹配列表：NDF 事件通道名(無法標準匹配時) -> 目標 raw 通道名，保存在專案根 channel_matching.md
        # 匹配不區分大小寫，載入/套用時自動 trim；用於 enrich display、matches 判斷、刷新 list/markers
        self._exception_map: list | None = None

        # 事件蒙層與 X軸顯示偏好（檢視選單控制）
        self.show_event_background_overlay: bool = False  # 預設不顯示背景黃色蒙層
        self.show_event_text_labels: bool = False  # 預設不顯示事件 marker 上的小文字標籤（type @ location）
        self.x_axis_time_format: str = 'hms'  # 'hms' 或 'seconds'，預設時分秒
        self.x_axis_show_milliseconds: bool = False  # 預設不顯示毫秒
        self.x_axis_absolute_time: bool = True  # 預設顯示絕對牆鐘時間（origin + 相對秒）；關閉則顯示自 0 起的相對經過時間
        self.antialias_on: bool = True  # 抗鋸齒（檢視選單可切換，預設開）；關閉可在密集波形大幅加速繪製

        # PR2: VizSettings (memory container) + position special renderers；PR4 由 PrefsManager 持久化（per-rec）
        self._viz_settings = VizSettings()
        self.position_renderers: dict = {}

        # PR3: banner + clinical summary (set in _setup_ui)
        self.patient_banner: PatientInfoBanner | None = None

        # PR4: PrefsManager (QSettings persistence; restore happens in _load_recording)
        self.prefs: PrefsManager | None = PrefsManager()
        self._loading_rec = False  # 載入期間避免儲存預設值覆寫 prefs

        # 初始從 prefs 載入全域高度 (issue2) 與事件蒙層顯示 (新功能)，UI 建立後會再同步
        if self.prefs:
            self.desired_channel_height = self.prefs.load_channel_height(200)

        self.right_events_splitter: QSplitter | None = None
        self.main_splitter: QSplitter | None = None  # 主水平 3 欄 splitter，提供滑順寬度拖曳的核心

        self._setup_ui()
        if hasattr(self, 'btn_load_events') and self.btn_load_events:
            self.btn_load_events.setVisible(True)
        if hasattr(self, 'instruction_label') and self.instruction_label:
            self.instruction_label.setVisible(False)
        if hasattr(self, 'btn_replace_excel') and self.btn_replace_excel:
            self.btn_replace_excel.setEnabled(False)
            self.btn_replace_excel.setStyleSheet("")  # 確保正常樣式，非灰色
        self._create_actions()
        self._create_menus()
        self._create_toolbar()
        self._connect_shortcuts()

        # 確保初始佈局（延遲執行）。
        # 現在使用 main_splitter (水平 3 欄) 取代舊 dock，拖曳寬度原生滑順、無卡頓、無 geometry 衝突。
        QTimer.singleShot(0, self._reset_panel_layout)
        # 初始也套用事件 splitter 的 2:1 預設（還原時會再保險套用一次）
        QTimer.singleShot(80, self._apply_default_event_splitter_ratio)

        # 已無 F2/F3 toggle 按鈕，改用「還原初始面板」按鈕恢復佈局

        self.statusBar().showMessage("請選擇資料夾載入 PSG 研究資料")

    def _setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        central.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(4, 4, 4, 4)

        # 頂部研究概覽區 (PR3: 以 PatientInfoBanner 取代 info_text 部分；quick buttons 維持右側可見)
        # 左右側面板改用主水平 QSplitter 管理寬度（見後續 main_splitter 建立），
        # 提供滑順拖曳、無需複雜 dock 還原邏輯。
        overview_group = QGroupBox("研究概覽")
        overview_layout = QHBoxLayout(overview_group)

        # PR3: banner 取代原 info_text (prominent grid + accents + tooltips + clinical)
        self.patient_banner = PatientInfoBanner()
        overview_layout.addWidget(self.patient_banner, 3)

        # 快速操作區 (維持不變，確保所有舊流程如 資料來源 switch / export 正常)
        quick_box = QWidget()
        quick_layout = QVBoxLayout(quick_box)
        self.patient_combo = QComboBox()
        self.patient_combo.currentIndexChanged.connect(self._on_patient_changed)
        self.patient_combo.setToolTip("資料來源（資料夾名稱，優先使用 raw data 子資料夾的完整名稱如 D18_... raw data_...），切換會重新載入對應錄音")
        quick_layout.addWidget(QLabel("資料來源:"))
        quick_layout.addWidget(self.patient_combo)

        self.btn_refresh = QPushButton("重新載入")
        self.btn_refresh.clicked.connect(self._reload_current)
        quick_layout.addWidget(self.btn_refresh)

        quick_layout.addStretch()
        overview_layout.addWidget(quick_box, 1)

        # 研究概覽 稍後會包在中間欄（middle_widget），只顯示在中央波形區上方，不橫跨左右面板。
        # === 左側控制面板內容 (時間視窗 + 通道表格) ===
        # 注意：稍後與中央、右側一起加入 main_splitter (QSplitter Horizontal)
        # 取代舊 QDockWidget 方式，解決寫死 min / 複雜還原 / 拖曳卡頓 / 裁切 / geometry 錯誤。
        # QSplitter 提供原生平滑拖曳分隔線，無 WM 干擾，體驗流暢。
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)

        # 通道選擇
        ch_group = QGroupBox("通道")
        ch_layout = QVBoxLayout(ch_group)

        self.channel_table = QTableWidget()
        # 取消樣式欄位，讓控制面板更窄（移除第 6 欄 "樣式" 按鈕）
        self.channel_table.setColumnCount(6)
        self.channel_table.setHorizontalHeaderLabels(["顯示", "名稱", "fs (Hz)", "單位", "樣本數", "來源"])
        self.channel_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.channel_table.itemChanged.connect(self._on_channel_check_changed)
        self.channel_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        # 窄面板時啟用水平捲軸，避免裁切；使用者可拖曳欄寬或靠搜尋過濾
        self.channel_table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        # 支援點擊表頭切換升冪/降冪排序（名稱、fs、樣本數等）
        # 使用 setSortIndicatorShown + 小箭頭，不新增任何寬度元素，符合「不要造成版面太多變寬」
        header = self.channel_table.horizontalHeader()
        header.setSectionsClickable(True)
        header.setSortIndicatorShown(True)
        header.sectionClicked.connect(self._on_channel_header_clicked)
        self._channel_sort_col = 1  # 名稱欄，預設按名稱順序排列
        self._channel_sort_order = Qt.SortOrder.AscendingOrder
        header.setToolTip("點擊表頭排序通道。排序後波形順序會更新。")

        # 每個欄位都設為 Interactive，讓使用者可以手動拖拉標題分隔線調整寬度
        for i in range(6):
            header.setSectionResizeMode(i, QHeaderView.ResizeMode.Interactive)
        header.setMinimumSectionSize(28)  # 允許欄位縮得更窄，降低表格 sizeHint，避免主視窗 minSize 過大導致小螢幕 geometry 錯誤；使用者仍可手動拖拉調整

        # 通道搜索：即時過濾表格（不影響實際可見通道列表，只隱藏 row）
        self.channel_search = QLineEdit()
        self.channel_search.setPlaceholderText("搜尋通道")
        self.channel_search.textChanged.connect(self._filter_channels)
        ch_layout.addWidget(self.channel_search)
        ch_layout.addWidget(self.channel_table)

        # 預設通道組合：使用 combo 取代多按鈕水平排列，節省寬度避免小螢幕水平擠壓/重疊 (issue 3 UX)
        preset_layout = QHBoxLayout()
        preset_layout.addWidget(QLabel("預設:"))
        self.preset_combo = QComboBox()
        self.preset_items = [
            ("DeepNYX", CLINICAL_DEFAULT_CHANNELS),
            ("標準 PSG", ["c3", "c4", "f3", "f4", "o1", "o2", "e1", "e2", "ecg", "flow", "spo2"]),
            ("呼吸", ["flow", "nasal pressure", "abdomen rip", "thorax rip", "mask pressure", "spo2"]),
            ("Position+Resp", ["position", "flow", "spo2", "heart rate"]),
            ("心肺", ["ecg", "pleth", "spo2", "heart rate", "flow"]),
            ("EEG", ["c3", "c4", "f3", "f4", "o1", "o2", "e1", "e2"]),
            ("全選", None),
            ("清除", []),
        ]
        for label, _ in self.preset_items:
            self.preset_combo.addItem(label)
        preset_layout.addWidget(self.preset_combo, 1)
        btn_apply_preset = QPushButton("套用")
        btn_apply_preset.setToolTip("套用預設通道組合")
        btn_apply_preset.clicked.connect(self._apply_selected_preset)
        preset_layout.addWidget(btn_apply_preset)
        ch_layout.addLayout(preset_layout)

        # 時間視窗控制先加入（使用者要求放到最上面）
        # 通道稍後以 stretch 加入填滿高度
        # （事件篩選已移到右側事件面板）
        time_group = QGroupBox("時間視窗")
        time_group.setToolTip("調整時間起點與長度；點擊 Fit 全長 顯示整個錄音")
        time_layout = QVBoxLayout(time_group)

        # 位置滑桿 (概略)
        pos_layout = QHBoxLayout()
        pos_layout.addWidget(QLabel("位置:"))
        self.pos_slider = QSlider(Qt.Orientation.Horizontal)
        self.pos_slider.setMinimum(0)
        self.pos_slider.setMaximum(1000)
        self.pos_slider.valueChanged.connect(self._on_slider_changed)
        pos_layout.addWidget(self.pos_slider, 1)
        time_layout.addLayout(pos_layout)

        # 絕對時間視窗（顯示目前 x 軸最小/最大牆鐘時間，使用者可自行輸入 hh:mm:ss 調整視窗）
        # 編輯起點 → 平移視窗（保持長度）；編輯終點 → 固定起點、改變視窗長度。
        abs_layout = QHBoxLayout()
        abs_layout.addWidget(QLabel("絕對時間:"))
        self.abs_start_edit = QLineEdit()
        self.abs_start_edit.setPlaceholderText("hh:mm:ss")
        self.abs_start_edit.setMaximumWidth(96)
        self.abs_start_edit.setToolTip("目前視窗起點的絕對牆鐘時間；可輸入 hh:mm:ss 後按 Enter 平移視窗（保持長度）。")
        self.abs_start_edit.editingFinished.connect(self._on_abs_start_edited)
        abs_layout.addWidget(self.abs_start_edit)
        abs_layout.addWidget(QLabel("-"))
        self.abs_end_edit = QLineEdit()
        self.abs_end_edit.setPlaceholderText("hh:mm:ss")
        self.abs_end_edit.setMaximumWidth(96)
        self.abs_end_edit.setToolTip("目前視窗終點的絕對牆鐘時間；可輸入 hh:mm:ss 後按 Enter 調整視窗長度（保持起點）。")
        self.abs_end_edit.editingFinished.connect(self._on_abs_end_edited)
        abs_layout.addWidget(self.abs_end_edit)
        abs_layout.addStretch()
        time_layout.addLayout(abs_layout)

        # 精確控制
        ctrl_layout = QHBoxLayout()
        ctrl_layout.addWidget(QLabel("開始 (秒):"))
        self.start_spin = QDoubleSpinBox()
        self.start_spin.setDecimals(1)
        self.start_spin.setRange(0, 999999)
        self.start_spin.valueChanged.connect(self._on_time_spin_changed)
        ctrl_layout.addWidget(self.start_spin)

        ctrl_layout.addWidget(QLabel("長度 (秒):"))
        self.dur_spin = QDoubleSpinBox()
        self.dur_spin.setDecimals(1)
        self.dur_spin.setRange(0.1, 999999.0)  # 動態上限在 _update_time_controls 依 max_duration 調整
        self.dur_spin.setValue(60.0)
        self.dur_spin.valueChanged.connect(self._on_time_spin_changed)
        ctrl_layout.addWidget(self.dur_spin)

        time_layout.addLayout(ctrl_layout)

        # 每通道高度 + Fit 全長 放在同一列（使用者要求：fit 全長 按鈕放到每通道高度的右邊）
        # 開始/長度已是兩欄水平；此處高度控制右側直接放 fit 按鈕，節省垂直空間
        height_layout = QHBoxLayout()
        height_layout.addWidget(QLabel("每通道高度:"))
        self.height_spin = QSpinBox()
        self.height_spin.setRange(30, 999)  # 支援輸入到三位數，允許較高解析度或極少通道時大高度
        self.height_spin.setValue(self.desired_channel_height)
        self.height_spin.setSuffix(" px")
        self.height_spin.setSingleStep(5)
        self.height_spin.valueChanged.connect(self._on_channel_height_changed)
        height_layout.addWidget(self.height_spin)
        height_layout.addSpacing(8)
        fit_btn = QPushButton("Fit 全長")
        fit_btn.setToolTip("顯示整個錄音長度")
        fit_btn.clicked.connect(self._fit_full)
        height_layout.addWidget(fit_btn)
        height_layout.addStretch()
        time_layout.addLayout(height_layout)

        self.time_label = QLabel("視窗: 00:00 - 00:30   / 總長 00:00")
        time_layout.addWidget(self.time_label)

        # === 事件顯示篩選已移到右側 ===
        # 提示 channel 與數量，使用 QListWidget 帶 checkbox + search + all/clear，最佳 UX
        self.event_filter_group = QGroupBox("事件篩選")
        self.event_filter_group.setToolTip("請選擇要顯示的事件通道（含無法匹配的獨立事件）。點擊欄位標題可排序。")
        ef_lay = QVBoxLayout(self.event_filter_group)
        ef_lay.setContentsMargins(4, 2, 4, 2)
        ef_lay.setSpacing(2)

        self.event_channel_search = QLineEdit()
        self.event_channel_search.setPlaceholderText("搜尋事件通道")
        self.event_channel_search.textChanged.connect(self._filter_event_channels)
        ef_lay.addWidget(self.event_channel_search)

        # 效能優化按鈕：預設不載入事件資料（避免初始載入大型 xls/xlsx 分析事件造成卡頓）
        # 使用者主動點擊後才「解讀所有標記資料」，之後在列表勾選時才繪製表格與波形標記。
        # 如果從未載入或未全選，絕不處理全部資料。
        self.btn_load_events = QPushButton("載入事件資料")
        self.btn_load_events.setToolTip("載入事件資料（nef + xls/xlsx）。載入後才能勾選顯示事件與標記。")
        self.btn_load_events.clicked.connect(self._on_load_events_clicked)

        self.btn_replace_excel = QPushButton("替換Excel")
        self.btn_replace_excel.setToolTip("選擇另一個 Excel 檔案（.xls 或 .xlsx）來替換目前的分析事件標記資料（nef 裝置事件會保留）。")
        self.btn_replace_excel.clicked.connect(self._on_replace_xls_clicked)
        self.btn_replace_excel.setEnabled(False)  # 只有在已載入事件後才啟用
        self.btn_replace_excel.setStyleSheet("")  # 確保正常啟用樣式，非灰色

        self.instruction_label = QLabel("請選擇要顯示的事件通道")
        self.instruction_label.setStyleSheet("color: #333333;")
        self.instruction_label.setVisible(False)

        btns_hbox = QHBoxLayout()
        btns_hbox.addWidget(self.btn_load_events)
        btns_hbox.addWidget(self.instruction_label)
        btns_hbox.addWidget(self.btn_replace_excel)
        ef_lay.addLayout(btns_hbox)

        self.btn_load_events.setVisible(True)
        self.instruction_label.setVisible(False)
        self.btn_replace_excel.setEnabled(False)
        self.btn_replace_excel.setStyleSheet("")

        # 改為 QTableWidget 兩欄「名稱」「數量」，預設名稱順序，header 可點擊排序（升/降）
        self.event_channel_list = _EventChannelTable()
        self.event_channel_list.setColumnCount(2)
        self.event_channel_list.setHorizontalHeaderLabels(["名稱", "數量"])
        self.event_channel_list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.event_channel_list.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        # 每個欄位都設為 Interactive，讓使用者可以手動拖拉標題分隔線調整寬度（名稱 / 數量）
        header = self.event_channel_list.horizontalHeader()
        for i in range(2):
            header.setSectionResizeMode(i, QHeaderView.ResizeMode.Interactive)
        header.setMinimumSectionSize(28)  # 允許欄位縮得更窄，降低表格 sizeHint，避免主視窗 minSize 過大導致小螢幕 geometry 錯誤；使用者仍可手動拖拉調整
        self.event_channel_list.setSortingEnabled(True)
        self.event_channel_list.verticalHeader().setVisible(False)
        self.event_channel_list.itemChanged.connect(self._on_event_channel_toggled)
        self.event_channel_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        ef_lay.addWidget(self.event_channel_list, 1)

        btn_lay = QHBoxLayout()
        self.btn_event_all = QPushButton("全選")
        self.btn_event_all.clicked.connect(self._select_all_event_channels)
        btn_lay.addWidget(self.btn_event_all)
        self.btn_event_clear = QPushButton("清除")
        self.btn_event_clear.clicked.connect(self._clear_event_channels)
        btn_lay.addWidget(self.btn_event_clear)
        ef_lay.addLayout(btn_lay)

        # 另外一個獨立的勾選：「在所有通道顯示」（不是列表中的項目）
        # 需求：勾選後，會將「目前左列表所有已選項」（真實 channel 的 event） 的標記強制顯示在「所有」波形通道上。
        # 無法匹配的獨立 event 本身以 "xxx (無匹配)" 形式出現在左列表，選取它們就會讓它們的標記（淺灰）出現在所有通道，並在右表格以 "xxx(無匹配)" 位置顯示。
        self.chk_display_all_channels = QCheckBox("顯示於所有通道")
        self.chk_display_all_channels.setToolTip("將已選真實通道的事件標記也顯示在所有波形上。")
        self.chk_display_all_channels.stateChanged.connect(lambda: self._refresh_event_displays())

        # 例外匹配列表按鈕：放在「顯示於所有通道」右邊
        self.btn_exception_list = QPushButton("例外匹配列表")
        self.btn_exception_list.setToolTip("設定 NDF 通道無法標準匹配時的例外映射規則（保存到 channel_matching.md）。點擊後立即刷新事件篩選、標記與表格。")
        self.btn_exception_list.clicked.connect(self._on_show_exception_dialog)

        exc_hbox = QHBoxLayout()
        exc_hbox.setContentsMargins(0, 0, 0, 0)
        exc_hbox.setSpacing(4)
        exc_hbox.addWidget(self.chk_display_all_channels)
        exc_hbox.addWidget(self.btn_exception_list)
        exc_hbox.addStretch(1)
        ef_lay.addLayout(exc_hbox)

        # 左側：時間控制在上，通道在下並填滿高度（stretch）
        left_layout.addWidget(time_group)
        left_layout.addWidget(ch_group, 1)  # stretch factor 1 讓通道高度鋪滿剩餘空間

        # left_widget 準備好，稍後加入 main_splitter（左欄）。不再使用 QDockWidget 以避免拖曳寬度卡頓與裁切。
        # === 中央：pyqtgraph 波形區 ===
        # 結構調整：固定概觀軸 (overview) 置頂，永遠可見、不隨詳細波形捲動
        # 下方詳細通道波形區使用獨立 QScrollArea，提供垂直滾動條（選擇太多通道時可下拉查看）
        # 視覺區隔：概觀區固定高度 + 淺色背景 + 分隔線 + tooltip 說明
        pg.setConfigOptions(antialias=True)

        self.central_split = QSplitter(Qt.Orientation.Vertical)

        # --- 固定概觀軸區 (不捲動，獨立於下方詳細波形) ---
        # UX 針對需求重新設計：單一時間軸（從 0 到 max_duration 完整刻度樣式，根據秒顯示刻度與標籤）。
        # 淡藍色可拖動區域（LinearRegionItem）與紅色位置指示必須有足夠垂直空間（至少 10px 高度的視覺面積），
        # 讓窄視窗（長錄音下 60s 預設比例仍極小）時仍清楚可見、可拖曳整個區域或邊緣縮放，即時同步下方所有詳細波形。
        # 移除上方文字標題以釋放所有垂直像素給軸與蒙層本身；靠 plot tooltip + 藍色粗框區域本身說明操作。
        overview_container = QWidget()
        overview_container.setFixedHeight(85)  # 增加少許總高：小標題11px + ruler ~66px (底部刻度14px 後仍有足夠給淡藍蒙層 >10px 實質高度) + 邊界，紅線也有空間；固定高度避免 ticks 造成變化
        overview_container.setStyleSheet("""
            background-color: #f0f4f8; 
            border-bottom: 1px solid #b8d4e0;  /* 改淺色細底線，減少「不需要的底線」感覺，保留與下方波形的視覺分隔 */
            border-radius: 0;
        """)
        overview_layout = QVBoxLayout(overview_container)
        overview_layout.setContentsMargins(3, 2, 3, 2)
        overview_layout.setSpacing(1)

        # 恢復緊湊名稱標題（7pt 小字 + 固定小高度），避免完全消失；同時保持足夠空間給下方 ruler 與淡藍區域
        overview_header = QLabel("概觀軸")
        overview_header.setStyleSheet("font-size: 7pt; color: #2c5f7c; font-weight: 500; padding:0; margin:0; border:0;")
        overview_header.setFixedHeight(11)
        overview_header.setToolTip("拖曳淡藍色蒙層區域（或左右邊緣）調整/縮放下方時間視窗；紅線為視窗中心。目前僅載入開頭60s以快速啟動。")
        overview_layout.addWidget(overview_header)

        self.overview_pg = pg.GraphicsLayoutWidget()
        self.overview_pg.setBackground("#fafdfe")
        self.overview_pg.setMaximumHeight(66)
        self.overview_pg.setMinimumHeight(48)

        # 使用自定義 TimeAxisItem，只改標籤格式，保持預設 tick 間隔
        try:
            self.overview_time_axis = TimeAxisItem(self, orientation='bottom')
            self.overview_time_axis.setStyle(tickFont=pg.QtGui.QFont("Microsoft JhengHei", 6), tickLength=2, tickTextOffset=1)
            self.overview_plot = self.overview_pg.addPlot(row=0, col=0, axisItems={'bottom': self.overview_time_axis})
        except Exception:
            self.overview_plot = self.overview_pg.addPlot(row=0, col=0)
        self.overview_plot.setMaximumHeight(66)
        self.overview_plot.setLabel("left", "", units="")  # 極簡左側，只留時間軸刻度
        self.overview_plot.getAxis('left').setWidth(20)  # 更小左軸，最大化刻度顯示寬度
        try:
            self.overview_plot.getAxis('bottom').setHeight(14)  # 固定刻度軸高度，防止 label/tick 出現時造成整體高度變化
        except Exception:
            pass
        self.overview_plot.setToolTip(
            "淡藍色區域 = 目前下方顯示的時間範圍（拖曳整塊平移、拖左右邊緣縮放）。紅線為視窗中心位置。\n概觀軸永遠顯示 0~總長全寬，獨立不與下方同步。"
        )
        self.overview_plot.showGrid(x=True, y=False)
        # 淡藍色半透明可拖動蒙層 + 紅線指示，現在有足夠像素高度（>10px）確保即使極窄時仍醒目可操作
        self.overview_region = pg.LinearRegionItem([0, 60], movable=True,
            brush=pg.mkBrush(135, 206, 250, 155),   # 淡藍色，alpha 調整為清晰但不完全蓋住刻度
            pen=pg.mkPen(70, 130, 180, 230, width=4))  # 更粗邊框 (4px)，窄區間時形成明顯可抓的藍色指示條
        self.overview_region.sigRegionChanged.connect(self._on_overview_region_changed)
        self.overview_plot.addItem(self.overview_region)
        self.overview_region.setZValue(100)
        self.overview_vline = pg.InfiniteLine(pos=0, angle=90, pen=pg.mkPen("#d62728", width=3))
        self.overview_plot.addItem(self.overview_vline)
        self.overview_vline.setZValue(101)
        self.overview_plot.setMenuEnabled(False)

        # 鎖定全寬 x，y 範圍讓 region 填滿可用的垂直高度（確保 ≥10px 視覺空間）
        self.overview_plot.getViewBox().sigRangeChanged.connect(self._force_overview_xrange)
        self.overview_plot.setYRange(0, 1, padding=0)

        if self.overview_region:
            self._updating_view = True
            try:
                self.overview_region.setRegion([0, 60])
                self.overview_region.setZValue(100)
                self.overview_region.setVisible(True)
            finally:
                self._updating_view = False
        self._update_overview_region()

        # 在現有 overview_pg / plot 內部添加結束時間 TextItem（相同 container，不額外佔位空間）
        # 移到右上角，z最高顯示在最前面 (蓋過藍色 region 和紅線)
        self.overview_end_time_label = pg.TextItem(
            text="MAX 00:00:00.000",
            color='#333333',
            anchor=(1, 0)  # top-right of the text box at the position
        )
        self.overview_end_time_label.setFont(pg.QtGui.QFont("Microsoft JhengHei", 7))
        self.overview_end_time_label.setZValue(1000)
        self.overview_plot.addItem(self.overview_end_time_label)

        # 初始文字與位置（load 後會更新到正確 end time，靠右上 + 留空）
        self.overview_end_time_label.setText("MAX 00:00:00.000")
        self.overview_end_time_label.setPos(0, 0.95)

        overview_layout.addWidget(self.overview_pg)
        self.central_split.addWidget(overview_container)

        # --- 詳細波形區 (可垂直捲動) ---
        self.signals_pg = pg.GraphicsLayoutWidget()
        self.signals_pg.setBackground("w")
        # 專業間距：頂部留白，避免與上方固定概觀區的分隔線太貼近，防止元素重疊
        self.signals_pg.setContentsMargins(0, 6, 0, 0)

        # 主要信號區 (動態加入多個 PlotItem)
        self.signal_container = self.signals_pg.addLayout(row=0, col=0)

        self.signal_scroll = QScrollArea()
        self.signal_scroll.setWidgetResizable(True)
        self.signal_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.signal_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.signal_scroll.setWidget(self.signals_pg)
        self.signal_scroll.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        # 動態最小高度確保選擇大量通道時 scrollbar 出現
        # (在 _init_signal_plots 結束時會根據實際 plot 數更新)
        self.signal_scroll.setMinimumHeight(180)

        # 額外：確保 overview 與 signals 之間有視覺呼吸空間（已由容器 border 處理）

        self.central_split.addWidget(self.signal_scroll)

        # 移除固定底部 hint (issue3 小螢幕優化)：原 hint 會強佔垂直空間導致波形區塊被擠壓或重疊。
        # 提示已移至工具列 tooltips、狀態列初始訊息、右鍵選單、及 F1 等隱含說明。節省空間給波形本身。
        # 比例：概觀小而固定、signals 彈性大空間
        self.central_split.setStretchFactor(0, 0)
        self.central_split.setStretchFactor(1, 1)
        self.central_split.setSizes([85, 800])  # 配合新的概觀軸容器固定高度（小標題 + ruler 給淡藍/紅 ≥10px+ 空間，ticks 固定高度不抖動）

        # (central_split 稍後由 main_splitter 加入，舊的 local splitter 已移除)

        # === 右側：事件面板 ===
        # 事件顯示篩選 移到最上方
        # 使用垂直 QSplitter，讓上方篩選區 和 下方「事件/標記」區 可以拖拉分隔線調整相對高度

        # events_content 包含原本的 label + table + note
        events_content_widget = QWidget()
        events_content_layout = QVBoxLayout(events_content_widget)

        self.events_label = QLabel("事件標記")
        events_content_layout.addWidget(self.events_label)

        # 真虛擬化：QTableView + 資料模型 + 排序 proxy。只渲染可見列，上萬列瞬開不凍結。
        self.events_model = _EventTableModel(self)
        self.events_proxy = QSortFilterProxyModel(self)
        self.events_proxy.setSortRole(_EventTableModel.SORT_ROLE)
        self.events_proxy.setSourceModel(self.events_model)
        self.events_table = QTableView()
        self.events_table.setModel(self.events_proxy)
        # 每個欄位 Interactive 可手動拖拉寬度（跳轉/開始/結束/持續/名稱/備註）
        header = self.events_table.horizontalHeader()
        for i in range(6):
            header.setSectionResizeMode(i, QHeaderView.ResizeMode.Interactive)
        header.setMinimumSectionSize(32)  # 允許欄位縮得更窄，降低 sizeHint，避免小螢幕 geometry 問題
        header.setResizeContentsPrecision(50)  # 限制 resizeToContents 掃描列數，保住虛擬化效能
        self.events_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.events_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.events_table.setWordWrap(False)
        self.events_table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.events_table.setSortingEnabled(True)
        # 雙擊任一列跳轉（取代舊的每列「→」按鈕；按鈕在虛擬化下不可行且耗資源）
        self.events_table.doubleClicked.connect(self._on_event_row_activated)
        events_content_layout.addWidget(self.events_table, 1)

        self.events_note_label = QLabel("")
        self.events_note_label.setStyleSheet("color: #666666; font-size: 8pt;")
        events_content_layout.addWidget(self.events_note_label)

        # 移除 nef 裝置通知 placeholder（使用者要求整個區域取消，不需要佔位提示）

        # 垂直 splitter：上方 = 事件顯示篩選 (已在左區塊定義)，下方 = events_content
        # 使用者要求：預設高度比例約 2:1 （上方篩選 : 下方事件標記）
        # 這樣上方多選 channel 列表有足夠空間，下方事件表格仍可見。
        self.right_events_splitter = QSplitter(Qt.Orientation.Vertical)
        self.right_events_splitter.addWidget(self.event_filter_group)
        self.right_events_splitter.addWidget(events_content_widget)
        self.right_events_splitter.setStretchFactor(0, 2)
        self.right_events_splitter.setStretchFactor(1, 1)
        self.right_events_splitter.setSizes([280, 140])  # 2:1 初始比例
        # 防止篩選區完全收合（使用者仍可拖拉調整）
        self.right_events_splitter.setCollapsible(0, False)

        # === 建立主水平 QSplitter (左控制面板 | 中央波形區 | 右事件面板) ===
        # 這是關鍵重構：取代 QDockWidget + resizeDocks + 大量 timer/force/restore 邏輯。
        # QSplitter 的分隔線拖曳是原生、即時、滑順的，不會有 minSizeHint 打架、Windows WM setGeometry 警告、
        # 浮動融合、內容消失、卡頓或邊緣裁切。左右寬度可無限接近內容最小值自由調整。
        # 舊 dock 相關的 _initial_dock_state、installEventFilter、titleBar 過濾、topLevelChanged 全部移除。
        self.main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.main_splitter.addWidget(left_widget)

        # 中間欄：研究概覽 只放在這裡（只有在中間，不橫跨左右面板）
        middle_widget = QWidget()
        middle_layout = QVBoxLayout(middle_widget)
        middle_layout.setContentsMargins(0, 0, 0, 0)
        middle_layout.setSpacing(0)
        middle_layout.addWidget(overview_group)  # 恢復「只有在中間」
        middle_layout.addWidget(self.central_split, 1)
        self.main_splitter.addWidget(middle_widget)

        self.main_splitter.addWidget(self.right_events_splitter)

        self.main_splitter.setStretchFactor(0, 0)  # 左欄偏好固定寬
        self.main_splitter.setStretchFactor(1, 1)  # 中央（含概覽）盡量大
        self.main_splitter.setStretchFactor(2, 0)  # 右欄偏好固定寬

        self.main_splitter.setCollapsible(0, True)  # 允許完全收合左欄（小螢幕友好）
        self.main_splitter.setCollapsible(2, True)  # 允許完全收合右欄
        self.main_splitter.setHandleWidth(6)  # 較粗 handle 容易拖曳，視覺清楚

        # 設定低最小寬，讓拖曳時不會因內容 hint 卡住或 stutter
        # 使用者覺得預設窄，可拖到此 min 再停（或用 reset 回較寬 base）
        left_widget.setMinimumWidth(180)
        self.right_events_splitter.setMinimumWidth(170)
        # 中央至少留一些給波形
        self.central_split.setMinimumWidth(300)

        # 初始寬度（適應螢幕）
        try:
            lw, rw = self._get_side_panel_widths()
            total_w = max(900, self.width() or 1280)
            central_w = max(300, total_w - lw - rw)
            self.main_splitter.setSizes([lw, central_w, rw])
        except Exception:
            self.main_splitter.setSizes([325, 600, 275])  # 預設左 325、右 275（使用者指定）

        main_layout.addWidget(self.main_splitter, 1)

        # 拖動時顯示寬度 tag (px)
        self.main_splitter.splitterMoved.connect(self._on_main_splitter_moved)

        # 狀態列
        self.setStatusBar(QStatusBar())
        self.progress = QProgressBar()
        self.progress.setMaximumWidth(200)
        self.progress.setVisible(False)
        self.statusBar().addPermanentWidget(self.progress)

    def _create_actions(self):
        self.act_open = QAction("開啟資料夾...", self)
        self.act_open.setShortcut(QKeySequence.StandardKey.Open)
        self.act_open.triggered.connect(self._open_folder_dialog)

        self.act_open_edf = QAction("開啟 EDF 檔案...", self)
        self.act_open_edf.setToolTip(
            "直接開啟 Nox 儀器匯出的 .edf（含非標準 Nox 專有格式，無需 Noxturnal 授權）"
        )
        self.act_open_edf.triggered.connect(self._open_edf_dialog)

        self.act_export = QAction("匯出 EDF...", self)
        self.act_export.triggered.connect(self._export_selected_edf)

        self.act_quit = QAction("離開", self)
        self.act_quit.setShortcut(QKeySequence.StandardKey.Quit)
        self.act_quit.triggered.connect(self.close)

        self.act_fit = QAction("Fit 全長", self)
        self.act_fit.triggered.connect(self._fit_full)

        self.act_preset_psg = QAction("套用標準通道組合", self)
        self.act_preset_psg.triggered.connect(lambda: self._apply_channel_preset(["c3","c4","f3","f4","o1","o2","e1","e2","ecg","flow","spo2"]))

        # PR4: 清除偏好設定（方便測試 rollback，清除 registry/ini 後用 clinical defaults）
        self.act_clear_prefs = QAction("清除偏好記錄", self)
        self.act_clear_prefs.triggered.connect(self._clear_prefs)

        # 還原初始面板按鈕：徹底恢復 splitter 初始寬度比例（左/右側欄 + 中央最大）。
        # 現在用簡單 setSizes，無舊 dock 複雜還原，永遠滑順。
        self.act_reset_panels = QAction("還原初始面板", self)
        self.act_reset_panels.setToolTip("重置左右面板寬度")
        self.act_reset_panels.triggered.connect(self._reset_panel_layout)

        # 螢幕適應：小解析度或移動螢幕時，一鍵讓左右面板寬度適應當前解析度，保證可拖動。
        self.act_adapt_screen = QAction("適應螢幕寬度", self)
        self.act_adapt_screen.setToolTip("根據目前螢幕調整左右面板寬度")
        self.act_adapt_screen.triggered.connect(self._adapt_side_panel_widths)

        # 選擇事件Excel (原替換XLS功能)，放在檔案選單
        self.act_choose_event_excel = QAction("選擇事件Excel", self)
        self.act_choose_event_excel.setToolTip("選擇另一個 Excel (.xls 或 .xlsx) 檔案來替換目前的分析事件標記資料（nef 裝置事件保留）。")
        self.act_choose_event_excel.triggered.connect(self._on_replace_xls_clicked)

        # 例外匹配列表
        self.act_exception_list = QAction("例外匹配列表", self)
        self.act_exception_list.setToolTip("開啟例外匹配列表，設定 NDF 無法匹配時的例外規則（保存並應用後刷新事件相關 UI）。")
        self.act_exception_list.triggered.connect(self._on_show_exception_dialog)

        # 抗鋸齒 (預設開啟；放在「事件預設背景蒙層」上方)。關閉可在密集波形時加速繪製。
        self.act_antialias = QAction("抗鋸齒", self, checkable=True)
        self.act_antialias.setChecked(getattr(self, 'antialias_on', True))
        self.act_antialias.setToolTip("波形線條抗鋸齒。關閉可在密集波形/大量通道時明顯加速（線條略不平滑）。")
        self.act_antialias.triggered.connect(self._on_toggle_antialias)

        # 事件預設背景蒙層 (預設關閉)
        self.act_show_event_overlay = QAction("事件預設背景蒙層", self, checkable=True)
        self.act_show_event_overlay.setChecked(getattr(self, 'show_event_background_overlay', False))
        self.act_show_event_overlay.triggered.connect(self._on_toggle_event_overlay)

        # 事件文字標籤 (預設關閉，放在「事件預設背景蒙層」下方)
        self.act_show_event_text_labels = QAction("事件文字標籤", self, checkable=True)
        self.act_show_event_text_labels.setChecked(getattr(self, 'show_event_text_labels', False))
        self.act_show_event_text_labels.triggered.connect(self._on_toggle_event_text_labels)

        # X軸時間顯示方式 (二級選單)
        self.act_time_hms = QAction("時分秒", self, checkable=True)
        self.act_time_hms.setChecked(getattr(self, 'x_axis_time_format', 'hms') == 'hms')
        self.act_time_seconds = QAction("秒數", self, checkable=True)
        self.act_time_seconds.setChecked(getattr(self, 'x_axis_time_format', 'hms') == 'seconds')
        self.act_time_group = QActionGroup(self)
        self.act_time_group.addAction(self.act_time_hms)
        self.act_time_group.addAction(self.act_time_seconds)
        self.act_time_group.setExclusive(True)
        self.act_time_hms.triggered.connect(lambda checked: self._on_x_time_format_changed('hms') if checked else None)
        self.act_time_seconds.triggered.connect(lambda checked: self._on_x_time_format_changed('seconds') if checked else None)

        # X軸顯示絕對時間 (預設開啟)：開→顯示牆鐘時間(origin+秒)，關→自 0 起相對經過時間
        self.act_x_absolute = QAction("X軸顯示絕對時間", self, checkable=True)
        self.act_x_absolute.setChecked(getattr(self, 'x_axis_absolute_time', True))
        self.act_x_absolute.triggered.connect(self._on_toggle_absolute_time)

        # X軸顯示毫秒 (預設關閉)
        self.act_show_ms = QAction("X軸顯示毫秒", self, checkable=True)
        self.act_show_ms.setChecked(getattr(self, 'x_axis_show_milliseconds', False))
        self.act_show_ms.triggered.connect(self._on_toggle_show_ms)

    def _create_menus(self):
        menubar = self.menuBar()

        # 檔案：開啟資料夾、選擇事件Excel（現有替換功能）、匯出EDF、離開
        file_menu = menubar.addMenu("檔案 (&F)")
        file_menu.addAction(self.act_open)
        file_menu.addAction(self.act_open_edf)
        file_menu.addAction(self.act_choose_event_excel)
        file_menu.addAction(self.act_export)
        file_menu.addSeparator()
        file_menu.addAction(self.act_quit)

        # 檢視：還原初始面板、Fit全長 + 事件預設背景蒙層 / 事件文字標籤 + X軸顯示選項
        view_menu = menubar.addMenu("檢視 (&V)")
        view_menu.addAction(self.act_fit)
        view_menu.addAction(self.act_reset_panels)
        view_menu.addSeparator()
        view_menu.addAction(self.act_antialias)
        view_menu.addAction(self.act_show_event_overlay)
        view_menu.addAction(self.act_show_event_text_labels)
        view_menu.addAction(self.act_x_absolute)
        xaxis_menu = QMenu("X軸時間顯示方式", view_menu)
        xaxis_menu.addAction(self.act_time_hms)
        xaxis_menu.addAction(self.act_time_seconds)
        view_menu.addMenu(xaxis_menu)
        view_menu.addAction(self.act_show_ms)

        # 工具：例外匹配列表、清除偏好記錄
        tools_menu = menubar.addMenu("工具 (&T)")
        tools_menu.addAction(self.act_exception_list)
        tools_menu.addAction(self.act_clear_prefs)

    def _create_toolbar(self):
        # 已移除工具列按鈕行（依需求：不要那一行按鈕，所有功能改由上方選單提供）
        # 如需可在此建立無按鈕的空 toolbar，或完全不建立
        pass

    def _connect_shortcuts(self):
        # 額外快速鍵（已取消快速時間加減快捷鍵）
        self._add_shortcut("F", self._fit_full)
        self._add_shortcut("Ctrl+E", self._export_selected_edf)

    def _add_shortcut(self, key, slot):
        from PyQt6.QtGui import QShortcut
        sc = QShortcut(QKeySequence(key), self)
        sc.activated.connect(slot)

    # ==================== 資料載入 ====================

    def _open_folder_dialog(self):
        folder = QFileDialog.getExistingDirectory(
            self, "選擇 PSG 資料夾 (可選擇 input/ 根目錄或單一 Dxx_... 資料來源資料夾)",
            str(Path.cwd() / "input")
        )
        if not folder:
            return
        self._load_folder(folder, show_error=True)

    def _open_edf_dialog(self):
        edf_path, _ = QFileDialog.getOpenFileName(
            self,
            "開啟 EDF 檔案",
            str(Path.cwd() / "input"),
            "EDF 檔案 (*.edf);;所有檔案 (*.*)",
        )
        if not edf_path:
            return
        self._load_edf_file(edf_path, show_error=True)

    def _load_edf_file(self, edf_path: str | Path, show_error: bool = True):
        """直接載入單一 EDF（支援 Nox 專有非標準格式）。"""
        self.statusBar().showMessage("載入 EDF 中...")
        self.progress.setVisible(True)
        self.progress.setValue(10)
        QApplication.processEvents()
        try:
            self.study = NoxStudy.from_edf(edf_path)
            self.patient_combo.clear()
            self.patient_combo.blockSignals(True)
            for pid in self.study.patients:
                self.patient_combo.addItem(pid, pid)
            self.patient_combo.blockSignals(False)
            self.progress.setValue(40)
            if self.study.patients:
                self.patient_combo.setCurrentIndex(0)
                self._on_patient_changed(0)
            self.statusBar().showMessage(f"已載入 EDF：{Path(edf_path).name}")
        except Exception as e:
            if show_error:
                QMessageBox.critical(
                    self,
                    "EDF 載入失敗",
                    f"無法開啟 EDF 檔案：\n{e}\n\n"
                    "若為 Nox A1 匯出檔，本工具使用自訂解析器讀取（無需 Noxturnal）。",
                )
            self.statusBar().showMessage("EDF 載入失敗")
        finally:
            self.progress.setVisible(False)

    def _get_nice_folder_display_name(self, pid: str) -> str:
        """統一為每個資料來源計算顯示用的資料夾名稱。
        不再寫死特定 pid，也不再讓摘要 MR# 覆蓋 selector。
        優先使用 patient_dir 下 "*raw data*" 或 "*PSG raw*" 子資料夾的完整名稱
        （例如 D18_17923057_PSG raw data_20260511、D20_18075135_PSG raw data_20260512），
        否則退回 pid（頂層資料夾名）。這樣列表中每個項目都走相同邏輯。
        """
        try:
            rec = self.study.get(pid)
            if rec and getattr(rec, "patient_dir", None):
                pd = rec.patient_dir
                for sub in pd.glob("*"):
                    if sub.is_dir() and ("raw data" in sub.name.lower() or "psg raw" in sub.name.lower()):
                        return sub.name
                if getattr(rec, "raw_dir", None):
                    rp = rec.raw_dir.parent
                    if rp and rp != pd and ("raw" in rp.name.lower() or "PSG" in rp.name):
                        return rp.name
        except Exception:
            pass
        return pid

    def _apply_empty_state(self):
        """無可載入資料時重置 UI 為空白，不顯示錯誤對話框。"""
        self.current_rec = None
        self.max_duration = 0.0
        self.visible_channels = []
        self.parsed_events = []
        self._events_loaded = False
        self._custom_xls_path = None
        self.time_start = 0.0
        self.time_duration = 60.0

        self.patient_combo.clear()
        if self.patient_banner:
            self.patient_banner.set_patient_info({})

        self._populate_channel_table()
        self._populate_events_table()
        self._init_signal_plots()

        if hasattr(self, "dur_spin") and self.dur_spin:
            try:
                self.dur_spin.setMaximum(3600.0)
                self.dur_spin.setValue(60.0)
            except Exception:
                pass
        if hasattr(self, "start_spin") and self.start_spin:
            try:
                self.start_spin.setMaximum(0.0)
                self.start_spin.setValue(0.0)
            except Exception:
                pass
        if hasattr(self, "overview_end_time_label") and self.overview_end_time_label:
            try:
                self.overview_end_time_label.setText("")
            except Exception:
                pass

    def _load_folder(self, folder_path: str | Path, show_error: bool = True):
        """載入資料夾。
        show_error=False 時失敗只更新 status，不跳 QMessageBox（適合啟動自動載入 input）。
        只有使用者點「開啟資料夾」並選到無效結構時才顯示錯誤對話框。
        """
        self.statusBar().showMessage("載入中...")
        self.progress.setVisible(True)
        self.progress.setValue(10)
        QApplication.processEvents()

        try:
            self.study = NoxStudy(folder_path)
            if len(self.study) == 0:
                self._apply_empty_state()
                self.statusBar().showMessage("就緒 — 未找到可載入的資料", 5000)
                return

            self.patient_combo.clear()
            self.patient_combo.blockSignals(True)
            for pid in self.study.patients:
                display_name = self._get_nice_folder_display_name(pid)
                self.patient_combo.addItem(display_name, pid)  # userData 存原始 key 供切換使用
            self.patient_combo.blockSignals(False)

            self.progress.setValue(40)
            # 預設載入第一個
            if self.study.patients:
                self.patient_combo.blockSignals(True)
                self.patient_combo.setCurrentIndex(0)
                self.patient_combo.blockSignals(False)
                self._on_patient_changed(0)

            # PR4: _load_folder 結束後最終強制套用 prefs（其他如 visible/height 仍套用；time 會被 _set_initial_fast_time_window 覆蓋成前 60s 保證快速啟動）
            if self.current_rec and getattr(self, 'prefs', None):
                pm = PrefsManager()
                # time 視窗不從 prefs 還原（強制 0+60s 快速啟動），其他 prefs 繼續套用
                self._set_initial_fast_time_window()
                # 還原通道高度
                try:
                    if hasattr(self, 'height_spin'):
                        hh = pm.load_channel_height(200)
                        self.height_spin.blockSignals(True)
                        self.height_spin.setValue(hh)
                        self.height_spin.blockSignals(False)
                        self.desired_channel_height = hh
                        if getattr(self, 'plot_items', None):
                            self._apply_channel_heights()
                except Exception:
                    pass
                # 還原事件蒙層
                try:
                    show_ev = pm.load_show_events(False)
                    pass  # 舊 show_event_overlays 已移除
                    pass  # 事件篩選 UI 已改為 channel 多選列表，無舊 cb
                except Exception:
                    pass
                saved_vis = pm.load_visible(self.current_rec.name, None)
                if saved_vis is not None:
                    self._apply_saved_visible(saved_vis)
                pm.load_viz_settings(self.current_rec.name, self._viz_settings)
                self._update_overview_region()
                self._update_time_controls()
                self._update_view()

            self.progress.setValue(100)
            self.statusBar().showMessage(f"已載入 {len(self.study)} 個錄音", 5000)

        except Exception as e:
            if show_error:
                QMessageBox.critical(self, "載入失敗", f"無法載入資料夾：\n{e}\n\n請確認資料夾結構是否正確 (包含 raw data 子目錄與 SETUP.INI)。")
                self.statusBar().showMessage("載入失敗")
            else:
                self._apply_empty_state()
                self.statusBar().showMessage("就緒 — 未找到可載入的資料", 5000)
        finally:
            self.progress.setVisible(False)

    def _on_patient_changed(self, index: int):
        if not self.study or index < 0:
            return
        # 從 combo itemData 取原始 key（顯示名稱已是資料夾名，切換仍用原始 pid）
        key = self.patient_combo.itemData(index)
        if not key:
            key = self.study.patients[index] if index < len(self.study.patients) else None
        if not key:
            return
        # PR4: 只有真正切換到不同資料來源時才保存上一個（避免 init 期間重複 _on 同一 rec 時用預設值覆寫 prefs）
        if self.current_rec and self.current_rec.name != key:
            self._save_current_prefs()
        # 切換資料來源時重置事件篩選列表與選取（包含「在所有通道顯示」勾選）
        self.selected_event_channels.clear()
        self._events_loaded = False
        self._custom_xls_path = None  # 切換錄音時重置自訂 xls
        if hasattr(self, 'chk_display_all_channels') and self.chk_display_all_channels:
            self.chk_display_all_channels.blockSignals(True)
            self.chk_display_all_channels.setChecked(False)
            self.chk_display_all_channels.blockSignals(False)
        if not getattr(self, '_events_loaded', False):
            if hasattr(self, 'btn_load_events') and self.btn_load_events:
                self.btn_load_events.setVisible(True)
                self.btn_load_events.setText("載入事件資料")
                self.btn_load_events.setEnabled(True)
                self.btn_load_events.setStyleSheet("")  # 重置樣式
            if hasattr(self, 'instruction_label') and self.instruction_label:
                self.instruction_label.setVisible(False)
            if hasattr(self, 'btn_replace_excel') and self.btn_replace_excel:
                self.btn_replace_excel.setEnabled(False)
                self.btn_replace_excel.setStyleSheet("")  # 重置樣式
        else:
            if hasattr(self, 'btn_load_events') and self.btn_load_events:
                self.btn_load_events.setVisible(False)
            if hasattr(self, 'instruction_label') and self.instruction_label:
                self.instruction_label.setVisible(True)
            if hasattr(self, 'btn_replace_excel') and self.btn_replace_excel:
                self.btn_replace_excel.setEnabled(True)
                self.btn_replace_excel.setStyleSheet("")  # 確保正常樣式
        self._set_event_list_placeholder_if_needed()
        rec = self.study.get(key)
        self._load_recording(rec)

    def _reload_current(self):
        if self.current_rec:
            self._load_recording(self.current_rec)

    def _load_recording(self, rec: NoxRecording | NoxEdfRecording):
        self._loading_rec = True
        self.current_rec = rec
        # 概觀軸/捲軸右界用「全域跨度」（origin→max channel end），涵蓋因分批啟動而較晚結束的通道與 EDF 尾段；
        # NoxEdfRecording 無 total_span_sec 時退回 duration_sec。
        self.max_duration = float(getattr(rec, 'total_span_sec', None) or rec.duration_sec)
        # 立即更新時間 spin 的動態上限（長度上限從硬編 3600 改為總長，避免無法設定 >3600s 的視窗）
        if hasattr(self, 'dur_spin') and self.dur_spin:
            try:
                self.dur_spin.setMaximum(self.max_duration)
            except Exception:
                pass
        if hasattr(self, 'start_spin') and self.start_spin:
            try:
                self.start_spin.setMaximum(self.max_duration)
            except Exception:
                pass
        if hasattr(self, 'overview_end_time_label'):
            self.overview_end_time_label.setText(self._overview_end_text())
            # 位置：右上角，留一點空 (x 靠右但不貼邊)
            delta = max(1.0, self.max_duration * 0.005)
            self.overview_end_time_label.setPos(self.max_duration - delta, 0.95)

        # 立即強制初始只載前 60s from 0（解決開啟慢）。這會設定 time_start/dur 並更新 overview region。
        self._set_initial_fast_time_window()

        # 確保概觀軸永遠鎖定在 0 ~ max_duration 全寬度（100% 時間），
        # 不隨下方 zoom/pan 改變其 x-scale；藍色 region 只視覺表示目前下方視窗位置
        if getattr(self, 'overview_plot', None):
            self.overview_plot.setMouseEnabled(x=False, y=False)
            self._set_overview_xrange()
        if getattr(self, 'overview_region', None):
            self.overview_region.setBounds([0, self.max_duration])
            self._updating_view = True
            try:
                self.overview_region.setRegion([self.time_start, self.time_start + self.time_duration])
            finally:
                self._updating_view = False

        # 重置事件篩選（預設不顯示任何 event，包含「在所有通道顯示」）
        # 效能：重置 loaded flag，列表將在 load 後被設為 placeholder
        self.selected_event_channels.clear()
        self._events_loaded = False
        self._custom_xls_path = None  # 切換錄音時重置自訂 xls
        if hasattr(self, 'event_channel_list') and self.event_channel_list:
            tbl = self.event_channel_list
            tbl.clear()
            tbl.setRowCount(0)
            tbl.clearSpans()
            tbl.setColumnCount(2)
            tbl.setHorizontalHeaderLabels(["名稱", "數量"])
            tbl.verticalHeader().setVisible(False)
            tbl.setRowCount(1)
            item0 = QTableWidgetItem("請先載入事件資料")
            tbl.setItem(0, 0, item0)
            item1 = QTableWidgetItem("")
            item1.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            tbl.setItem(0, 1, item1)
        if hasattr(self, 'chk_display_all_channels') and self.chk_display_all_channels:
            self.chk_display_all_channels.blockSignals(True)
            self.chk_display_all_channels.setChecked(False)
            self.chk_display_all_channels.blockSignals(False)
        if hasattr(self, 'btn_load_events') and self.btn_load_events:
            self.btn_load_events.setVisible(True)
            self.btn_load_events.setText("載入事件資料")
            self.btn_load_events.setEnabled(True)
            self.btn_load_events.setStyleSheet("")  # 重置樣式
        if hasattr(self, 'instruction_label') and self.instruction_label:
            self.instruction_label.setVisible(False)
        if hasattr(self, 'btn_replace_excel') and self.btn_replace_excel:
            self.btn_replace_excel.setEnabled(False)
            self.btn_replace_excel.setStyleSheet("")  # 重置樣式

        # 初始設定概觀軸 region，確保藍色蒙層立即顯示目前視窗位置
        # （time 已在 max_duration 後立即由 _set_initial_fast_time_window 設為 from-0 60s）
        if getattr(self, 'overview_region', None):
            self._updating_view = True
            try:
                self.overview_region.setRegion([self.time_start, self.time_start + self.time_duration])
            finally:
                self._updating_view = False

        # PR3: 使用 PatientInfoBanner + ClinicalSummary 取代舊 info_text (從 get_patient_info 填真實臨床資料)
        # 舊技術 meta (name/dur/channels/path) 併入 banner 次要區；device 也由 banner 顯示
        if self.patient_banner:
            self.patient_banner.set_from_rec(rec)

        # 重置通道列表排序狀態：新載入 rec 時回到 rec.channels 原始順序（build 時已按 -fs, name 排序）
        # 使用者點擊表頭後才進入排序模式；避免預設改變既有列表順序。
        self._channel_sort_col = 1  # 名稱欄，預設按名稱順序排列
        self._channel_sort_order = Qt.SortOrder.AscendingOrder
        if hasattr(self, 'channel_table') and self.channel_table:
            try:
                self.channel_table.horizontalHeader().setSortIndicator(1, Qt.SortOrder.AscendingOrder)
            except Exception:
                pass

        # 填充通道表格（內部使用硬編 clinical defaults 做初始 checks）
        self._populate_channel_table()

        # PR4: 若此 rec 有儲存的 visible，覆蓋表格 checks 並重建 visible_channels（per-rec override）
        if getattr(self, "prefs", None) and self.current_rec:
            pm = PrefsManager()
            saved_vis = pm.load_visible(self.current_rec.name, CLINICAL_DEFAULT_CHANNELS)
            if saved_vis is None or len(saved_vis) > 15:  # 避免之前「全選」儲存導致載入卡死
                saved_vis = CLINICAL_DEFAULT_CHANNELS
            self._apply_saved_visible(saved_vis)

        # 預設按名稱順序排列通道顯示（含波形順序）
        if getattr(self, '_channel_sort_col', -1) >= 0:
            self._sort_channel_table()

        # PR4: 還原此 rec 的 position style / viz settings（per-rec；若無則保留 VizSettings 內建 step_fill clinical default）
        if getattr(self, "prefs", None) and self.current_rec:
            pm = PrefsManager()
            pm.load_viz_settings(self.current_rec.name, self._viz_settings)

        # 效能優化：預設不自動載入事件資料（不呼叫 get_events 解讀 xls，避免初始載入全部資料造成卡頓）
        # 只有使用者點擊左側「載入事件資料」按鈕後才解讀所有標記，之後勾選時才繪製。
        # 如果未載入或未選取，絕不載入/處理全部事件資料。
        # 清除偏好後的預設可見通道改為 DeepNYX，高度 200px。
        self.selected_event_channels.clear()
        if hasattr(self, 'chk_display_all_channels') and self.chk_display_all_channels:
            self.chk_display_all_channels.blockSignals(True)
            self.chk_display_all_channels.setChecked(False)
            self.chk_display_all_channels.blockSignals(False)
        self._events_loaded = False
        self._custom_xls_path = None  # 切換錄音時重置自訂 xls
        self.parsed_events = []
        self._populate_events_table()  # 會因未載入而顯示「尚未載入事件資料」的提示
        # 載入後 dock 高度可能變化，保險套用一次事件 splitter 2:1 預設
        QTimer.singleShot(0, self._apply_default_event_splitter_ratio)
        # 列表由按鈕載入時更新，這裡先設 placeholder（如果 list 存在）
        if hasattr(self, 'event_channel_list') and self.event_channel_list:
            tbl = self.event_channel_list
            tbl.clear()
            tbl.setRowCount(0)
            tbl.clearSpans()
            tbl.setColumnCount(2)
            tbl.setHorizontalHeaderLabels(["名稱", "數量"])
            tbl.verticalHeader().setVisible(False)
            tbl.setRowCount(1)
            item0 = QTableWidgetItem("請先載入事件資料")
            tbl.setItem(0, 0, item0)
            item1 = QTableWidgetItem("")
            item1.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            tbl.setItem(0, 1, item1)

        # 還原通道高度 (issue2) - 早期還原
        if hasattr(self, 'height_spin') and getattr(self, 'prefs', None) and self.current_rec:
            try:
                pm2 = PrefsManager()
                h = pm2.load_channel_height(200)
                self.height_spin.blockSignals(True)
                self.height_spin.setValue(h)
                self.height_spin.blockSignals(False)
                self.desired_channel_height = h
            except Exception:
                pass
            # 還原事件蒙層 (早期)
            try:
                show_ev = pm2.load_show_events(False)
                pass  # 舊 show_event_overlays 已移除
            except Exception:
                pass

        # 初始化波形檢視
        self._init_signal_plots()

        # 初始視圖：改用逐步載入避免大量通道時卡死（後續時間導航仍用完整 _update_view）。
        # 另外在控制面板改變 visible channel 時已實作「差異載入」（只 get_data 新增的，其餘從快照重用），
        # + range change 高頻時 debounce，顯著降低互動卡頓。
        self._start_progressive_update()
        self._update_time_controls()

        # PR4: 收尾再套用 prefs（啟動流程中可能有多次 _load_folder / 信號觸發，確保最後狀態為儲存值）
        if getattr(self, "prefs", None) and self.current_rec:
            pm = PrefsManager()  # fresh read to avoid any instance timing
            # 注意：時間視窗**不**還原（我們已在 _load_recording 開頭 + 這裡收尾強制只用前 60s from 0，保證啟動快）。
            # 其他 prefs（visible、height、viz、event 等）仍正常套用。
            # 已取消 scale UI，固定 1.0
            # 還原通道高度 (issue2)
            try:
                h = pm.load_channel_height(200)
                self.height_spin.blockSignals(True)
                self.height_spin.setValue(h)
                self.height_spin.blockSignals(False)
                self.desired_channel_height = h
                # 立即同步到已初始化的 plots (若此時已有)
                if getattr(self, 'plot_items', None):
                    self._apply_channel_heights()
            except Exception:
                pass
            # 還原事件蒙層顯示 (新功能1)
            try:
                show_ev = pm.load_show_events(False)
                pass  # 舊 show_event_overlays 已移除
                pass  # 舊事件 cb 已移除
                if getattr(self, 'plot_items', None):
                    for reg in getattr(self, 'event_overlay_regions', []):
                        for pp in self.plot_items or []:
                            try:
                                pp.removeItem(reg)
                            except Exception:
                                pass
                    self.event_overlay_regions = []
                    if show_ev and getattr(self, 'show_event_background_overlay', False):
                        self._add_event_time_overlays()
            except Exception:
                pass
            saved_vis = pm.load_visible(self.current_rec.name, CLINICAL_DEFAULT_CHANNELS)
            if saved_vis is None or len(saved_vis) > 15:  # 避免之前「全選」儲存導致載入卡死
                saved_vis = CLINICAL_DEFAULT_CHANNELS
            self._apply_saved_visible(saved_vis)
            pm.load_viz_settings(self.current_rec.name, self._viz_settings)
            # 最後再強制一次初始快取 60s 視窗（覆蓋任何中間可能還原的大 dur），確保 progressive / update 只載少量資料
            self._set_initial_fast_time_window()
            self._update_time_controls()
            self._update_view(immediate=True)

        # 確保如果未載入事件，列表顯示 placeholder（防多處 clear 後遺漏）
        if not getattr(self, '_events_loaded', False) and hasattr(self, 'event_channel_list') and self.event_channel_list:
            tbl = self.event_channel_list
            if tbl.rowCount() == 0 or "(尚未載入" not in (tbl.item(0, 0).text() if tbl.item(0, 0) else ""):
                tbl.clear()
                tbl.setRowCount(1)
                item = QTableWidgetItem("請先載入事件資料")
                tbl.setItem(0, 0, item)
                tbl.setSpan(0, 0, 1, 2)
        if hasattr(self, 'btn_load_events') and self.btn_load_events and not getattr(self, '_events_loaded', False):
            self.btn_load_events.setVisible(True)
            self.btn_load_events.setText("載入事件資料")
            self.btn_load_events.setEnabled(True)
            self.btn_load_events.setStyleSheet("")
        if hasattr(self, 'instruction_label') and self.instruction_label:
            self.instruction_label.setVisible(False)
        if hasattr(self, 'btn_replace_excel') and self.btn_replace_excel and not getattr(self, '_events_loaded', False):
            self.btn_replace_excel.setEnabled(False)
            self.btn_replace_excel.setStyleSheet("")

        self._set_event_list_placeholder_if_needed()

        # 確保載入後 splitter 佈局正確（適應可能因載入 channel 數改變的內容 hint）
        QTimer.singleShot(0, self._reset_panel_layout)

        self._loading_rec = False
        self.statusBar().showMessage(f"已載入 {rec.name}", 4000)

    def _populate_channel_table(self):
        self.channel_table.blockSignals(True)
        self.channel_table.setRowCount(0)

        if not self.current_rec:
            self.channel_table.blockSignals(False)
            return

        chans = self.current_rec.channels  # list of dicts
        # 清除偏好記錄後的預設：採用 DeepNYX（而非標準PSG），通道預設高度改為 200px
        # 避免全選導致載入過慢；使用者可手動勾選更多或使用預設按鈕
        for ch in chans:
            is_checked = channel_matches_preset(ch["name"], CLINICAL_DEFAULT_CHANNELS)
            self._create_channel_row(ch, is_checked)

        self.channel_table.blockSignals(False)

        # 初始可見通道（此時表格順序 = rec.channels 原始順序，除非之前有 header 排序但新載入已重置）
        self.visible_channels = []
        for row in range(self.channel_table.rowCount()):
            chk = self.channel_table.cellWidget(row, 0).findChild(QCheckBox)
            if chk and chk.isChecked():
                name = chk.property("channel_name")
                if name:
                    self.visible_channels.append(name)

        # 壓縮欄寬 + 設定較小 minWidth：大幅降低 channel_table 的 sizeHint，
        # 避免它貢獻過大主視窗 minSize（跨小螢幕時的 geometry/MINMAXINFO 錯誤主因）。
        # resizeColumnsToContents 會依內容算合理寬；我們再 cap 過寬的（尤其是「樣本數」長數字），
        # Interactive 模式仍允許使用者之後手動拖拉表頭欄位變寬/窄。
        try:
            self.channel_table.resizeColumnsToContents()
            if self.channel_table.columnCount() >= 6:
                caps = {0: 34, 1: 85, 2: 52, 3: 52, 4: 72, 5: 110}  # 顯示/名稱/fs/單位/樣本數/來源
                for col, mw in caps.items():
                    if self.channel_table.columnWidth(col) > mw:
                        self.channel_table.setColumnWidth(col, mw)
            self.channel_table.setMinimumWidth(110)
            self.channel_table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        except Exception:
            pass

    def _on_channel_check_changed(self, *args):
        if not self.current_rec:
            return
        self._sync_visible_channels_from_table()  # 內含掃描表格順序 + _init + progressive + save prefs
        # 註：原大量通道卡死問題已由 _start_progressive_update 處理

    def _apply_channel_preset(self, desired: list[str] | None):
        if desired is None:
            # 全選
            for row in range(self.channel_table.rowCount()):
                chk = self.channel_table.cellWidget(row, 0).findChild(QCheckBox)
                if chk:
                    chk.setChecked(True)
            return
        if not desired:
            for row in range(self.channel_table.rowCount()):
                chk = self.channel_table.cellWidget(row, 0).findChild(QCheckBox)
                if chk:
                    chk.setChecked(False)
            return

        for row in range(self.channel_table.rowCount()):
            chk = self.channel_table.cellWidget(row, 0).findChild(QCheckBox)
            if not chk:
                continue
            name = chk.property("channel_name") or ""
            chk.setChecked(channel_matches_preset(name, desired))

    def _filter_channels(self, text: str):
        """即時過濾通道表格（隱藏不匹配 row），不改變 visible_channels。"""
        text = (text or "").lower().strip()
        for row in range(self.channel_table.rowCount()):
            name_item = self.channel_table.item(row, 1)
            if name_item:
                match = text == "" or text in name_item.text().lower()
                self.channel_table.setRowHidden(row, not match)

    def _hide_channel(self, ch_name: str):
        """右鍵選單「隱藏通道」隱藏該通道（或在左側表格取消勾選）。
        會更新左側 checkbox 並重建波形顯示。
        """
        if not ch_name:
            return
        # 在左側表格找到對應 checkbox 並取消勾選
        for row in range(self.channel_table.rowCount()):
            w = self.channel_table.cellWidget(row, 0)
            if not w:
                continue
            chk = w.findChild(QCheckBox)
            if chk and chk.property("channel_name") == ch_name:
                if chk.isChecked():
                    chk.setChecked(False)
                break

    def _on_channel_header_clicked(self, logical_index: int):
        """表頭點擊：切換該欄的升序/降序排序。
        點擊同欄 toggle 順序/倒序；切換欄則預設升序。
        排序後會重建表格 + 讓波形 plots 跟隨 checked channels 的新順序。
        （已取消樣式欄）
        """
        if logical_index < 0 or logical_index >= self.channel_table.columnCount():
            return
        if self._channel_sort_col == logical_index:
            self._channel_sort_order = (
                Qt.SortOrder.DescendingOrder
                if self._channel_sort_order == Qt.SortOrder.AscendingOrder
                else Qt.SortOrder.AscendingOrder
            )
        else:
            self._channel_sort_col = logical_index
            self._channel_sort_order = Qt.SortOrder.AscendingOrder

        # 顯示小箭頭（▲/▼），不佔額外寬度
        self.channel_table.horizontalHeader().setSortIndicator(
            self._channel_sort_col, self._channel_sort_order
        )
        self._sort_channel_table()

    def _create_channel_row(self, ch: dict, checked: bool):
        """建立單一通道 row（checkbox widget + 資料欄 + tooltip + signal connect）。
        供 _populate_channel_table 與 _sort_channel_table 共用，避免重複。
        （已取消樣式按鈕）
        """
        row = self.channel_table.rowCount()
        self.channel_table.insertRow(row)
        ch_name = ch["name"]

        # 顯示勾選
        chk = QCheckBox()
        chk.setChecked(checked)
        cell_widget = QWidget()
        lay = QHBoxLayout(cell_widget)
        lay.addWidget(chk)
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.setContentsMargins(0, 0, 0, 0)
        self.channel_table.setCellWidget(row, 0, cell_widget)
        chk.setProperty("channel_name", ch_name)

        # 資料欄（名稱、fs、單位、樣本數）使用 QTableWidgetItem，方便未來擴充
        self.channel_table.setItem(row, 1, QTableWidgetItem(ch_name))
        self.channel_table.setItem(row, 2, QTableWidgetItem(f"{ch['fs']:.1f}"))
        self.channel_table.setItem(row, 3, QTableWidgetItem(ch["unit"] or ""))
        self.channel_table.setItem(row, 4, QTableWidgetItem(str(ch["samples"])))
        source_label = ch.get("source_label") or ""
        src_item = QTableWidgetItem(source_label)
        src_item.setToolTip(ch.get("path") or source_label)
        self.channel_table.setItem(row, 5, src_item)

        # PR5 低頻提示（position / fs<5）
        fs_val = ch.get("fs", 0)
        if fs_val < 5 or self.current_rec.is_position_channel(ch_name):
            lowfreq_tip = "低頻通道（fs<5Hz 或 position），使用特殊渲染。"
            for c in range(6):
                item = self.channel_table.item(row, c)
                if item:
                    item.setToolTip(lowfreq_tip)
            if self.channel_table.cellWidget(row, 0):
                self.channel_table.cellWidget(row, 0).setToolTip(lowfreq_tip)

        # 狀態變化連線（注意：呼叫端負責 blockSignals 避免初始 setChecked 觸發）
        chk.stateChanged.connect(self._on_channel_check_changed)

    def _sync_visible_channels_from_table(self):
        """從表格目前順序 + 勾選狀態，重建 visible_channels 並刷新 plots（用於勾選改變、表頭排序後）。
        效能優化：使用 _snapshot_current_data + _load_channels_data 做「差異載入」，
        只對「新增」的 channel 真正呼叫 get_data，其餘直接重用先前已載入的波形資料（避免每次都全量卡頓）。
        """
        if not self.current_rec:
            return

        # 1. 改變前快照目前顯示資料（staying channels 可直接重用）
        data_snapshot = self._snapshot_current_data()

        # 2. 依表格目前由上到下順序掃描，決定新的 visible 列表（這是 plot 垂直順序的來源）
        self.visible_channels = []
        for row in range(self.channel_table.rowCount()):
            chk_w = self.channel_table.cellWidget(row, 0)
            if chk_w:
                chk = chk_w.findChild(QCheckBox)
                if chk and chk.isChecked():
                    name = chk.property("channel_name")
                    if name:
                        self.visible_channels.append(name)

        # 3. 重建 plot skeletons（為了正確 row 順序與新加的 channel，目前仍需重建結構；但資料層面差異處理）
        self._init_signal_plots()

        # 4. 立即套用快照到「未改變的 channel」（零 I/O，瞬間顯示）
        for i, ch in enumerate(self.visible_channels):
            if ch in data_snapshot:
                x, y, _fs = data_snapshot[ch]
                if i < len(getattr(self, 'curves', [])) and self.curves[i] is not None:
                    self.curves[i].setData(x, y, antialias=self.antialias_on)
                    if i < len(self.plot_items):
                        p = self.plot_items[i]
                        p.setXRange(self.time_start, self.time_start + self.time_duration, padding=0)
                        if len(y) > 0:
                            ymin, ymax = self._get_robust_y_limits(y, ch)
                            p.setYRange(ymin, ymax, padding=0)
                        if len(y) > 0 and len(y) < 300:
                            self.curves[i].setSymbol('o')
                            self.curves[i].setSymbolSize(3)
                            self.curves[i].setSymbolBrush(pg.mkBrush(0, 0, 139))
                            self.curves[i].setSymbolPen(None)
                        else:
                            self.curves[i].setSymbol(None)

        # 5. 只針對「這次真正新增或不在快照裡」的 channel 做選擇性資料載入（差異）
        to_load = [ch for ch in self.visible_channels if ch not in data_snapshot]
        if to_load:
            self._load_channels_data(to_load)
        else:
            # 全部重用快照，補 set X（以防）
            for i, p in enumerate(getattr(self, 'plot_items', []) or []):
                try:
                    p.setXRange(self.time_start, self.time_start + self.time_duration, padding=0)
                except Exception:
                    pass

        self._save_current_prefs()

    def _sort_channel_table(self):
        """依 _channel_sort_col / _channel_sort_order 重新排列表格 row。
        快照目前 checked 狀態（支援 col 0 依勾選分組）、重建 row（保留 checked 與按鈕連線）、
        重新套用搜尋過濾、最後同步 visible 與 plots 順序。
        不會讓左面板變寬（只用既有 header 的小排序箭頭）。
        """
        if not self.current_rec or self._channel_sort_col < 0:
            return
        table = self.channel_table
        table.blockSignals(True)

        # 快照目前哪些 channel 被勾選（不依賴 row 順序）
        checked_names = set()
        for r in range(table.rowCount()):
            w = table.cellWidget(r, 0)
            chk = w.findChild(QCheckBox) if w else None
            if chk and chk.isChecked():
                nm = chk.property("channel_name") or ""
                if nm:
                    checked_names.add(nm)

        # 取得來源 channels 並依 col 排序
        chans = list(self.current_rec.channels)
        col = self._channel_sort_col
        reverse = (self._channel_sort_order == Qt.SortOrder.DescendingOrder)

        def get_sort_key(ch):
            name = ch.get("name", "")
            if col == 0:  # 顯示：勾選的在前
                return (0 if name in checked_names else 1, name.lower())
            if col == 1:  # 名稱
                return name.lower()
            if col == 2:  # fs (Hz)
                return ch.get("fs", 0)
            if col == 3:  # 單位
                return (ch.get("unit") or "").lower()
            if col == 4:  # 樣本數
                return ch.get("samples", 0)
            if col == 5:  # 來源
                return (ch.get("source_label") or "").lower()
            # 其他或無效 col：退回名稱排序
            return name.lower()

        chans.sort(key=get_sort_key, reverse=reverse)

        # 重建表格（依新順序 + 還原 checked）
        table.setRowCount(0)
        for ch in chans:
            is_checked = ch["name"] in checked_names
            self._create_channel_row(ch, is_checked)

        # 還原搜尋過濾狀態
        search_text = self.channel_search.text() if hasattr(self, "channel_search") else ""
        self._filter_channels(search_text)

        table.blockSignals(False)

        # 讓 plots 順序跟隨表格新排序（checked channels 的出現順序）
        self._sync_visible_channels_from_table()

        # sort 後也重新壓縮欄寬（保持小 minSize）
        try:
            self.channel_table.resizeColumnsToContents()
            if self.channel_table.columnCount() >= 6:
                caps = {0: 34, 1: 85, 2: 52, 3: 52, 4: 72, 5: 110}
                for col, mw in caps.items():
                    if self.channel_table.columnWidth(col) > mw:
                        self.channel_table.setColumnWidth(col, mw)
        except Exception:
            pass

    # ==================== 事件表 ====================

    def _populate_events_table(self):
        if hasattr(self, 'events_note_label') and self.events_note_label:
            self.events_note_label.setText("")
        if not self.current_rec:
            self._show_events_message("")
            return
        if not getattr(self, '_events_loaded', False):
            self._show_events_message("請先載入事件資料（上方事件篩選）")
            return
        # 先解析並儲存含 rel time + 供圖表使用 (只在已載入時)
        self._parse_and_store_events()
        events = self._get_filtered_events()
        if not events:
            msg = "請從上方勾選事件通道" if not self.selected_event_channels else "所選項目無事件資料"
            self._show_events_message(msg)
            return

        # 真虛擬化：把全部事件丟給模型，畫面只渲染可見列（不截斷、不分批、不凍結）。
        rows = [self._build_event_row_dict(ev) for ev in events]
        self.events_table.clearSpans()
        self.events_model.set_rows(rows)
        self.events_proxy.sort(1, Qt.SortOrder.AscendingOrder)  # 預設依開始時間（rel_s）升冪
        self.events_table.horizontalHeader().setSortIndicator(1, Qt.SortOrder.AscendingOrder)
        self._apply_event_column_widths()

        label = f"事件標記 - 共 {len(events)} 筆"
        if hasattr(self, 'chk_display_all_channels') and self.chk_display_all_channels and self.chk_display_all_channels.isChecked():
            label += "（含所有通道）"
        self.events_label.setText(label)

    def _show_events_message(self, msg):
        """事件表顯示單列佔位訊息（未載入 / 無資料）。"""
        self.events_table.clearSpans()
        self.events_model.set_message(msg)
        try:
            self.events_table.setSpan(0, 0, 1, len(_EventTableModel.COLUMNS))
        except Exception:
            pass
        self.events_label.setText("事件標記")

    def _apply_event_column_widths(self):
        """時間相關欄位依內容初始寬度 + 上限（precision 已限制掃描列數，保住虛擬化效能）。"""
        try:
            for col in (0, 1, 2, 3):  # 跳轉 + 開始/結束/持續
                self.events_table.resizeColumnToContents(col)
            caps = {0: 42, 1: 92, 2: 92, 3: 65, 4: 78, 5: 68}
            for col, mw in caps.items():
                if self.events_table.columnWidth(col) > mw:
                    self.events_table.setColumnWidth(col, mw)
            self.events_table.setMinimumWidth(130)
            self.events_table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        except Exception:
            pass

    def _build_event_row_dict(self, ev):
        """把單一事件 dict 轉成虛擬化模型用的一列（顯示字串 + 排序鍵 + 背景色 + tooltip）。"""
        rel_s = ev.get("rel_start", None)
        if rel_s is None:
            ts = ev.get("starts_at") or ev.get("starts") or 0
            rel_s = self._filetime_to_relative_seconds(ts)
        rel_e = ev.get("rel_end", rel_s)
        if rel_e is None or rel_e < rel_s:
            rel_e = rel_s

        # 時間欄一律顯示絕對牆鐘時間；排序鍵用 rel_s（跨午夜單調），不受 X軸選單影響。
        start_str = self._abs_clock_str(rel_s, include_ms=True) if rel_s is not None else "-"
        has_end = self._event_has_meaningful_end(ev)
        end_str = self._abs_clock_str(rel_e, include_ms=True) if has_end else "-"
        dur = rel_e - rel_s if has_end else 0
        dur_str = f"{dur:.3f} s" if has_end else "-"

        type_ = str(ev.get("type", ""))
        loc = ev.get('table_position', str(ev.get("location", "")))
        notes = str(ev.get("notes", ""))

        remark = f"[{type_}] {notes[:35]}" if type_ else notes[:40]
        if not remark:
            remark = type_ or "-"

        is_no_match = ev.get('is_no_match', False) or self._is_unmatched_event(ev)
        exc_mapped = ev.get('exception_matched_to')
        if is_no_match and not remark.startswith("[無法匹配"):
            remark = "[無法匹配 channel] " + remark

        bg = None
        if is_no_match:
            bg = QColor(255, 255, 200)
        elif exc_mapped:
            bg = QColor(232, 245, 233)

        full_tip = (
            f"開始: {start_str}\n"
            f"結束: {end_str}\n"
            f"持續: {dur_str}\n"
            f"類型: {type_}\n"
            f"名稱 (channel/sensor): {loc}\n"
            f"備註: {notes or '(無)'}\n"
            f"原始 starts_at: {ev.get('starts_at', '')}\n"
            f"原始 ends_at: {ev.get('ends_at', '')}"
        )
        if exc_mapped:
            full_tip = f"【通過例外列表匹配: {exc_mapped}】\n{full_tip}"
        if is_no_match:
            full_tip = "【無法匹配任何 raw data Channel，獨立被記錄的 event】\n" + full_tip

        rs_key = rel_s if rel_s is not None else -1.0
        return {
            'disp': ["→", start_str, end_str, dur_str, loc, remark],
            'sort': [rs_key, rs_key, (rel_e if has_end else -1.0),
                     (dur if has_end else 0.0), loc.lower(), remark.lower()],
            'rel_start': rel_s,
            'bg': bg,
            'tip': full_tip,
        }

    def _filetime_to_relative_seconds(self, ft: int) -> float:
        """粗略將 Windows FILETIME (100ns since 1601) 轉成相對錄音開始的秒數。
        這裡用簡化方式：假設最小值對應 0，最大對應總時長。
        實際應用中應使用錄音開始的精確 FILETIME 做 offset。
        """
        if not ft or ft < 1e15:
            return 0.0
        # 簡化：使用線性映射（實際資料中事件時間跨度應接近總時長）
        # 更好的做法是計算 (ft - base) / 1e7 得到 unix 秒，再減錄音開始 unix 秒。
        # 這裡為了即時可用，做一個簡單的相對估計。
        try:
            # 取目前已知事件的最小/最大做比例 (動態)
            # 簡化：直接用一個合理的 offset 估計相對位置
            # 實際上很多 Nox 事件時間戳與錄音時長比例接近
            # 我們直接回傳一個在 0~max 之間的估計值
            # 為了簡單，先用一個 heuristic：除以一個常數得到合理秒數
            # 從先前樣本看，值約 6.39e17 ，對應 ~7 小時 (25200s)
            # 粗估： (ft - 6.3914e17) / 適當比例
            base = 639141000000000000   # 從樣本推
            delta = (ft - base) / 1e7 / 100   # 非常粗略，僅供 demo 跳轉
            if delta < 0:
                delta = 0
            if delta > self.max_duration:
                delta = self.max_duration * 0.95
            return float(delta)
        except Exception:
            return 0.0

    def _jump_to_time(self, target_sec: float):
        """事件/標記點擊跳轉專用：
        將標記的「開始時間」置於紅線（視窗中心）位置，
        而不是放在查看區域的開始時間（左邊緣）。
        這樣紅線（及各通道的對應細紅虛線）會正好對準該標記，方便對照。
        其他時間導航（slider、spin、滾輪、概觀拖曳）維持原來「紅線為目前視窗中心」的行為。
        """
        if not self.max_duration or self.time_duration <= 0:
            return
        half = self.time_duration / 2.0
        desired_start = target_sec - half
        self.time_start = max(0.0, min(desired_start, self.max_duration - self.time_duration))
        self._update_time_controls()
        self._update_overview_region()
        self._update_view()
        if not getattr(self, '_loading_rec', False) and not getattr(self, '_updating_view', False):
            self._save_time_for_current()

    def _on_event_row_activated(self, index):
        """雙擊事件列：用該列事件的 rel_start（精確相對原點秒數）跳轉並置中。
        虛擬化模型直接給秒數，無需從顯示字串反解析（也修掉舊版把絕對時間誤當相對秒的問題）。
        """
        if not index or not index.isValid():
            return
        src = self.events_proxy.mapToSource(index)
        rel = self.events_model.rel_start_at(src.row())
        if rel is not None and self.max_duration > 0:
            self._jump_to_time(rel)

    # ==================== 波形檢視 (pyqtgraph 核心) ====================

    def _init_signal_plots(self):
        # 停止任何進行中的逐步載入（避免切換通道時多個 timer 同時跑）
        if getattr(self, '_prog_timer', None):
            self._prog_timer.stop()
            self._prog_timer = None
        self._prog_index = 0
        # 停止 debounce timer
        if getattr(self, '_update_debounce_timer', None):
            self._update_debounce_timer.stop()

        # 清空舊的 (只清 signals 區，overview 固定保留)
        if hasattr(self, 'signal_container') and self.signal_container:
            self.signal_container.clear()
        self.plot_items = []
        self.curves = []
        self.event_lines = []
        self.event_marker_items = []
        self.event_overlay_regions = []
        self.event_label_items = []
        self.position_renderers = {}
        self.plot_red_lines = []  # 重建時清空舊的紅線

        if not self.current_rec or not self.visible_channels:
            # 放一個提示在 signals 區
            # placeholder 也用自定義 axis
            try:
                p_time_axis = TimeAxisItem(self, orientation='bottom')
                p = self.signal_container.addPlot(axisItems={'bottom': p_time_axis})
            except Exception:
                p = self.signal_container.addPlot()
            p.getAxis("left").setWidth(65)
            p.setTitle("請勾選通道")
            self.plot_items.append(p)
            return

        n = len(self.visible_channels)
        for i, ch_name in enumerate(self.visible_channels):
            # 使用自定義 TimeAxisItem，只改標籤顯示格式 (時分秒/秒數 + 毫秒)，tick 間隔保持預設
            try:
                p_time_axis = TimeAxisItem(self, orientation='bottom')
                p_time_axis.setStyle(tickFont=pg.QtGui.QFont("Microsoft JhengHei", 7), tickLength=2, tickTextOffset=1)
                p = self.signal_container.addPlot(row=i, col=0, axisItems={'bottom': p_time_axis})
            except Exception:
                p = self.signal_container.addPlot(row=i, col=0)
            # 移除上方的標題label（依使用者要求，取消此佔位與關閉icon）。通道名稱由左軸 y label 顯示。
            # 寫死固定左軸寬度，確保所有通道波形區域寬度一致（專業軟體常見做法）
            p.getAxis("left").setWidth(65)
            p.showGrid(x=True, y=True)
            h = getattr(self, 'desired_channel_height', 200)
            hh = max(28, int(h))
            p.setMinimumHeight(hh)
            p.setMaximumHeight(hh)  # 鎖定 row 高度，避免資料載入後的自動計算高度覆蓋使用者的設定 (sync 問題)
            p.setContentsMargins(3, 1, 3, 1)
            p._nodata_item = None  # 供無資料指示用

            # 左軸顯示通道名稱或單位。所有通道左邊 y 軸都有名稱（revert 後維持此邏輯以符合要求）。
            short_label = ""
            if "spo2" in ch_name.lower():
                short_label = "SpO2 %"
            elif "heart" in ch_name.lower():
                short_label = "bpm"
            elif "flow" in ch_name.lower():
                short_label = "flow"
            elif "position" in ch_name.lower():
                short_label = ""  # position 用 renderer y-ticks

            left_label = short_label if short_label else ch_name
            if len(left_label) > 12:
                left_label = left_label[:10] + "…"
            p.setLabel("left", left_label, **{'offset': (0, 0)})
            # 左軸寬度已固定為 65，波形顯示區域寬度統一。
            # 標籤受版面寬度截斷時，hover 左軸可看到完整通道名稱（醫療辨識需求，避免名稱看不全）。
            try:
                left_axis = p.getAxis("left")
                left_axis.setToolTip(ch_name)
            except Exception:
                pass

            p.setMenuEnabled(False)  # 移除預設 UI 裝飾，減少左上角重疊

            # 簡潔的滑鼠滾輪提示（依使用者要求：去除廢話與「已修復」自我說明）
            p.setToolTip("使用滑鼠滾輪調整所有顯示通道的時間顯示範圍")

            if i < n - 1:
                p.setXLink(self.plot_items[0] if self.plot_items else None)  # 連動 X (signals 之間)

            if self.current_rec and self.current_rec.is_position_channel(ch_name):
                # PR2: position 使用專用 renderer (step_fill default) 而非 generic curve
                r = self._create_position_curve(p, ch_name)
                self.position_renderers[i] = r
                self.curves.append(None)
                self.plot_items.append(p)
                # 右鍵提供「Fit Y」
                try:
                    vb = p.getViewBox()
                    if hasattr(vb, 'menu') and vb.menu is not None:
                        vb.menu.addSeparator()
                        act_fit = vb.menu.addAction("Fit Y 軸")
                        act_fit.triggered.connect(lambda checked=False, idx=i: self._fit_y_for_plot(idx))
                        act_hide = vb.menu.addAction("隱藏通道")
                        act_hide.triggered.connect(lambda checked=False, name=ch_name: self._hide_channel(name))
                except Exception:
                    pass
            else:
                # 固定預設樣式（已取消樣式編輯功能）
                curve = p.plot(pen=pg.mkPen(color='#1f77b4', width=1.0))
                # 效能：peak 降採樣 + clipToView。大視窗（Fit 全長 = 數百萬點）時只繪製
                # 可視範圍、且以每像素 min/max 抽稀 → 速度大增且「不漏任何 spike」（醫療要求）。
                try:
                    curve.setDownsampling(auto=True, method='peak')
                    curve.setClipToView(True)
                except Exception:
                    pass
                self.curves.append(curve)
                self.position_renderers[i] = None
                self.plot_items.append(p)
                # 右鍵提供「Fit Y」
                try:
                    vb = p.getViewBox()
                    if hasattr(vb, 'menu') and vb.menu is not None:
                        vb.menu.addSeparator()
                        act_fit = vb.menu.addAction("Fit Y 軸")
                        act_fit.triggered.connect(lambda checked=False, idx=i: self._fit_y_for_plot(idx))
                        act_hide = vb.menu.addAction("隱藏通道")
                        act_hide.triggered.connect(lambda checked=False, name=ch_name: self._hide_channel(name))
                except Exception:
                    pass

            # (事件 markers 由 _add_event_markers_to_plots 依 by-channel 加入，取代舊 placeholder)

            # 為每個詳細通道 plot 加入細紅虛線，與概觀軸的紅線同步位置（中心時間），方便對照目前視窗在各波形上的位置
            # 使用 dashed 細線 (width=1)，zValue 適中不遮主資料
            try:
                red = pg.InfiniteLine(pos=0, angle=90,
                    pen=pg.mkPen('#d62728', width=1, style=Qt.PenStyle.DashLine))
                red.setZValue(45)
                p.addItem(red)
                self.plot_red_lines.append(red)
            except Exception:
                self.plot_red_lines.append(None)

        # 詳細 plots 之間 X 軸已連動（在 per-plot loop 中 setXLink 到第一個）
        # overview 必須獨立保持 0 ~ max_duration 100% 寬度，不與下方任何軸同步 scale
        # 藍色 region 只用來視覺指示「目前下方瀏覽的時間範圍在整體錄音的哪一段」

        # 關鍵修復：將 sigRangeChanged 連接到「所有」plot_items（含上方第一個與下方所有）
        # 之前只連 [0]，導致「滑鼠滾輪只在上方圖表有效，下方滾動不會同步所有，造成上下不一致」
        # 現在任何圖表上的 wheel / 拖曳 range change 都會呼叫 handler -> 更新 time state -> _update_view 強制所有 plot 同步
        # 同時也連 overview，讓概觀軸滾輪也能驅動同步
        for p in self.plot_items:
            try:
                vb = p.getViewBox()
                vb.sigRangeChanged.disconnect(self._on_plot_range_changed)
            except Exception:
                pass
            try:
                p.getViewBox().sigRangeChanged.connect(self._on_plot_range_changed)
            except Exception:
                pass
        # 不要連 overview 的 range changed 到 _on_plot_range_changed（那是給下方詳細 plot 用的）
        # overview 保持固定全寬度，由 region item 控制視窗表示

        # 更新 overview region 邊界
        if self.overview_region and self.plot_items:
            self.overview_region.setBounds([0, self.max_duration])

        # 初始同步紅線到目前中心（新建立的 red lines 預設 pos=0，需設正確值）
        self._sync_red_markers()

        # 為 signals 區的 GraphicsLayout 請求足夠高度，讓外層 QScrollArea 正確顯示垂直滾動條
        # (選擇太多通道時可獨立捲動詳細波形，概觀軸固定在上方不動)
        # 專業排版：根據通道數動態計算，避免裁切或無捲動
        if self.plot_items:
            h = getattr(self, 'desired_channel_height', 200)
            approx_height = 12 + len(self.plot_items) * (max(28, int(h)) + 6)
            self.signals_pg.setMinimumHeight(approx_height)
            self.signals_pg.updateGeometry()
            if hasattr(self, 'signal_scroll') and self.signal_scroll:
                self.signal_scroll.updateGeometry()

        # issue1: 加入 by-channel event markers (依 location 匹配的 channel 會有綠色粗虛線 + 半透明區間)
        if self.selected_event_channels:
            self._add_event_markers_to_plots()
            if getattr(self, 'show_event_background_overlay', False):
                self._add_event_time_overlays()
        self._refresh_all_time_axes()

    # ==================== 逐步載入（修復大量通道全選卡死 + 垂直捲動） ====================
    def _start_progressive_update(self):
        """開始逐步載入目前 visible_channels 的資料。
        每批只處理少量通道，透過 QTimer 讓出事件迴圈，UI 保持可操作。
        主要解決「全選」時一次 create 50+ PlotItem + get_data + setData 導致卡死。
        """
        if getattr(self, '_prog_timer', None):
            self._prog_timer.stop()
        self._prog_index = 0
        self._prog_timer = QTimer(self)
        self._prog_timer.timeout.connect(self._load_next_data_batch)
        # 每 8-15ms 處理一小批，給 UI 事件機會處理（拖曳、捲動、點擊）
        self._prog_timer.start(10)
        n = len(getattr(self, 'visible_channels', []))
        if n > 0:
            self.statusBar().showMessage(f"正在載入通道 0/{n} ...", 0)

    def _read_channel_window(self, ch, t0, dur, strip_header=True):
        """讀取通道在「全域時間窗 [t0, t0+dur]」的資料，並套用該通道的絕對起始 offset。

        各 NDF 通道因感測器分批上線，第一筆 sample 的牆鐘時間不同（D20 實測 spread 達 118s）。
        此處把全域時間換算成 channel-local 秒數讀取，再回報資料第一筆對應的全域 x（x0），
        讓波形擺在絕對時間軸的正確位置；offset=0（EDF 來源 / 無嵌入時間）時行為與舊版完全一致。

        回傳 (data, fs, x0)：x0 = max(t0, offset)，即資料第一筆的全域秒數。
        若整個視窗落在通道起始之前，回傳空資料。
        """
        rec = self.current_rec
        try:
            offset = float(rec.channel_offset_sec(ch))
        except Exception:
            offset = 0.0
        local_t0 = t0 - offset
        read_t0 = local_t0 if local_t0 > 0 else 0.0
        read_dur = dur - (read_t0 - local_t0)  # 視窗前緣若早於通道起始則縮短
        if read_dur <= 0:
            return np.array([]), 1.0, t0
        try:
            data, fs = rec.get_data(ch, read_t0, read_dur, strip_header=strip_header)
        except Exception:
            return np.array([]), 1.0, t0
        x0 = read_t0 + offset  # = max(t0, offset)
        return data, fs, x0

    def _load_next_data_batch(self):
        """每批載入 BATCH_SIZE 個通道的資料（get_data + 設定 curve / renderer）。"""
        if not self.current_rec or not getattr(self, 'visible_channels', None):
            self._stop_progressive_load()
            return

        n = len(self.visible_channels)
        if self._prog_index >= n:
            self._stop_progressive_load()
            self.statusBar().showMessage("載入完成", 2500)
            return

        BATCH_SIZE = 3   # 每批 3 個，平衡速度與回應性（可依實際測試調整 2~5）
        end = min(self._prog_index + BATCH_SIZE, n)

        loaded_any = False

        for i in range(self._prog_index, end):
            ch = self.visible_channels[i]
            try:
                data, fs, x0 = self._read_channel_window(ch, self.time_start, self.time_duration)
            except Exception:
                data = np.array([])
                fs = 1.0
                x0 = self.time_start

            # per-channel 實際 dur + 絕對起始 offset → 通道全域結束 = offset + ch_dur
            ch_dur = self.max_duration
            try:
                for c in (getattr(self.current_rec, 'channels', None) or []):
                    if c.get('name') == ch:
                        ch_dur = float(c.get('duration_sec') or ch_dur)
                        break
            except Exception:
                pass
            try:
                ch_global_end = float(self.current_rec.channel_offset_sec(ch)) + ch_dur
            except Exception:
                ch_global_end = ch_dur

            if len(data) == 0:
                x = np.array([])
                y = np.array([])  # 無資料時 empty，避免畫 flat 0 線
            else:
                got_dur = len(data) / max(fs or 1.0, 1e-9)
                got_end = x0 + got_dur
                if got_end > ch_global_end + 0.1:
                    keep_n = max(0, int( (ch_global_end - x0) * (fs or 1.0) ))
                    data = data[:keep_n]
                    got_dur = len(data) / max(fs or 1.0, 1e-9) if len(data) > 0 else 0.0
                x = np.linspace(x0, x0 + got_dur, len(data), endpoint=False) if len(data) > 0 else np.array([])
                y = data
                if "spo2" in ch.lower() and np.nanmax(np.abs(y)) > 200:
                    y = y / 10.0
                # 清理 inf/nan (snore 等可能有極端值)
                y = np.asarray(y, dtype=float)
                y = np.where(np.isfinite(y), y, 0.0)

            # 設定資料（position 用 renderer，其餘用 curve）
            if (getattr(self, 'position_renderers', None) and
                    i in self.position_renderers and self.position_renderers.get(i)):
                self.position_renderers[i].update(x, y, fs, x0)
            elif i < len(getattr(self, 'curves', [])) and self.curves[i] is not None:
                self.curves[i].setData(x, y, antialias=self.antialias_on)
                # 當點數少（放大或稀疏資料）時顯示點標記，讓資料點可見；專業軟體常見做法，避免放大後資料「不見」
                if len(y) > 0 and len(y) < 300:
                    self.curves[i].setSymbol('o')
                    self.curves[i].setSymbolSize(3)
                    self.curves[i].setSymbolBrush(pg.mkBrush(0, 0, 139))
                    self.curves[i].setSymbolPen(None)
                else:
                    self.curves[i].setSymbol(None)
                if len(y) > 0:
                    ymin, ymax = self._get_robust_y_limits(y, ch)
                    if i < len(self.plot_items):
                        self.plot_items[i].setYRange(ymin, ymax, padding=0)

            # 無資料指示（僅完全無資料時顯示，與主路徑一致）
            if i < len(self.plot_items):
                p = self.plot_items[i]
                if hasattr(p, '_nodata_item') and p._nodata_item is not None:
                    try: p.removeItem(p._nodata_item)
                    except: pass
                    p._nodata_item = None
                # 只在通道完全無資料時顯示提示（移除 partial 就提示的舊邏輯）
                has_missing = (len(data) == 0)
                if has_missing:
                    txt = pg.TextItem("無此時段資料", color=(180, 0, 0), anchor=(0.5, 0.5))
                    txt.setFont(QFont("Microsoft JhengHei", 8))
                    p.addItem(txt)
                    mid_t = self.time_start + self.time_duration / 2
                    my = 90 if "spo2" in ch.lower() else 0
                    txt.setPos(mid_t, my)
                    p._nodata_item = txt
                    # 強制 range
                    if "spo2" in ch.lower():
                        p.setYRange(80, 100, padding=0)
                    else:
                        p.setYRange(-1, 1, padding=0)

            # (真實事件 markers 在 init 時加入，位置固定；此處不再設假的 center line)

            # 為剛載入的 plot 設定 X range（讓它們跟 overview 一致）
            if i < len(self.plot_items):
                self.plot_items[i].setXRange(self.time_start, self.time_start + self.time_duration, padding=0)

            loaded_any = True
            # 記錄到 cache，供後續 channel 切換或重複 update 時差異/跳過重複讀取
            self._channel_data_cache[ch] = (x, y, fs)

        self._prog_index = end

        if loaded_any:
            self.statusBar().showMessage(
                f"正在逐步載入通道 {self._prog_index}/{n} ...（可拖曳/縮放已載入波形）", 0
            )

        # 下一批（用 singleShot 更乾淨，或繼續靠 timeout）
        # 這裡繼續靠 timer 即可；如果想更精準控制間隔，可在這裡 QTimer.singleShot(5, self._load_next...)
        # 為簡單，timer 繼續觸發下一批

    def _stop_progressive_load(self):
        if getattr(self, '_prog_timer', None):
            self._prog_timer.stop()
            self._prog_timer = None
        self._prog_index = 0
        # 確保 progressive 完成後概觀軸 region 仍正確（特別是初次載入或切換 visible 後）
        self._update_overview_region()

    def _snapshot_current_data(self) -> dict:
        """在 visible 改變前快照目前已載入的顯示資料（x, y, fs），供差異載入時讓「未改變的 channel」直接重用，避免重複 get_data。
        優先從 live curve.getData()，fallback 到 _channel_data_cache。
        只對一般 curve 有效（position 留給選擇性載入）。
        """
        snap = {}
        vis = getattr(self, 'visible_channels', []) or []
        curves = getattr(self, 'curves', []) or []
        cache = getattr(self, '_channel_data_cache', {}) or {}
        for i, ch in enumerate(vis):
            got = False
            if i < len(curves) and curves[i] is not None:
                try:
                    x, y = curves[i].getData()
                    if x is not None and len(x) > 0:
                        snap[ch] = (np.asarray(x), np.asarray(y), 1.0)
                        got = True
                except Exception:
                    pass
            if not got and ch in cache:
                x, y, fs = cache[ch]
                snap[ch] = (np.asarray(x), np.asarray(y), fs or 1.0)
        return snap

    def _load_channels_data(self, ch_list: list[str]):
        """僅針對指定的新增/需載入 channel 執行 get_data + 設定 curve/renderer（差異載入核心）。
        避免全量重新走 progressive。
        """
        if not ch_list or not self.current_rec:
            return
        loaded = 0
        for ch in ch_list:
            if ch not in self.visible_channels:
                continue
            try:
                i = self.visible_channels.index(ch)
            except ValueError:
                continue
            try:
                data, fs, x0 = self._read_channel_window(ch, self.time_start, self.time_duration)
            except Exception:
                data = np.array([])
                fs = 1.0
                x0 = self.time_start
            if len(data) == 0:
                x = np.array([])
                y = np.array([])  # 無資料時 empty，避免畫 flat 0 線
            else:
                got_dur = len(data) / max(fs or 1.0, 1e-9)
                x = np.linspace(x0, x0 + got_dur, len(data), endpoint=False)
                y = data
                if "spo2" in ch.lower() and np.nanmax(np.abs(y)) > 200:
                    y = y / 10.0  # 使 SpO2 數據範圍對應 % (約 0-100)，以匹配 label 與無資料時的 y-range，避免顯示 scale 異常

            if (getattr(self, 'position_renderers', None) and
                    i in self.position_renderers and self.position_renderers.get(i)):
                self.position_renderers[i].update(x, y, fs, x0)
            elif i < len(getattr(self, 'curves', [])) and self.curves[i] is not None:
                self.curves[i].setData(x, y, antialias=self.antialias_on)
                # 當點數少（放大或稀疏資料）時顯示點標記，讓資料點可見；專業軟體常見做法，避免放大後資料「不見」
                if len(y) > 0 and len(y) < 300:
                    self.curves[i].setSymbol('o')
                    self.curves[i].setSymbolSize(3)
                    self.curves[i].setSymbolBrush(pg.mkBrush(0, 0, 139))
                    self.curves[i].setSymbolPen(None)
                else:
                    self.curves[i].setSymbol(None)
                if len(y) > 0:
                    ymin, ymax = self._get_robust_y_limits(y, ch)
                    if i < len(self.plot_items):
                        self.plot_items[i].setYRange(ymin, ymax, padding=0)

            # 無資料指示：僅當此通道在目前視窗完全無資料時才顯示
            has_missing = (len(data) == 0)
            if has_missing and i < len(self.plot_items):
                p = self.plot_items[i]
                if hasattr(p, '_nodata_item') and p._nodata_item is not None:
                    try: p.removeItem(p._nodata_item)
                    except: pass
                    p._nodata_item = None
                txt = pg.TextItem("無此時段資料", color=(180, 0, 0), anchor=(0.5, 0.5))
                txt.setFont(QFont("Microsoft JhengHei", 8))
                p.addItem(txt)
                mid_t = self.time_start + self.time_duration / 2
                my = 90 if "spo2" in ch.lower() else 0
                txt.setPos(mid_t, my)
                p._nodata_item = txt
                if "spo2" in ch.lower():
                    p.setYRange(80, 100, padding=0)
                else:
                    p.setYRange(-1, 1, padding=0)

            if i < len(self.plot_items):
                self.plot_items[i].setXRange(self.time_start, self.time_start + self.time_duration, padding=0)
            loaded += 1
            self._channel_data_cache[ch] = (x, y, fs)

        if loaded:
            self.statusBar().showMessage(f"已載入 {loaded} 個通道", 1800)

    def _update_view(self, immediate: bool = False):
        """公開的更新入口。預設做 debounce（高頻 wheel/drag 只在停頓後執行一次完整更新，避免卡頓）。
        明確需要立即重繪的場合（如按鈕 Fit）可傳 immediate=True。
        """
        if immediate:
            self._perform_update_view()
            return
        # 合併快速連續的 range 改變（典型 每事件 10-30ms）
        if self._update_debounce_timer.isActive():
            self._update_debounce_timer.stop()
        self._update_debounce_timer.start(45)

    def _perform_update_view(self):
        """實際執行資料重讀 + 畫面更新（原 _update_view 內容）。"""
        if self._updating_view or not self.current_rec or not self.visible_channels:
            return
        self._updating_view = True

        try:
            n = len(self.visible_channels)

            for i in range(min(n, len(self.plot_items))):
                ch = self.visible_channels[i]
                try:
                    data, fs, x0 = self._read_channel_window(ch, self.time_start, self.time_duration)
                except Exception:
                    data = np.array([])
                    fs = 1.0
                    x0 = self.time_start

                # 取得此通道的實際資料長度（重要：部分通道如 oximeter 衍生可能短於總長）
                # 並加上絕對起始 offset → 通道全域結束 = offset + ch_dur
                ch_dur = self.max_duration
                try:
                    for c in (getattr(self.current_rec, 'channels', None) or []):
                        if c.get('name') == ch:
                            ch_dur = float(c.get('duration_sec') or ch_dur)
                            break
                except Exception:
                    pass
                try:
                    ch_global_end = float(self.current_rec.channel_offset_sec(ch)) + ch_dur
                except Exception:
                    ch_global_end = ch_dur

                if len(data) > 0:
                    got_dur = len(data) / max(fs or 1.0, 1e-9)
                    got_end = x0 + got_dur
                    # trim 到通道實際可得範圍（以全域結束為界），避免 reader 回傳超出或我們拉伸
                    if got_end > ch_global_end + 0.1:
                        keep_n = max(0, int( (ch_global_end - x0) * (fs or 1.0) ))
                        data = data[:keep_n]
                        got_dur = len(data) / max(fs or 1.0, 1e-9) if len(data) > 0 else 0.0
                    x = np.linspace(x0, x0 + got_dur, len(data), endpoint=False) if len(data) > 0 else np.array([])
                    y = data
                    if "spo2" in ch.lower():
                        y = y / 10.0
                    # 清理 inf / nan，避免 snore 或其他通道極端值導致 y-range 爆炸、超出畫面或 label 異常
                    y = np.asarray(y, dtype=float)
                    y = np.where(np.isfinite(y), y, 0.0)
                else:
                    x = np.array([])
                    y = np.array([])

                # 記錄快照，供後續差異載入或重複 _update_view 時跳過重複讀檔
                self._channel_data_cache[ch] = (x, y, fs)

                # PR2: position 用 renderer (windowed step/fill/labels)；其他維持原 line
                if i in getattr(self, 'position_renderers', {}) and self.position_renderers.get(i):
                    self.position_renderers[i].update(x, y, fs, x0)
                elif i < len(self.curves) and self.curves[i] is not None:
                    self.curves[i].setData(x, y, antialias=self.antialias_on)
                    # 當點數少（放大或稀疏資料）時顯示點標記，讓資料點可見；專業軟體常見做法，避免放大後資料「不見」
                    if len(y) > 0 and len(y) < 300:
                        self.curves[i].setSymbol('o')
                        self.curves[i].setSymbolSize(3)
                        self.curves[i].setSymbolBrush(pg.mkBrush(0, 0, 139))
                        self.curves[i].setSymbolPen(None)
                    else:
                        self.curves[i].setSymbol(None)

                # 無資料時顯示清楚指示（避免 flat 0 + 怪異 x0.001 label）
                if i < len(self.plot_items):
                    p = self.plot_items[i]
                    if hasattr(p, '_nodata_item') and p._nodata_item is not None:
                        try:
                            p.removeItem(p._nodata_item)
                        except Exception:
                            pass
                        p._nodata_item = None
                    # 只在當前通道在此視窗「完全沒有任何資料」時才顯示「無此時段資料」。
                    # 移除舊的「部分涵蓋就顯示」的邏輯（x[-1] < view_end），避免 90% 有資料卻提示無資料。
                    has_missing = (len(data) == 0)
                    if has_missing:
                        txt = pg.TextItem("無此時段資料", color=(180, 0, 0), anchor=(0.5, 0.5))
                        txt.setFont(QFont("Microsoft JhengHei", 8))
                        p.addItem(txt)
                        mid_t = self.time_start + self.time_duration / 2
                        my = 90 if "spo2" in ch.lower() else 0
                        txt.setPos(mid_t, my)
                        p._nodata_item = txt

                # 縮放時讓數據完整可見：為目前視窗的數據自動調整 Y 範圍（padding 避免貼邊）
                # 這樣用戶縮放時間後，該通道在視窗內的數據總是完整填滿 plot 高度
                # position 已在 renderer 中設定離散範圍
                if not (i in getattr(self, 'position_renderers', {}) and self.position_renderers.get(i)):
                    if len(y) > 0:
                        try:
                            ymin, ymax = self._get_robust_y_limits(y, ch)
                            if i < len(self.plot_items):
                                self.plot_items[i].setYRange(ymin, ymax, padding=0)
                        except Exception:
                            pass
                    else:
                        # 無此時段資料（通道在此視窗完全無資料點）
                        # 強制合理 Y 範圍，避免 pyqtgraph 因 range 極小 (zeros) 而在 label 顯示 x0.001 或類似，造成「太小看不見」與標籤異常
                        if "spo2" in ch.lower():
                            ymin, ymax = 80.0, 100.0
                        else:
                            ymin, ymax = -1.0, 1.0
                        pad = 0.5
                        if i < len(self.plot_items):
                            self.plot_items[i].setYRange(ymin - pad, ymax + pad, padding=0)
                        # 不設 curve data (上面已確保 empty)，只保留軸與 grid，讓用戶知道這個 channel 在此時段無波形
                        # 必要時可在此加 p.addItem( TextItem("無此時段資料", color='r', anchor=(0.5,0.5)) ) 但先以正確 range 為主

                # (事件 markers 採可視範圍渲染，於下方依目前視窗重繪)

            # 更新 overview region (淡藍色蒙層) + 目前位置紅線
            # 確保與下方詳細波形的時間視窗完全同步
            self._update_overview_region()

            # 更新 plot X range
            if self.plot_items:
                for p in self.plot_items:
                    p.setXRange(self.time_start, self.time_start + self.time_duration, padding=0)
                self.overview_plot.setYRange(0, 1, padding=0)
            self._refresh_all_time_axes()

            # 可視範圍事件標記：依目前視窗重繪（只畫視窗內標記，大量事件時平移仍順）。
            # 事件表不受影響仍顯示全部。
            if getattr(self, '_events_loaded', False) and getattr(self, 'selected_event_channels', None):
                self._clear_event_visuals()
                self._add_event_markers_to_plots()
                if getattr(self, 'show_event_background_overlay', False):
                    self._add_event_time_overlays()

        finally:
            self._updating_view = False

    def _on_plot_range_changed(self, vb, ranges, *extra):
        """支援 pyqtgraph ViewBox.sigRangeChanged 實際 emit 3 參數 (vb, ranges, [bool])。
        *extra 吸收第 3 參數，避免任何版本相容問題。之前只連第一個 plot，現在全部都連。
        """
        if self._updating_view:
            return
        x_range = ranges[0]
        new_start = x_range[0]
        new_dur = x_range[1] - x_range[0]
        if new_dur < 0.5:
            new_dur = 0.5
        self.time_start = max(0, min(new_start, self.max_duration - new_dur))
        self.time_duration = min(new_dur, self.max_duration)
        self._update_time_controls()
        self._update_overview_region()
        if not getattr(self, '_loading_rec', False) and not getattr(self, '_updating_view', False):
            self._save_time_for_current()  # PR4: 記住最後時間視窗（per-rec，含滑鼠拖曳縮放）
        # 用戶在 plot 上直接縮放/平移時，重新載入該視窗數據並自動 Y fit，讓數據完整可見
        self._update_view()

    def _on_overview_region_changed(self):
        if self._updating_view or not self.overview_region:
            return
        r = self.overview_region.getRegion()
        self.time_start = max(0, r[0])
        self.time_duration = max(1, r[1] - r[0])
        self._update_time_controls()
        # 立即更新 region（在 throttle 前），確保視覺即時
        self._update_overview_region()
        # 為了拖曳時即時同步下方詳細波形，使用 throttle + immediate 更新（約 30fps 上限）
        # 避免高頻全量重載導致卡頓，同時讓拖曳蒙層時感覺流暢同步
        now = time.time()
        if not hasattr(self, '_last_overview_update') or now - self._last_overview_update > 0.033:
            self._last_overview_update = now
            self._update_view(immediate=True)  # 直接執行，不 debounce，確保即時
        else:
            self._update_view()  # 常規 debounce 路徑
        if not getattr(self, '_loading_rec', False) and not getattr(self, '_updating_view', False):
            self._save_time_for_current()  # PR4: 記住最後時間視窗（per-rec）

    def _force_overview_xrange(self, vb, ranges):
        """強制鎖定概觀軸 x 範圍為 [0, max_duration] 全寬度，防止任何操作改變其 scale。
        這樣概觀軸永遠顯示 100% 時間軸，藍色 region 只負責指示下方目前視窗的位置。
        """
        if self._setting_overview_x or not self.overview_plot or not self.max_duration:
            return
        current = ranges[0] if ranges else self.overview_plot.getViewBox().viewRange()[0]
        if abs(current[0] - 0) > 0.1 or abs(current[1] - self.max_duration) > 0.1:
            self._setting_overview_x = True
            self._set_overview_xrange()
            try:
                self.overview_plot.update()
            except Exception:
                pass
            self._setting_overview_x = False
            self._refresh_all_time_axes()

    def _update_overview_region(self):
        """確保藍色蒙層和紅線永遠正確反映目前時間視窗，並鎖定概觀軸全寬度。
        調用此函式在所有時間改變處，保證顯示。
        使用 _updating_view flag 防止 setRegion 觸發 sigRegionChanged 導致遞迴。
        """
        if not self.overview_region:
            return
        self._updating_view = True
        try:
            self.overview_region.setRegion([self.time_start, self.time_start + self.time_duration])
            self.overview_region.setVisible(True)
            try:
                self.overview_region.update()
            except Exception:
                pass
        finally:
            self._updating_view = False
        if hasattr(self, 'overview_vline'):
            self.overview_vline.setValue(self.time_start + self.time_duration / 2)
        if self.overview_plot and getattr(self, 'max_duration', 0):
            self._set_overview_xrange()
            if hasattr(self, 'overview_end_time_label'):
                self.overview_end_time_label.setText(self._overview_end_text())
                delta = max(1.0, self.max_duration * 0.005)
                self.overview_end_time_label.setPos(self.max_duration - delta, 0.95)
            try:
                self.overview_plot.update()
            except Exception:
                pass

        # 同步紅線到各通道（在 vline 更新後）
        self._sync_red_markers()

    def _sync_red_markers(self):
        """同步所有詳細 plot 上的細紅虛線位置到概觀軸紅線（目前視窗中心時間）。在 _update_overview_region 及 time 改變處呼叫。"""
        if not getattr(self, 'plot_red_lines', None):
            return
        center = self.time_start + self.time_duration / 2.0 if getattr(self, 'max_duration', 0) else 0.0
        for line in self.plot_red_lines:
            if line is not None:
                try:
                    line.setValue(center)
                except Exception:
                    pass

    def _get_robust_y_limits(self, y, ch_name: str = ""):
        """計算 y 顯示範圍。
        醫療軟體要求完整呈現：全通道一律使用實際 min/max + 小 pad，
        確保任何 spike（含臨床上重要的尖峰）都完整入畫，不做百分位裁切。
        先做 inf/nan 清理。
        """
        yarr = np.asarray(y, dtype=float)
        yarr = yarr[np.isfinite(yarr)]
        if len(yarr) == 0:
            return -1.0, 1.0
        ymin = float(np.min(yarr))
        ymax = float(np.max(yarr))
        dy = ymax - ymin
        # snore / audio 大幅度通道沿用較小 pad；其餘給稍大 pad 留呼吸空間
        ch_lower = ch_name.lower()
        if "snore" in ch_lower or "audio" in ch_lower:
            pad = dy * 0.05 if dy > 0 else 10.0
        else:
            pad = dy * 0.08 if dy > 0 else 0.5
        return ymin - pad, ymax + pad

    def _set_initial_fast_time_window(self):
        """啟動 / 切換錄音時強制僅載入「從記錄絕對開始 (t=0) + 60 秒」的資料範圍。
        不論 prefs 之前儲存了多大的 time_duration 或位於錄音中間的 t_start，
        開啟時永遠只讀取少量資料（~60s x 通道數），解決「一打開就非常非常慢」。
        使用者之後可用概觀軸、滾輪、spin 等自由調整視窗（可大可移到中間），互動後會儲存 prefs。
        下次重新開啟 app 載入同 rec 時 again 只載前 60s（保證快速啟動）。
        """
        self.time_start = 0.0
        md = getattr(self, 'max_duration', 0) or 0
        self.time_duration = min(60.0, md) if md > 0 else 60.0
        self._update_overview_region()

    def _on_slider_changed(self, val):
        if not self.max_duration:
            return
        fraction = val / 1000.0
        self.time_start = fraction * (self.max_duration - self.time_duration)
        self.time_start = max(0, min(self.time_start, self.max_duration - self.time_duration))
        self._update_time_controls()
        self._update_overview_region()
        self._update_view()
        if not getattr(self, '_loading_rec', False) and not getattr(self, '_updating_view', False):
            self._save_time_for_current()  # PR4: 記住最後時間視窗（per-rec）

    def _on_time_spin_changed(self):
        self.time_start = self.start_spin.value()
        self.time_duration = self.dur_spin.value()
        if getattr(self, 'max_duration', 0) > 0:
            self.time_duration = min(self.time_duration, self.max_duration)
        self.time_start = max(0, min(self.time_start, self.max_duration - self.time_duration))
        self._update_overview_region()
        self._update_time_controls()
        self._update_view()
        if not getattr(self, '_loading_rec', False) and not getattr(self, '_updating_view', False):
            self._save_time_for_current()  # PR4: 記住最後時間視窗（per-rec）

    def _update_time_controls(self):
        if not self.max_duration:
            return
        self._updating_view = True
        try:
            # block signals on time widgets to prevent feedback _on_*_changed that would save during programmatic sync (e.g. load/post)
            self.start_spin.blockSignals(True)
            self.dur_spin.blockSignals(True)
            self.pos_slider.blockSignals(True)
            self.start_spin.setMaximum(self.max_duration)
            self.start_spin.setValue(self.time_start)
            if self.max_duration > 0:
                self.dur_spin.setMaximum(self.max_duration)
            self.dur_spin.setValue(min(self.time_duration, self.max_duration or 60.0))
            pos = int((self.time_start / max(1, self.max_duration - self.time_duration)) * 1000) if self.max_duration > self.time_duration else 0
            self.pos_slider.setValue(max(0, min(1000, pos)))
            end_t = self.time_start + self.time_duration
            self.time_label.setText(
                f"視窗: {self._fmt_time_pos(self.time_start, include_ms=False)} - {self._fmt_time_pos(end_t, include_ms=False)}   / 總長 {format_time_label(self.max_duration)}"
            )
            self._update_abs_time_edits()
        finally:
            self.start_spin.blockSignals(False)
            self.dur_spin.blockSignals(False)
            self.pos_slider.blockSignals(False)
            self._updating_view = False

    def _fit_full(self):
        if not self.max_duration:
            return
        self.time_start = 0.0
        self.time_duration = self.max_duration
        self._update_time_controls()
        self._update_overview_region()
        self._update_view()
        if not getattr(self, '_loading_rec', False) and not getattr(self, '_updating_view', False):
            self._save_time_for_current()  # PR4: 記住最後時間視窗（per-rec）

    def _fit_y_for_plot(self, plot_idx: int):
        """PR5: 完整 context menu 支援 - 對單一 plot 執行 Y 軸 Fit（臨床快速檢視該通道振幅）。"""
        if plot_idx < 0 or plot_idx >= len(self.plot_items):
            return
        p = self.plot_items[plot_idx]
        try:
            p.enableAutoRange(axis='y')  # pyqtgraph 標準 Y auto fit
            # 也可 p.getViewBox().autoRange()
        except Exception:
            pass

    # ==================== PR4: prefs save/restore helpers ====================
    def _save_current_prefs(self):
        """在關鍵點（check、time、close、switch 前）呼叫，儲存目前狀態。（已取消 style/scale UI）"""
        if getattr(self, '_updating_view', False) or getattr(self, '_loading_rec', False):
            return
        if not self.current_rec or not getattr(self, "prefs", None):
            return
        rec_name = self.current_rec.name
        try:
            if self.visible_channels:
                self.prefs.save_visible(rec_name, self.visible_channels)
            self.prefs.save_time_window(rec_name, self.time_start, self.time_duration)
            # 已取消 scale UI，無需 save
            self.prefs.save_viz_settings(rec_name, self._viz_settings)
            self.prefs.save_channel_height(self.desired_channel_height)
            # 舊 show_event_overlays 已移除，事件篩選由 selected_event_channels 控制（不持久化，預設無）
        except Exception:
            pass  # 持久化失敗不影響主功能

    def _save_time_for_current(self):
        if getattr(self, '_updating_view', False) or getattr(self, '_loading_rec', False):
            return
        if self.current_rec and getattr(self, "prefs", None):
            self.prefs.save_time_window(self.current_rec.name, self.time_start, self.time_duration)

    def _apply_saved_visible(self, saved: list[str]):
        """套用已儲存的可見通道列表到表格 checks（block signals），並重建 self.visible_channels。
        保護：如果儲存的列表過長（先前「全選」），則退回 DeepNYX 預設通道。
        """
        if not self.channel_table:
            return

        if not saved or len(saved) > 15:
            saved = CLINICAL_DEFAULT_CHANNELS

        self.channel_table.blockSignals(True)
        for row in range(self.channel_table.rowCount()):
            chk_w = self.channel_table.cellWidget(row, 0)
            if chk_w:
                chk = chk_w.findChild(QCheckBox)
                if chk:
                    name = chk.property("channel_name") or ""
                    chk.setChecked(channel_matches_preset(name, saved))
        self.channel_table.blockSignals(False)
        # 重建 visible 列表（類似 _populate 結尾邏輯）
        self.visible_channels = []
        for row in range(self.channel_table.rowCount()):
            chk_w = self.channel_table.cellWidget(row, 0)
            if chk_w:
                chk = chk_w.findChild(QCheckBox)
                if chk and chk.isChecked():
                    name = chk.property("channel_name")
                    if name:
                        self.visible_channels.append(name)

    def _clear_prefs(self):
        """PR4: 清除所有 QSettings 偏好。提供明確 UI 供測試/rollback。清除後重載目前 rec 即用 clinical defaults（DeepNYX + 200px 高度）。"""
        if not getattr(self, "prefs", None):
            return
        reply = QMessageBox.question(
            self,
            "清除偏好設定",
            "確定清除所有已儲存的使用者選擇（可見通道、時間視窗、通道高度等）？\n"
            "清除後將立即套用 clinical defaults（DeepNYX 預設通道 + 每通道高度 200px 等）。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.prefs.clear_all()
            QMessageBox.information(self, "已清除", "偏好已清除")
            if self.current_rec:
                # 重新載入會走無 prefs 分支 → clinical defaults + 表格初始 checks (現在預設 DeepNYX + 200px)
                self._load_recording(self.current_rec)
                if hasattr(self, 'preset_combo'):
                    self.preset_combo.setCurrentIndex(0)  # 明確預設選擇 DeepNYX
            else:
                self.statusBar().showMessage("已清除偏好設定", 5000)

    # ==================== PR2: Position renderer helpers (minimal) ====================

    def _create_position_curve(self, p: pg.PlotItem, ch_name: str):
        """為 position channel 建立專用 renderer（非通用 curve）。預設 step_fill + 參考 labels。"""
        r = PositionStepRenderer(p, ch_name, self._viz_settings)
        return r

    # ==================== 匯出 ====================

    def _export_selected_edf(self):
        if not self.current_rec or not self.visible_channels:
            QMessageBox.warning(self, "無資料", "請先載入錄音並勾選至少一個通道。")
            return

        default_name = f"{self.current_rec.name}_selected_standard.edf"
        out_path, _ = QFileDialog.getSaveFileName(
            self, "儲存標準 EDF", str(Path("output") / default_name), "EDF files (*.edf)"
        )
        if not out_path:
            return

        self.progress.setVisible(True)
        self.progress.setValue(20)
        QApplication.processEvents()

        try:
            out = export_to_standard_edf(
                self.current_rec,
                channel_names=self.visible_channels,
                out_path=out_path,
                max_duration_sec=self.time_duration if self.time_duration < self.max_duration * 0.95 else None
            )
            self.progress.setValue(100)
            QMessageBox.information(
                self, "匯出成功",
                f"已成功產生標準相容 EDF：\n{out}\n\n此檔案可直接用 EDFbrowser、MNE 等工具開啟。"
            )
            self.statusBar().showMessage(f"已匯出：{out}", 8000)
        except Exception as e:
            QMessageBox.critical(self, "匯出失敗", str(e))
        finally:
            self.progress.setVisible(False)

    # ==================== issue2/3: 高度設定 + 小螢幕 UX 控制方法 ====================

    def _on_channel_height_changed(self, val: int):
        self.desired_channel_height = int(val)
        if self.prefs:
            self.prefs.save_channel_height(self.desired_channel_height)
        self._apply_channel_heights()

    def _apply_channel_heights(self):
        """應用目前 desired_channel_height 到現有 plots，不重建資料 (高效)。
        同時設定 min + max 以鎖定高度，確保與 spinbox / desired 完全同步，不被 layout 自動計算覆蓋。
        """
        h = getattr(self, 'desired_channel_height', 70)
        hh = max(30, int(h))
        updated = False
        if getattr(self, 'plot_items', None):
            for p in self.plot_items:
                try:
                    p.setMinimumHeight(hh)
                    p.setMaximumHeight(hh)  # 關鍵：鎖定，避免載入資料後的計算高度勝出
                    p.updateGeometry()
                    updated = True
                except Exception:
                    pass
        n = len(getattr(self, 'visible_channels', [])) or 1
        approx = 12 + n * (hh + 6)
        if getattr(self, 'signals_pg', None):
            try:
                self.signals_pg.setMinimumHeight(approx)
                self.signals_pg.updateGeometry()
                updated = True
            except Exception:
                pass
        if hasattr(self, 'signal_scroll') and self.signal_scroll:
            try:
                self.signal_scroll.updateGeometry()
            except Exception:
                pass
        if updated:
            # 延後強制 layout 刷新，解決 Qt/graphics layout "計算高度" 蓋過使用者輸入的問題
            QTimer.singleShot(0, self._force_plot_layout_update)

    def _force_plot_layout_update(self):
        """強制刷新 plot 區域 layout，確保 height_spin 輸入的值與實際每通道顯示高度完全同步。
        解決選多個 channel 後再調高度時，被內部計算高度覆蓋無法改動的問題。
        同時更新 splitter 以完整同步兩個數據（spin 值 + 實際 plot row 高度 + scroll 內容尺寸）。
        """
        try:
            if getattr(self, 'signals_pg', None):
                self.signals_pg.updateGeometry()
                self.signals_pg.update()
                if hasattr(self.signals_pg, 'layout') and self.signals_pg.layout():
                    self.signals_pg.layout().activate()
            if getattr(self, 'signal_scroll', None):
                self.signal_scroll.updateGeometry()
                self.signal_scroll.update()
            if getattr(self, 'central_split', None):
                self.central_split.updateGeometry()
                self.central_split.update()
        except Exception:
            pass

    # ==================== 新功能1: 事件蒙層 (淺黃色時間段 overlay) 相關方法 ====================

    # 舊 _on_show_events_toggled 已移除，事件顯示現在由左側 channel 多選列表控制

    def _get_filtered_events(self):
        """嚴謹依左側列表選取的 display_channel 過濾事件。
        選取 "snore (18)" 只拿 display_channel == "snore" 的 event。
        選取 "Normal (無匹配)" 或 "B-snoring (匹配:snore)" 拿對應 virtual 獨立 event。
        左列表的真實 channel 與 (無匹配)/(匹配:xxx) 兩種 virtual 完全獨立可控。
        「在所有通道顯示」chk 只影響 real channel 標記是否強制畫到所有 plot，不影響此過濾與表格內容。
        """
        selected = getattr(self, 'selected_event_channels', set())
        if not selected:
            return []
        parsed = getattr(self, 'parsed_events', [])
        # 記憶化：C（可視範圍標記）讓本函式於每次平移被呼叫，避免每次重掃上萬筆。
        # key 隨選取集合 / parsed_events 物件或長度變動而失效。
        cache_key = (frozenset(selected), id(parsed), len(parsed))
        cache = getattr(self, '_filtered_events_cache', None)
        if cache is not None and cache[0] == cache_key:
            return cache[1]
        filtered = []
        seen = set()
        for ev in parsed:
            dc = ev.get('display_channel')
            if dc and dc in selected:
                key = (ev.get('rel_start'), ev.get('type'), ev.get('location', ''))
                if key not in seen:
                    filtered.append(ev)
                    seen.add(key)
        self._filtered_events_cache = (cache_key, filtered)
        return filtered

    def _events_in_view(self, events):
        """只保留與目前時間視窗相交的事件（含一個窗寬邊距），供波形標記/蒙層的可視範圍渲染。
        事件表仍顯示全部；此處只是減少 scene 上的 Qt 物件數，讓平移/縮放更順。
        平移/縮放停頓後由 _perform_update_view 觸發重繪，邊距確保邊緣標記已就緒。
        """
        md = getattr(self, 'max_duration', 0) or 0
        if not md or not events:
            return events
        vs = self.time_start
        ve = self.time_start + self.time_duration
        margin = self.time_duration  # 視窗外保留一個窗寬
        lo, hi = vs - margin, ve + margin
        out = []
        for ev in events:
            rs = ev.get('rel_start')
            if rs is None:
                continue
            re_ = ev.get('rel_end')
            end = re_ if re_ is not None else rs
            if end >= lo and rs <= hi:
                out.append(ev)
        return out

    def _clear_event_visuals(self):
        """清除目前波形上的事件標記與蒙層（含文字標籤）。"""
        for item in getattr(self, 'event_marker_items', []) + getattr(self, 'event_overlay_regions', []) + getattr(self, 'event_label_items', []):
            for p in getattr(self, 'plot_items', []) or []:
                try:
                    p.removeItem(item)
                except Exception:
                    pass
        self.event_marker_items = []
        self.event_overlay_regions = []
        self.event_label_items = []

    def _clear_event_overlays(self):
        """只清除事件背景黃色蒙層 (LinearRegionItem)，保留垂直事件標記線。"""
        for item in getattr(self, 'event_overlay_regions', []):
            for p in getattr(self, 'plot_items', []) or []:
                try:
                    p.removeItem(item)
                except Exception:
                    pass
        self.event_overlay_regions = []

    def _clear_event_labels(self):
        """只清除事件標記上的小文字標籤 (TextItem)，保留垂直線與背景蒙層。"""
        for item in getattr(self, 'event_label_items', []):
            for p in getattr(self, 'plot_items', []) or []:
                try:
                    p.removeItem(item)
                except Exception:
                    pass
        self.event_label_items = []

    def _apply_time_ticks_to_axis(self, axis, start: float, end: float, is_overview: bool = False):
        """非概觀軸保持預設 tick 間隔，只靠自定義 AxisItem 的 tickStrings 改變標籤文字格式。
        （原概觀軸強制只顯示 0 與結束標籤的邏輯已取消，改為在右側區域另顯示結束時間數字）
        """
        # 詳細通道：不動 tick 位置，讓預設間隔生效
        pass

    def _set_overview_xrange(self):
        """鎖定概觀軸 x 範圍為 0 ~ max_duration 全寬（不再依賴軸 label 顯示結束時間，改由獨立 label 顯示）。"""
        if not getattr(self, 'overview_plot', None) or getattr(self, 'max_duration', 0) <= 0:
            return
        self.overview_plot.setXRange(0, self.max_duration, padding=0)
        self.overview_plot.setYRange(0, 1, padding=0)

    def _refresh_all_time_axes(self):
        """刷新概觀軸與所有詳細通道的底部 X 軸時間顯示。"""
        if getattr(self, 'overview_plot', None):
            ax = getattr(self, 'overview_time_axis', None) or self.overview_plot.getAxis('bottom')
            try:
                self._apply_time_ticks_to_axis(ax, 0.0, self.max_duration, is_overview=True)
            except Exception:
                pass
        # 詳細通道：不動間隔，只靠 tickStrings 格式化標籤
        for p in getattr(self, 'plot_items', []) or []:
            if p:
                try:
                    ax = p.getAxis('bottom')
                    self._apply_time_ticks_to_axis(ax, self.time_start, self.time_start + self.time_duration, is_overview=False)
                except Exception:
                    pass

        # 概觀軸右上角結束時間數字（使用 TextItem 在相同 plot container，字體與通道 x 軸一致，總是完整 ms，顯示在最前面）
        if hasattr(self, 'overview_end_time_label') and getattr(self, 'max_duration', 0) > 0:
            self.overview_end_time_label.setText(self._overview_end_text())
            delta = max(1.0, self.max_duration * 0.005)
            self.overview_end_time_label.setPos(self.max_duration - delta, 0.95)

    def _on_toggle_event_overlay(self, checked):
        self.show_event_background_overlay = bool(checked)
        if self.show_event_background_overlay:
            if getattr(self, 'selected_event_channels', set()):
                self._add_event_time_overlays()
        else:
            self._clear_event_overlays()

    def _on_toggle_antialias(self, checked):
        """切換波形抗鋸齒。關閉可在密集波形/大量通道時明顯加速繪製。
        即時套用：更新全域設定（供新建曲線）+ 重繪目前曲線（setData 會帶入新的 antialias）。
        """
        self.antialias_on = bool(checked)
        try:
            pg.setConfigOptions(antialias=self.antialias_on)
        except Exception:
            pass
        # 立即重繪：_perform_update_view 的 setData 會帶入 antialias=self.antialias_on
        self._update_view(immediate=True)

    def _on_toggle_event_text_labels(self, checked):
        """切換事件 marker 上的小文字標籤顯示（type @ location）。
        預設關閉，避免畫面太亂。放在檢視選單「事件預設背景蒙層」下方。
        """
        self.show_event_text_labels = bool(checked)
        # 為求簡單可靠，直接清掉所有事件視覺元素後重繪（線條+條件式標籤+蒙層）。
        self._clear_event_visuals()
        if getattr(self, '_events_loaded', False) and getattr(self, 'selected_event_channels', None):
            self._add_event_markers_to_plots()
            if getattr(self, 'show_event_background_overlay', False):
                self._add_event_time_overlays()

    def _on_x_time_format_changed(self, fmt):
        self.x_axis_time_format = fmt
        self._refresh_all_time_axes()
        # 強制詳細通道 bottom axis 重新計算 tickStrings (預設間隔不變，只改標籤)
        for p in getattr(self, 'plot_items', []) or []:
            if p:
                try:
                    ax = p.getAxis('bottom')
                    # force regenerate axis picture so tickStrings is re-evaluated with new flags
                    if hasattr(ax, 'picture'):
                        ax.picture = None
                    ax.update()
                    p.update()
                except Exception:
                    pass
        if getattr(self, 'overview_plot', None):
            try:
                self.overview_plot.update()
            except Exception:
                pass

    def _on_toggle_show_ms(self, checked):
        self.x_axis_show_milliseconds = bool(checked)
        self._relabel_time_axes()

    def _on_toggle_absolute_time(self, checked):
        """切換 X 軸絕對牆鐘時間 / 相對經過時間。只改顯示文字，不影響資料座標與 event 位置（rel_start 不變）。"""
        self.x_axis_absolute_time = bool(checked)
        self._relabel_time_axes()
        # 同步刷新文字型時間顯示（狀態列視窗範圍、事件表格起訖欄）以對應新模式
        try:
            self._update_time_controls()
        except Exception:
            pass
        try:
            if getattr(self, 'events_table', None) is not None and getattr(self, '_events_loaded', False):
                self._populate_events_table()
        except Exception:
            pass

    def _relabel_time_axes(self):
        """強制所有詳細通道底部 X 軸與概觀軸重算 tickStrings（標籤刷新，tick 間隔與資料座標不變）。"""
        self._refresh_all_time_axes()
        for p in getattr(self, 'plot_items', []) or []:
            if p:
                try:
                    ax = p.getAxis('bottom')
                    if hasattr(ax, 'picture'):
                        ax.picture = None
                    ax.update()
                    p.update()
                except Exception:
                    pass
        if getattr(self, 'overview_plot', None):
            try:
                ax = self.overview_plot.getAxis('bottom')
                if ax is not None and hasattr(ax, 'picture'):
                    ax.picture = None
                self.overview_plot.update()
            except Exception:
                pass

    def _overview_end_text(self):
        """概觀軸右端標籤：絕對模式顯示結束牆鐘時間（origin + 總長），否則顯示總長度（MAX）。"""
        absolute = getattr(self, 'x_axis_absolute_time', False)
        rec = getattr(self, 'current_rec', None)
        origin = getattr(rec, 'start_datetime', None) if rec is not None else None
        if absolute and origin is not None:
            return "迄 " + self._fmt_time_pos(self.max_duration, include_ms=True)
        return "MAX " + format_hms(self.max_duration, include_ms=True)

    def _fmt_time_pos(self, sec, include_ms=True):
        """格式化一個『時間位置』（秒，相對全域原點）供文字顯示，與 X 軸標籤模式一致：
        絕對模式 → 牆鐘時間(origin + sec)；否則 → 自 0 起的相對經過時間。
        注意：這是給『時間點』用的；『時長/duration』請仍用 format_hms（長度無絕對形式）。"""
        if sec is None:
            sec = 0.0
        if getattr(self, 'x_axis_absolute_time', False):
            rec = getattr(self, 'current_rec', None)
            origin = getattr(rec, 'start_datetime', None) if rec is not None else None
            if origin is not None:
                t = origin + timedelta(seconds=float(sec))
                s = t.strftime('%H:%M:%S')
                if include_ms:
                    s += f".{int(t.microsecond / 1000):03d}"
                return s
        return format_hms(sec, include_ms=include_ms)

    def _abs_clock_str(self, sec, include_ms=False):
        """把『相對全域原點的秒數』格式化為**永遠絕對**的牆鐘 hh:mm:ss。
        與 _fmt_time_pos 不同：此函式不看 x_axis_absolute_time 開關，恆顯示牆鐘時間，
        供時間視窗的絕對時間輸入框與右下角事件表使用。無 origin（罕見）才退回相對經過時間。"""
        rec = getattr(self, 'current_rec', None)
        origin = getattr(rec, 'start_datetime', None) if rec is not None else None
        if origin is not None:
            t = origin + timedelta(seconds=float(sec or 0.0))
            s = t.strftime('%H:%M:%S')
            if include_ms:
                s += f".{int(t.microsecond / 1000):03d}"
            return s
        return format_hms(sec, include_ms=include_ms)

    @staticmethod
    def _parse_hms(text):
        """解析 'hh:mm:ss' 或 'hh:mm:ss.mmm'，回傳當日秒數（0~86400）；格式無效回 None。"""
        if not text:
            return None
        parts = str(text).strip().split(':')
        if len(parts) != 3:
            return None
        try:
            h = int(parts[0])
            m = int(parts[1])
            s = float(parts[2])
        except (ValueError, TypeError):
            return None
        if not (0 <= h < 24 and 0 <= m < 60 and 0 <= s < 60):
            return None
        return h * 3600 + m * 60 + s

    def _abs_text_to_rel(self, text):
        """把使用者輸入的牆鐘 hh:mm:ss 轉成『相對全域原點的秒數』。
        無 origin 時視為相對經過時間（輸入即相對秒）。跨午夜：取落在原點之後的解。
        回傳 None 表示輸入格式無效。"""
        secs = self._parse_hms(text)
        if secs is None:
            return None
        rec = getattr(self, 'current_rec', None)
        origin = getattr(rec, 'start_datetime', None) if rec is not None else None
        if origin is None:
            return secs
        origin_sod = (origin.hour * 3600 + origin.minute * 60
                      + origin.second + origin.microsecond / 1e6)
        rel = secs - origin_sod
        # 錄音可能跨午夜：輸入時間若在原點當日秒數之前，視為隔天，往後補整天
        while rel < -0.0005:
            rel += 86400.0
        return rel

    def _update_abs_time_edits(self):
        """把目前視窗起訖同步到絕對時間輸入框（顯示牆鐘 hh:mm:ss）。由 _update_time_controls 呼叫。"""
        if not hasattr(self, 'abs_start_edit'):
            return
        self.abs_start_edit.blockSignals(True)
        self.abs_end_edit.blockSignals(True)
        try:
            end_t = self.time_start + self.time_duration
            self.abs_start_edit.setText(self._abs_clock_str(self.time_start, include_ms=False))
            self.abs_end_edit.setText(self._abs_clock_str(end_t, include_ms=False))
        finally:
            self.abs_start_edit.blockSignals(False)
            self.abs_end_edit.blockSignals(False)

    def _on_abs_start_edited(self):
        """編輯絕對時間起點：平移視窗到該牆鐘時間，保持視窗長度。"""
        if getattr(self, '_updating_view', False) or not getattr(self, 'max_duration', 0):
            return
        sec = self._abs_text_to_rel(self.abs_start_edit.text())
        if sec is None:
            self._update_abs_time_edits()  # 格式無效：還原顯示
            return
        new_start = max(0.0, min(sec, self.max_duration - self.time_duration))
        if abs(new_start - self.time_start) < 1e-6:
            self._update_abs_time_edits()  # 無變化（或已被 clamp 回原值）：還原顯示
            return
        self.time_start = new_start
        self._update_overview_region()
        self._update_time_controls()
        self._update_view()
        if not getattr(self, '_loading_rec', False):
            self._save_time_for_current()

    def _on_abs_end_edited(self):
        """編輯絕對時間終點：固定起點，調整視窗長度到該牆鐘時間。"""
        if getattr(self, '_updating_view', False) or not getattr(self, 'max_duration', 0):
            return
        sec = self._abs_text_to_rel(self.abs_end_edit.text())
        if sec is None:
            self._update_abs_time_edits()  # 格式無效：還原顯示
            return
        new_dur = sec - self.time_start
        if new_dur <= 0:
            self._update_abs_time_edits()  # 終點不在起點之後：無效，還原顯示
            return
        new_dur = min(new_dur, self.max_duration - self.time_start)
        if abs(new_dur - self.time_duration) < 1e-6:
            self._update_abs_time_edits()
            return
        self.time_duration = new_dur
        self._update_overview_region()
        self._update_time_controls()
        self._update_view()
        if not getattr(self, '_loading_rec', False):
            self._save_time_for_current()

    def _refresh_event_displays(self):
        """選取改變時刷新表格與視覺標記。
        現在左列表的選取（包含真實 channel 與 xxx(無匹配)/xxx(匹配:yyy) 項目）決定表格內容與哪些 event 要標記。
        「在所有通道顯示」chk 只在 markers 內部決定是否把 real channel 的標記強制複製到所有 plot。
        如果尚未載入，會自動觸發載入 (解讀所有標記)。
        """
        self._clear_event_visuals()
        self._populate_events_table()
        if self.selected_event_channels:
            if not getattr(self, '_events_loaded', False):
                self._on_load_events_clicked()
            if getattr(self, '_events_loaded', False):
                self._add_event_markers_to_plots()
                if getattr(self, 'show_event_background_overlay', False):
                    self._add_event_time_overlays()

    def _compute_event_counts(self):
        """計算每個 display_channel (真實 raw channel 或 virtual (無匹配) / (匹配:yyy)) 的 event 數量。
        用於左側事件顯示篩選列表同時呈現真實 channel 與無法匹配/例外匹配兩種 virtual 資料。
        """
        from collections import defaultdict
        counts = defaultdict(int)
        for ev in getattr(self, 'parsed_events', []):
            key = ev.get('display_channel')
            if key:
                counts[key] += 1
        return dict(counts)

    def _update_event_channel_list(self):
        """嚴謹更新事件通道篩選表格（QTable 兩欄：名稱 / 數量）。
        預設名稱順序排列，header 可點擊切換升/降冪。
        名稱包含 (無匹配) 的項目有提示與淺黃底色，數量獨立一欄。
        使用者勾選後才顯示對應 event 標記與右側表格。
        """
        if not hasattr(self, 'event_channel_list') or self.event_channel_list is None:
            return
        tbl = self.event_channel_list
        # 記住使用者目前排序狀態（點過「數量」表頭後，希望維持多到少或少到多，而不是每次更新都硬重設成名稱排序）
        header = tbl.horizontalHeader()
        prev_sort_col = header.sortIndicatorSection() if header.isSortIndicatorShown() else -1
        prev_sort_order = header.sortIndicatorOrder()
        if not getattr(self, '_events_loaded', False):
            tbl.clear()
            tbl.setRowCount(0)
            tbl.clearSpans()
            tbl.setColumnCount(2)
            tbl.setHorizontalHeaderLabels(["名稱", "數量"])
            tbl.verticalHeader().setVisible(False)
            self.selected_event_channels.clear()
            tbl.setRowCount(1)
            item0 = QTableWidgetItem("請先載入事件資料")
            tbl.setItem(0, 0, item0)
            item1 = QTableWidgetItem("")
            item1.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            tbl.setItem(0, 1, item1)
            return
        tbl.clear()
        tbl.setRowCount(0)
        tbl.clearSpans()  # 清除任何之前 placeholder 的 span，防止提示樣式影響第一行數量顯示
        tbl.setColumnCount(2)
        tbl.setHorizontalHeaderLabels(["名稱", "數量"])
        tbl.verticalHeader().setVisible(False)  # 隱藏行號，避免與數量混淆或造成第一行顯示異常
        self.selected_event_channels.clear()
        counts = self._compute_event_counts()
        if not counts:
            tbl.setRowCount(1)
            item0 = QTableWidgetItem("(本錄音無事件)")
            tbl.setItem(0, 0, item0)
            item1 = QTableWidgetItem("")
            item1.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            tbl.setItem(0, 1, item1)
            return
        # 暫時關閉排序，避免在 insertRow 過程中 Qt 自動重排，導致第一行（尤其是 (無匹配) 項目）數量欄無法正確顯示（之前與默認 placeholder 樣式互動造成）
        tbl.setSortingEnabled(False)
        tbl.blockSignals(True)
        # 預設按名稱順序（含 (無匹配) 自然排序）
        sorted_items = sorted(counts.items(), key=lambda kv: kv[0].lower())
        tbl.setRowCount(len(sorted_items))
        for row, (key, cnt) in enumerate(sorted_items):
            # 名稱欄（可勾選）
            name_item = QTableWidgetItem(key)
            name_item.setFlags(name_item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            name_item.setCheckState(Qt.CheckState.Unchecked)
            name_item.setData(Qt.ItemDataRole.UserRole, key)
            if '(無匹配)' in key:
                name_item.setBackground(QColor(255, 255, 220))
                name_item.setToolTip(
                    f"無法匹配任何實際 raw data channel 的獨立 event 群組（原始 channel/type = {key}）。"
                )
            elif '(匹配:' in key:
                # 例外匹配項目：淡綠底 + 專用提示
                name_item.setBackground(QColor(232, 245, 233))
                # 萃取 base 與 target 供 tooltip
                base = key.split(' (匹配:')[0] if ' (匹配:' in key else key
                target = ''
                if '(匹配:' in key:
                    target = key.split('(匹配:')[1].rstrip(')')
                name_item.setToolTip(
                    f"通過例外匹配列表強制匹配的事件（原始 NDF 通道名/位置 = {base}）。\n"
                    f"已設定例外對應到 raw 通道: {target}"
                )
            tbl.setItem(row, 0, name_item)
            # 數量欄：使用專用 Numeric item 確保排序是數字順序（多到少 / 少到多），而非字串排序
            cnt_item = _NumericCountItem(str(cnt))
            cnt_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            cnt_item.setData(Qt.ItemDataRole.UserRole, cnt)  # numeric 排序 key
            tbl.setItem(row, 1, cnt_item)
        tbl.blockSignals(False)
        # 恢復排序：如果之前使用者有在「數量」欄點擊排序，就用 numeric sort（靠 _NumericCountItem.__lt__）重新排序；
        # 否則預設名稱升冪（我們 insert 時已是此順序，不需重排）
        tbl.setSortingEnabled(True)
        if prev_sort_col >= 0 and prev_sort_col < tbl.columnCount():
            tbl.sortByColumn(prev_sort_col, prev_sort_order)
            tbl.horizontalHeader().setSortIndicator(prev_sort_col, prev_sort_order)
        else:
            # set indicator to show default name asc sort without re-sorting (already inserted in order)
            tbl.horizontalHeader().setSortIndicator(0, Qt.SortOrder.AscendingOrder)
        tbl.resizeColumnsToContents()
        # force re-set qty texts after sort/layout to ensure first row (and all) display correctly, in case of any side effect from signals or reentrancy
        counts2 = self._compute_event_counts()
        for r in range(tbl.rowCount()):
            nitem = tbl.item(r, 0)
            if nitem:
                k = nitem.data(Qt.ItemDataRole.UserRole)
                if k and k in counts2:
                    citem = tbl.item(r, 1)
                    if citem:
                        citem.setText(str(counts2[k]))

    def _filter_event_channels(self, text):
        """表格即時搜尋（依名稱欄）。"""
        text = (text or "").lower()
        tbl = self.event_channel_list
        for r in range(tbl.rowCount()):
            name_item = tbl.item(r, 0)
            if name_item:
                hidden = text not in (name_item.text() or "").lower()
                tbl.setRowHidden(r, hidden)

    def _force_refresh_event_channel_qtys(self):
        """強制刷新事件通道列表的數量欄文字，確保在任何後續處理（timer、layout）後第一行等數量正確顯示。"""
        if not hasattr(self, 'event_channel_list') or self.event_channel_list is None:
            return
        tbl = self.event_channel_list
        counts = self._compute_event_counts()
        for r in range(tbl.rowCount()):
            nitem = tbl.item(r, 0)
            if nitem:
                k = nitem.data(Qt.ItemDataRole.UserRole)
                if k and k in counts:
                    citem = tbl.item(r, 1)
                    if citem:
                        citem.setText(str(counts[k]))
        # also force button/label vis to correct state
        if getattr(self, '_events_loaded', False):
            if hasattr(self, 'btn_load_events') and self.btn_load_events:
                self.btn_load_events.setVisible(False)
            if hasattr(self, 'instruction_label') and self.instruction_label:
                self.instruction_label.setVisible(True)
            if hasattr(self, 'btn_replace_excel') and self.btn_replace_excel:
                self.btn_replace_excel.setEnabled(True)
                self.btn_replace_excel.setStyleSheet("")
        else:
            if hasattr(self, 'btn_load_events') and self.btn_load_events:
                self.btn_load_events.setVisible(True)
            if hasattr(self, 'instruction_label') and self.instruction_label:
                self.instruction_label.setVisible(False)
            if hasattr(self, 'btn_replace_excel') and self.btn_replace_excel:
                self.btn_replace_excel.setEnabled(False)
                self.btn_replace_excel.setStyleSheet("")

    def _on_show_exception_dialog(self):
        """彈出簡潔的例外匹配列表對話框。
        - 說明文字 + 兩欄 (NDF通道名 / 匹配通道名) + 順序編號
        - 預設展示至 10 項（若已 5 內容則補 5 空白；>10 內容則全展示）
        - 每列上限 50 字，離開焦點(或變更) 時自動 trim 前後空格
        - 已有內容的列右側有 🗑 刪除 icon 按鈕
        - 右上小 X 關閉（不存）
        - 下方：新增（加空白列） / 保存並應用（寫 channel_matching.md + 立即刷新篩選/標記）
        - 未點保存並應用則不持久化
        """
        from PyQt6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QTableWidget, QTableWidgetItem, QHeaderView, QMessageBox
        from PyQt6.QtGui import QColor
        from PyQt6.QtCore import Qt
        dlg = QDialog(self)
        dlg.setWindowTitle("例外匹配列表")
        dlg.resize(520, 420)
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(6)

        # 說明
        tip = QLabel("NDF通道無法匹配時，可在下方設定例外匹配邏輯：")
        tip.setStyleSheet("color:#333; font-weight:500;")
        lay.addWidget(tip)

        # 表格： # | NDF通道名 | 匹配通道名 | (del)
        tbl = QTableWidget()
        tbl.setColumnCount(4)
        tbl.setHorizontalHeaderLabels(["#", "NDF通道名", "匹配通道名", ""])
        tbl.verticalHeader().setVisible(False)
        tbl.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        tbl.setEditTriggers(QTableWidget.EditTrigger.AllEditTriggers)
        tbl.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        tbl.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        tbl.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        tbl.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        tbl.setMinimumHeight(220)

        pairs = self._load_exception_pairs()
        n_content = len(pairs)
        n_blanks = max(0, 10 - n_content) if n_content <= 10 else 0
        total_rows = n_content + n_blanks

        # 內部狀態 block 避免 trim 時遞迴
        block_change = {"v": False}

        def ensure_del_btn(r: int):
            ndf_it = tbl.item(r, 1)
            mat_it = tbl.item(r, 2)
            has = bool((ndf_it.text().strip() if ndf_it else "") or (mat_it.text().strip() if mat_it else ""))
            cur = tbl.cellWidget(r, 3)
            if has:
                if not cur:
                    dbtn = QPushButton("🗑")
                    dbtn.setFixedSize(26, 22)
                    dbtn.setToolTip("刪除此列")
                    def _make_del(b, table_ref=tbl):
                        def h():
                            tbl.setSortingEnabled(False)
                            tbl.blockSignals(True)
                            for rr in range(table_ref.rowCount()):
                                if table_ref.cellWidget(rr, 3) is b:
                                    table_ref.removeRow(rr)
                                    break
                            tbl.blockSignals(False)
                            tbl.setSortingEnabled(False)
                            _renumber_exc_rows()
                            _do_sort()  # 移除後重新排序內容，空白在後
                        return h
                    dbtn.clicked.connect(_make_del(dbtn))
                    tbl.setCellWidget(r, 3, dbtn)
            else:
                if cur:
                    tbl.setCellWidget(r, 3, None)

        def _renumber_exc_rows():
            tbl.blockSignals(True)
            for i in range(tbl.rowCount()):
                ni = tbl.item(i, 0)
                if ni:
                    ni.setText(str(i + 1))
            tbl.blockSignals(False)

        def on_item_changed(it: QTableWidgetItem):
            if block_change["v"]:
                return
            if it.column() not in (1, 2):
                return
            txt = it.text()
            stripped = txt.strip()[:50]
            if stripped != txt:
                block_change["v"] = True
                it.setText(stripped)
                block_change["v"] = False
            # 變更後檢查是否需顯示/隱藏 del
            r = it.row()
            ensure_del_btn(r)

            # 資料變更後，重新套用目前排序（僅內容列），讓修改的列移到正確位置，並更新順序編號
            _do_sort()
            _renumber_exc_rows()

        # 建立初始 rows （不使用 Qt 自動排序，由我們控制僅對有內容的資料排序，空白列永遠在後）
        tbl.setSortingEnabled(False)
        tbl.blockSignals(True)
        tbl.setRowCount(total_rows)
        for i in range(total_rows):
            ndf, mat = ("", "")
            if i < n_content:
                ndf, mat = pairs[i]
            # #
            ni = QTableWidgetItem(str(i + 1))
            ni.setFlags(ni.flags() & ~Qt.ItemFlag.ItemIsEditable)
            ni.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            tbl.setItem(i, 0, ni)
            # ndf
            di = QTableWidgetItem(ndf)
            tbl.setItem(i, 1, di)
            # mat
            mi = QTableWidgetItem(mat)
            tbl.setItem(i, 2, mi)
            ensure_del_btn(i)

        tbl.blockSignals(False)

        # 自訂排序狀態與邏輯：僅排序「兩欄任一欄有內容」的資料，空白 padding 列永遠排在最後
        sort_state = {"col": 1, "order": Qt.SortOrder.AscendingOrder}

        def _get_content_and_blanks():
            content = []
            blanks = []
            for r in range(tbl.rowCount()):
                k = (tbl.item(r, 1).text().strip() if tbl.item(r, 1) else "")
                v = (tbl.item(r, 2).text().strip() if tbl.item(r, 2) else "")
                has = bool(k or v)
                rowd = [
                    tbl.item(r, 0).text() if tbl.item(r, 0) else "",
                    tbl.item(r, 1).text() if tbl.item(r, 1) else "",
                    tbl.item(r, 2).text() if tbl.item(r, 2) else "",
                    tbl.cellWidget(r, 3)
                ]
                if has:
                    content.append(rowd)
                else:
                    blanks.append(rowd)
            return content, blanks

        def _do_sort():
            col = sort_state["col"]
            order = sort_state["order"]
            if col not in (1, 2):
                return
            content, blanks = _get_content_and_blanks()
            def keyf(rowd):
                txt = rowd[col] if col < len(rowd) else ""
                return txt.lower().strip()
            content.sort(key=keyf, reverse=(order == Qt.SortOrder.DescendingOrder))
            tbl.blockSignals(True)
            tbl.setRowCount(0)
            for rowd in content + blanks:
                r = tbl.rowCount()
                tbl.insertRow(r)
                for c in range(3):
                    it = QTableWidgetItem(rowd[c])
                    if c == 0:
                        it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEditable)
                        it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                    tbl.setItem(r, c, it)
                ensure_del_btn(r)
            tbl.blockSignals(False)
            tbl.horizontalHeader().setSortIndicator(col, order)
            _renumber_exc_rows()

        def on_header_clicked(logical_index):
            if logical_index not in (1, 2):
                return
            if logical_index == sort_state["col"]:
                sort_state["order"] = Qt.SortOrder.DescendingOrder if sort_state["order"] == Qt.SortOrder.AscendingOrder else Qt.SortOrder.AscendingOrder
            else:
                sort_state["col"] = logical_index
                sort_state["order"] = Qt.SortOrder.AscendingOrder
            _do_sort()

        tbl.horizontalHeader().sectionClicked.connect(on_header_clicked)

        # 初始排序內容（預設 NDF 名稱升冪），空白在後
        _do_sort()

        # 之後才接 signal，新增/使用者編輯時才 trim + ensure + re-sort
        tbl.itemChanged.connect(on_item_changed)

        lay.addWidget(tbl, 1)

        # 按鈕區
        btns = QHBoxLayout()
        btn_add = QPushButton("新增")
        btn_add.setToolTip("在最下方新增一列空白")
        btn_save = QPushButton("保存並應用")
        btn_save.setToolTip("保存到專案根 channel_matching.md，並立即套用刷新事件篩選與標記（不按則不保存）")
        btns.addWidget(btn_add)
        btns.addStretch()
        btns.addWidget(btn_save)
        lay.addLayout(btns)

        def do_add():
            tbl.setSortingEnabled(False)
            tbl.blockSignals(True)
            r = tbl.rowCount()
            tbl.insertRow(r)
            ni = QTableWidgetItem(str(r + 1))
            ni.setFlags(ni.flags() & ~Qt.ItemFlag.ItemIsEditable)
            ni.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            tbl.setItem(r, 0, ni)
            tbl.setItem(r, 1, QTableWidgetItem(""))
            tbl.setItem(r, 2, QTableWidgetItem(""))
            # new blank: no del btn
            ensure_del_btn(r)
            tbl.blockSignals(False)
            tbl.setSortingEnabled(False)
            _renumber_exc_rows()
            _do_sort()  # 確保新空白排在最後，內容維持排序
            # scroll to new
            tbl.scrollToBottom()

        btn_add.clicked.connect(do_add)

        def do_save_apply():
            # 收集非空 pair (兩邊都有內容)，允許相同來源多筆（由使用者決定順序，匹配時取第一個）
            new_pairs = []
            for r in range(tbl.rowCount()):
                k = (tbl.item(r, 1).text().strip()[:50] if tbl.item(r, 1) else "")
                v = (tbl.item(r, 2).text().strip()[:50] if tbl.item(r, 2) else "")
                if k and v:
                    new_pairs.append((k, v))
            # 寫檔
            from pathlib import Path
            import sys
            if getattr(sys, "frozen", False):
                base = Path(sys.executable).parent
                if (base / "channel_matching.md").exists():
                    root = base
                elif (base / "_internal" / "channel_matching.md").exists():
                    root = base / "_internal"
                else:
                    root = base
            else:
                root = Path(__file__).resolve().parent
            fpath = root / "channel_matching.md"
            lines = [
                "# 例外匹配列表",
                "# 格式：每行 NDF通道名 -> 匹配通道名 （支援 -> : | = ，載入自動 trim）",
                "# 匹配時不區分大小寫；保存後會刷新目前事件顯示（若已載入）。",
                ""
            ]
            for k, v in new_pairs:
                lines.append(f"{k} -> {v}")
            try:
                fpath.write_text("\n".join(lines) + "\n", encoding="utf-8")
            except Exception as ex:
                QMessageBox.critical(dlg, "保存失敗", f"無法寫入 {fpath}：{ex}")
                return
            # 套用
            self._invalidate_exception_map_cache()
            self._apply_exception_map_to_current()
            dlg.accept()

        btn_save.clicked.connect(do_save_apply)

        # 關閉不存（預設行為）
        dlg.exec()

    def _apply_exception_map_to_current(self):
        """保存例外列表後，立即套用：若已載入事件則 re-enrich + migrate 選取 + 刷新 list/table/markers。
        若尚未載入，僅提示下次載入會自動套用。
        """
        if not getattr(self, '_events_loaded', False) or not getattr(self, 'parsed_events', None):
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.information(self, "已保存", "例外匹配列表已保存至 channel_matching.md ，下次載入事件資料時會自動套用。")
            return
        from PyQt6.QtWidgets import QMessageBox
        from PyQt6.QtCore import Qt
        old_selected = set(getattr(self, 'selected_event_channels', set()))
        # re enrich 會用新 map 計算 display / is / exception flag
        self._enrich_events_with_display_channels()
        # migrate: 原本選的 (無匹配) 若現在變成 (匹配: ) 則切換到新 key
        migrated = set()
        for old in old_selected:
            if '(無匹配)' in old:
                base = old.split(' (無匹配)')[0].strip()
                found_new = False
                for ev in self.parsed_events:
                    dc = ev.get('display_channel', '')
                    if dc.startswith(base + ' (匹配:'):
                        migrated.add(dc)
                        found_new = True
                        break
                if not found_new:
                    migrated.add(old)
            else:
                migrated.add(old)
        self.selected_event_channels = migrated
        # 重建列表（內部會 clear selected 與 uncheck）
        self._update_event_channel_list()
        # 恢復勾選狀態
        tbl = self.event_channel_list
        tbl.blockSignals(True)
        for r in range(tbl.rowCount()):
            it = tbl.item(r, 0)
            if it:
                key = it.data(Qt.ItemDataRole.UserRole)
                if key and key in self.selected_event_channels:
                    it.setCheckState(Qt.CheckState.Checked)
                else:
                    it.setCheckState(Qt.CheckState.Unchecked)
        tbl.blockSignals(False)
        self._refresh_event_displays()
        QMessageBox.information(self, "已應用", "例外匹配列表已保存並應用，事件篩選、表格與波形標記已刷新。")

    def _on_event_channel_toggled(self, item):
        """單一 checkbox 改變（只看 col 0）。"""
        if not item or item.column() != 0:
            return
        if not getattr(self, '_events_loaded', False):
            self._on_load_events_clicked()
            if not self._events_loaded:
                return  # 載入失敗
        self._update_selected_from_list()
        self._refresh_event_displays()

    def _update_selected_from_list(self):
        self.selected_event_channels.clear()
        tbl = self.event_channel_list
        for r in range(tbl.rowCount()):
            item = tbl.item(r, 0)
            if item and item.checkState() == Qt.CheckState.Checked:
                ch = item.data(Qt.ItemDataRole.UserRole)
                if ch:  # 移除舊 UNMATCHED_SPECIAL 判斷（現在獨立 checkbox 控制）
                    self.selected_event_channels.add(ch)

    def _select_all_event_channels(self):
        if not getattr(self, '_events_loaded', False):
            self._on_load_events_clicked()
        tbl = self.event_channel_list
        tbl.blockSignals(True)
        for r in range(tbl.rowCount()):
            item = tbl.item(r, 0)
            if item and not tbl.isRowHidden(r) and (item.flags() & Qt.ItemFlag.ItemIsEnabled):
                item.setCheckState(Qt.CheckState.Checked)
        tbl.blockSignals(False)
        self._update_selected_from_list()
        self._refresh_event_displays()

    def _clear_event_channels(self):
        tbl = self.event_channel_list
        tbl.blockSignals(True)
        for r in range(tbl.rowCount()):
            item = tbl.item(r, 0)
            if item:
                item.setCheckState(Qt.CheckState.Unchecked)
        tbl.blockSignals(False)
        self._update_selected_from_list()
        self._refresh_event_displays()

    def _on_load_events_clicked(self):
        """使用者主動載入事件資料：解讀所有標記 (nef + xls)。
        載入後才允許勾選篩選，勾選時才繪製表格與波形。
        這是效能優化的核心：預設完全不載入/處理全部事件資料。
        """
        if getattr(self, '_events_loaded', False):
            return
        if not self.current_rec:
            return
        self.statusBar().showMessage("正在載入事件資料...", 0)
        QApplication.processEvents()  # 讓 UI 回應
        self._parse_and_store_events()  # 初始載入 force=False
        self._events_loaded = True
        self._update_event_channel_list()
        self._populate_events_table()
        print('DEBUG inside load success, setting vis')
        if hasattr(self, 'btn_load_events') and self.btn_load_events:
            self.btn_load_events.setVisible(False)
            print('set load vis false')
        if hasattr(self, 'instruction_label') and self.instruction_label:
            self.instruction_label.setVisible(True)
            print('set label vis true')
        if hasattr(self, 'btn_replace_excel') and self.btn_replace_excel:
            self.btn_replace_excel.setEnabled(True)
            self.btn_replace_excel.setStyleSheet("")  # 確保正常啟用樣式（非灰色）
        self.statusBar().showMessage("事件資料已載入", 4000)
        # force correct button/label visibility at end, to handle reentrancy from load_folder etc.
        if hasattr(self, 'btn_load_events') and self.btn_load_events:
            self.btn_load_events.setVisible(False)
        if hasattr(self, 'instruction_label') and self.instruction_label:
            self.instruction_label.setVisible(True)
        if hasattr(self, 'btn_replace_excel') and self.btn_replace_excel:
            self.btn_replace_excel.setEnabled(True)
            self.btn_replace_excel.setStyleSheet("")
        # force set qtys and vis after all processing in load (timers from reset_panel etc at 0-100ms)
        QTimer.singleShot(200, self._force_refresh_event_channel_qtys)

    def _on_replace_xls_clicked(self):
        """使用者選擇另一個 XLS 來替換目前的分析事件資料（nef 裝置事件保留）。
        讀取後若有效，更新 parsed_events、刷新 channel list 與 events table、並重繪標記。
        """
        if not getattr(self, '_events_loaded', False) or not self.current_rec:
            QMessageBox.information(self, "請先載入", "請先點擊載入事件資料。")
            return
        xls_path, _ = QFileDialog.getOpenFileName(
            self,
            "選擇要替換的 XLS / XLSX 檔案",
            "",
            "Excel Files (*.xls *.xlsx);;All Files (*)"
        )
        if not xls_path:
            return
        try:
            # 測試讀取是否有效
            test_evs = self.current_rec.get_xls_events(xls_path=xls_path)
            if not test_evs:
                QMessageBox.warning(self, "無效檔案", "所選 Excel 檔案中沒有讀取到有效的事件資料，請確認格式是否正確（應為 Nox 分析軟體產生的 event_*.xls 或 *.xlsx 格式）。")
                return
            # 有效，記錄自訂路徑
            self._custom_xls_path = xls_path
            # 強制重新解析（使用新 custom xls，nef 部分自動來自 get_events）
            self.parsed_events = []
            self._parse_and_store_events(force=True)
            # 刷新 UI
            self._update_event_channel_list()
            self._populate_events_table()
            self._refresh_event_displays()  # 重繪波形上的標記（若有選取的通道）
            QTimer.singleShot(200, self._force_refresh_event_channel_qtys)
            self.statusBar().showMessage(f"已成功替換 Excel 標記資料：{os.path.basename(xls_path)}", 5000)
        except Exception as e:
            QMessageBox.critical(self, "替換失敗", f"讀取或解析 Excel 失敗：\n{str(e)}")
            self.statusBar().showMessage("替換 Excel 失敗", 3000)

    def _set_event_list_placeholder_if_needed(self):
        """確保未載入時列表顯示 placeholder，不讓 count=0 導致 UI 問題。"""
        if getattr(self, '_events_loaded', False):
            return
        if not hasattr(self, 'event_channel_list') or self.event_channel_list is None:
            return
        tbl = self.event_channel_list
        if tbl.rowCount() == 0 or "(尚未載入" not in (tbl.item(0, 0).text() if tbl.item(0, 0) else ""):
            tbl.clear()
            tbl.setRowCount(0)
            tbl.clearSpans()
            tbl.setColumnCount(2)
            tbl.setHorizontalHeaderLabels(["名稱", "數量"])
            tbl.verticalHeader().setVisible(False)
            tbl.setRowCount(1)
            item0 = QTableWidgetItem("請先載入事件資料")
            tbl.setItem(0, 0, item0)
            item1 = QTableWidgetItem("")
            item1.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            tbl.setItem(0, 1, item1)

    def _add_event_time_overlays(self):
        """為選取的 event 加入淺黃色透明蒙層 (LinearRegionItem)。
        現在嚴格 per-channel：
        - 只有當 event 有明確結束時間時才畫 band。
        - 真實匹配的 event：預設只在對應的 channel plot 畫黃色 band（除非「在所有通道顯示」勾選，才畫在所有 plot）。
        - 無匹配 event：只有勾選「在所有通道顯示」時才畫在所有 plot。
        沒有 end 資料的 event 永遠不畫任何 band（只可能有 start line，如果顯示模式允許）。
        這樣當只選 snore 且沒勾「所有通道顯示」時，snore event 的黃色 band 只會出現在 snore 通道。
        受「事件預設背景蒙層」選單控制，預設關閉。
        """
        self._clear_event_overlays()
        if not getattr(self, 'show_event_background_overlay', False):
            return
        if not getattr(self, 'parsed_events', None) or not self.plot_items:
            return
        # 繪製所有已選通道的事件蒙層（不再截斷）。
        to_overlay = self._events_in_view(self._get_filtered_events())
        show_all = bool(
            hasattr(self, 'chk_display_all_channels') and
            self.chk_display_all_channels and
            self.chk_display_all_channels.isChecked()
        )
        merge_gap = self._marker_merge_gap()
        # 預先算每事件的蒙層區段（含 duration 正規化），不隨通道改變，避免 per-channel 重算。
        prepared = []
        for ev in to_overlay:
            if not self._event_has_meaningful_end(ev):
                # 沒有真實 end 資料 → 不畫 region（與表格一致顯示 - ）
                continue
            rs = float(ev.get("rel_start", 0.0))
            re = float(ev.get("rel_end"))
            dur = re - rs
            if dur < 0.8 or (abs(rs - round(rs)) < 0.05 and dur < 1.5):
                rs = float(int(rs))
                re = rs + 1.0
            rs = max(0.0, min(rs, self.max_duration))
            re = max(rs + 0.05, min(re, self.max_duration))
            prepared.append((ev, rs, re, self._is_unmatched_event(ev)))

        brush = pg.mkBrush(255, 255, 140, 38)
        pen = pg.mkPen(255, 200, 50, 90)
        for i, p in enumerate(self.plot_items):
            ch_name = self.visible_channels[i] if i < len(self.visible_channels) else ""
            intervals = []
            for ev, rs, re, is_unm in prepared:
                if is_unm:
                    # 無匹配 event 只有在「在所有通道顯示」勾選時才貢獻黃色 band。
                    if not show_all:
                        continue
                # show_all 在前可短路，避免每通道對每事件做昂貴的 _event_matches_channel。
                elif not (show_all or self._event_matches_channel(ev, ch_name)):
                    continue
                intervals.append((rs, re))
            # 像素級合併重疊/相鄰區段後逐段畫（合併後數量已少，視覺無損、放大還原）。
            for a, b in self._merge_intervals(intervals, merge_gap):
                region = pg.LinearRegionItem([a, b], movable=False, brush=brush, pen=pen)
                p.addItem(region)
                self.event_overlay_regions.append(region)

    def _apply_selected_preset(self):
        """回應 combo 選擇，套用對應通道清單。"""
        if not hasattr(self, 'preset_combo') or not self.current_rec:
            return
        idx = self.preset_combo.currentIndex()
        if idx < 0 or idx >= len(getattr(self, 'preset_items', [])):
            return
        label, chans = self.preset_items[idx]
        self._apply_channel_preset(chans)

    # === 版面重構後的簡潔 splitter 管理 ===
    # 舊的 dock 浮動/還原/force 複雜邏輯（數百行 timers、remove/add、save/restoreState、eventFilter 吞 dblclick）
    # 已移除。這些是導致拖曳寬度卡頓、裁切、geometry 錯誤的主因。
    # 現在只保留對 central 的擴展輔助 + 簡單的 splitter setSizes + 適應計算。
    # 拖曳由 QSplitter 原生處理，零額外開銷，體驗流暢簡潔。

    def _reset_panel_layout(self):
        """還原左右面板寬度預設（左 325 / 右 275），中央最大。"""
        try:
            lw, rw = self._get_side_panel_widths()
            if self.main_splitter:
                total = max(900, self.width() or 1280)
                cw = max(300, total - lw - rw)
                self.main_splitter.setSizes([lw, cw, rw])
                self.main_splitter.setStretchFactor(0, 0)
                self.main_splitter.setStretchFactor(1, 1)
                self.main_splitter.setStretchFactor(2, 0)
            scr = self.screen() or QGuiApplication.primaryScreen()
            scr_info = f"{scr.name() if scr else 'None'} {scr.size() if scr else ''}"
            main_geom = self.geometry()
            min_hint = self.minimumSizeHint()
            print(f"[SPLITTER-DEBUG] reset: screen={scr_info}, main_geom={main_geom}, minSizeHint={min_hint}, target_side_w=({lw},{rw})")
            if self.main_splitter:
                print(f"  main_splitter sizes now: {self.main_splitter.sizes()}")
        except Exception as de:
            print("[SPLITTER-DEBUG] error:", de)

        QTimer.singleShot(30, self._apply_default_event_splitter_ratio)
        QTimer.singleShot(50, self._force_central_expansion)

        QTimer.singleShot(100, lambda: self.statusBar().showMessage("已還原面板寬度", 3000))

    def _force_central_expansion(self):
        """強制讓中間波形區吃掉剩餘最大空間。"""
        try:
            if hasattr(self, 'central_split') and self.central_split:
                self.central_split.setMinimumWidth(0)
                self.central_split.setMaximumWidth(16777215)
                self.central_split.updateGeometry()
                if self.central_split.layout():
                    self.central_split.layout().activate()
            if hasattr(self, 'signals_pg'):
                self.signals_pg.updateGeometry()
            central = self.centralWidget()
            if central:
                central.setMinimumWidth(0)
                central.setMaximumWidth(16777215)
                central.updateGeometry()
                if hasattr(central, 'layout') and central.layout():
                    central.layout().activate()
            QApplication.processEvents()
        except Exception:
            pass

    def _get_side_panel_widths(self):
        """計算左右面板預設寬度（左 325、右 275）。小螢幕自動調整。"""
        try:
            scr = self.screen()
            if scr is None:
                scr = QGuiApplication.primaryScreen()
            if scr:
                sw = scr.availableGeometry().width()
                # 優先使用使用者指定的預設 325 / 275，若螢幕不夠則壓縮（保證中央至少 400px）
                pref_l, pref_r = 325, 275
                min_c = 400
                if sw > pref_l + pref_r + min_c:
                    left_w = pref_l
                    right_w = pref_r
                else:
                    left_w = max(180, int((sw - min_c) * 0.55))
                    right_w = max(160, int((sw - min_c) * 0.45))
                sensible_min_w = max(700, min(1000, int(sw * 0.5)))
                self.setMinimumSize(sensible_min_w, 480)
                return left_w, right_w
        except Exception:
            pass
        self.setMinimumSize(700, 480)
        return 325, 275  # 預設左 325、右 275（使用者指定）

    def _adapt_side_panel_widths(self):
        """適應目前螢幕，設定左右面板寬度。"""
        lw, rw = self._get_side_panel_widths()
        try:
            if self.main_splitter:
                total = max(800, self.width() or 1200)
                cw = max(300, total - lw - rw)
                self.main_splitter.setSizes([lw, cw, rw])
        except Exception:
            pass
        self._force_central_expansion()
        try:
            scr_name = (self.screen().name() if self.screen() else "unknown")
            self.statusBar().showMessage(f"已適應螢幕：左 {lw}px，右 {rw}px", 5000)
        except Exception:
            pass

    def _on_main_splitter_moved(self, pos: int, index: int):
        """拖曳時顯示面板寬度 (px)。"""
        try:
            if not self.main_splitter:
                return
            sizes = self.main_splitter.sizes()
            if len(sizes) >= 3:
                left_w = sizes[0]
                right_w = sizes[2]
                tag = f"左控制: {left_w} px | 右事件: {right_w} px"
                self.statusBar().showMessage(tag, 1500)
                # 顯示 tooltip tag 作為浮動提示
                QToolTip.showText(QCursor.pos(), tag, self.main_splitter, QRect(), 1200)
        except Exception:
            pass

    def _apply_default_event_splitter_ratio(self):
        """強制把右側事件面板的垂直 splitter 設為預設 2:1（上方事件顯示篩選 : 下方事件/標記）。
        在還原初始面板、或需要重置時呼叫。
        依目前 splitter 實際高度計算比例（避免在小高度 offscreen 時被 clamp 無法達到 2:1）。
        setStretchFactor 讓之後拖拉仍傾向維持大致比例。
        """
        sp = getattr(self, 'right_events_splitter', None)
        if not sp:
            return
        try:
            total = sp.height() or 450
            if total < 200:
                total = 450  # 合理預設高度
            top = int(total * 2 / 3)   # 2:1
            bot = total - top
            sp.setSizes([top, bot])
            sp.setStretchFactor(0, 2)
            sp.setStretchFactor(1, 1)
            if sp.layout():
                sp.layout().activate()
        except Exception:
            pass

    def eventFilter(self, obj, event):
        # 舊 dock 專用的 dblclick 吞噬已移除（新版用 QSplitter，無標題列浮動問題）。
        # 如果未來需要其他過濾，可在此擴充；目前直接 pass。
        return super().eventFilter(obj, event)

    def resizeEvent(self, event):
        """小螢幕 UX: 視窗太小時給提示，鼓勵點擊「還原初始面板」恢復 splitter 原始寬度，給波形更多空間 (不強制自動切換)。"""
        super().resizeEvent(event)
        try:
            h = self.height()
            w = self.width()
            if h < 620 or w < 900:
                if not getattr(self, '_small_screen_warned', False):
                    self._small_screen_warned = True
                    self.statusBar().showMessage("視窗較小，建議調整面板寬度", 6000)
            else:
                self._small_screen_warned = False
        except Exception:
            pass

    def moveEvent(self, event):
        """螢幕適應：移動到不同解析度螢幕時更新 main minimumSize，讓 splitter 拖曳順暢。
        使用者遇到卡頓時可點「適應目前螢幕大小」或「還原初始面板」。
        """
        super().moveEvent(event)
        try:
            if not getattr(self, '_move_adapt_pending', False):
                self._move_adapt_pending = True
                QTimer.singleShot(250, self._do_move_screen_adapt)
        except Exception:
            pass

    def _do_move_screen_adapt(self):
        self._move_adapt_pending = False
        try:
            # 只更新 minSize（讓 WM 重新計算 MINMAXINFO 用較低的值），不干擾使用者拖曳 splitter。
            scr = self.screen() or QGuiApplication.primaryScreen()
            if scr:
                sw = scr.availableGeometry().width()
                sensible = max(700, min(1000, int(sw * 0.5)))
                self.setMinimumSize(sensible, 480)
            # splitter 寬度由 _adapt_side_panel_widths 或 reset 控制
        except Exception:
            pass

    # ==================== issue1: event by-channel 顯示在波形圖 ====================

    def _event_matches_channel(self, ev: dict, ch_name: str) -> bool:
        """判斷 event 的 location/type 是否對應此通道。"""
        if not ev or not ch_name:
            return False

        try:
            pairs = self._get_exception_map()
            ev_id_full = str(ev.get("location", "") or ev.get("type", "") or "").strip()
            if ev_id_full:
                el = ev_id_full.lower()
                for k, v in pairs:
                    if k.lower() == el and v.strip().lower() == ch_name.strip().lower():
                        return True
        except Exception:
            pass

        is_xls = ev.get('_source') == 'xls'
        if is_xls:
            # XLS 嚴格 exact（使用者分析事件的名稱必須精確對應通道）
            xls_name = str(ev.get("location", "") or ev.get("type", "") or "").strip()
            if xls_name == ch_name:
                return True
            return False

        # 非 XLS（裝置事件）：先嘗試完整名稱 ci exact 匹配；
        # 如果完整不匹配且含 '.'，再 split 各部分（"Snore.Envelope-Audio" → 先 "Snore"，再 "Envelope-Audio"）。
        # 全部 ci exact，無 substring/like/aliases/family 猜測。
        loc = str(ev.get("location", "") or "").strip()
        typ = str(ev.get("type", "") or "").strip()
        ev_id = loc or typ
        if not ev_id:
            return False

        ch_l = ch_name.lower().strip()

        if ev_id.lower().strip() == ch_l:
            return True

        if '.' in ev_id:
            for p in [p.strip() for p in ev_id.split('.') if p.strip()]:
                if p.lower().strip() == ch_l:
                    return True

        return False

    def _enrich_events_with_display_channels(self):
        """為每個 parsed event 計算 display_channel 與 table_position。"""
        if not self.current_rec or not getattr(self, 'parsed_events', None):
            return
        raw_chans = [c['name'] for c in self.current_rec.channels]
        for ev in self.parsed_events:
            orig_loc = str(ev.get('location', '') or ev.get('Location', '') or '').strip()
            orig_type = str(ev.get('type', '') or ev.get('Type', '') or '').strip()
            matched_ch = None
            for ch in raw_chans:
                if self._event_matches_channel(ev, ch):
                    matched_ch = ch
                    break
            valid_exc = None
            ev_id = str(ev.get("location", "") or ev.get("type", "") or "").strip().lower()
            if ev_id:
                for k, v in self._get_exception_map():
                    if k.lower() == ev_id:
                        if any(ch.strip().lower() == v.strip().lower() for ch in raw_chans):
                            valid_exc = v
                            break
            if matched_ch and not valid_exc:
                ev['display_channel'] = matched_ch
                ev['table_position'] = orig_loc or matched_ch
                ev['is_no_match'] = False
                ev.pop('exception_matched_to', None)
            elif valid_exc:
                virtual = orig_loc or orig_type or 'unknown'
                ev['display_channel'] = f"{virtual} (匹配:{valid_exc})"
                ev['table_position'] = f"{virtual}(匹配:{valid_exc})"
                ev['is_no_match'] = False
                ev['exception_matched_to'] = valid_exc
            else:
                virtual = orig_loc or orig_type or 'unknown'
                ev['display_channel'] = f"{virtual} (無匹配)"
                ev['table_position'] = f"{virtual}(無匹配)"
                ev['is_no_match'] = True
                ev.pop('exception_matched_to', None)

    def _is_unmatched_event(self, ev: dict) -> bool:
        """回傳該 event 是否為無匹配（依 enrich 設定的 is_no_match 旗標）。
        保留此方法供 markers / 其他地方使用，現在由 _enrich 嚴謹決定。
        """
        return bool(ev.get('is_no_match', False))

    def _event_has_meaningful_end(self, ev: dict) -> bool:
        """嚴格檢查 event 是否有有效的結束時間資料（用於決定是否畫 duration region）。
        沒有資料就不要顯示任何結束時間區間（避免之前 2.0s 或 +1s 假造 band 的問題）。
        與表格的 has_end_data 邏輯一致。
        """
        re = ev.get('rel_end')
        rs = ev.get('rel_start')
        if re is None or rs is None:
            return False
        try:
            return float(re) > float(rs) + 0.0005
        except (TypeError, ValueError):
            return False

    def _get_unmatched_events(self):
        """取得所有無法匹配任何 channel 的事件列表。"""
        if not getattr(self, 'parsed_events', None):
            return []
        return [ev for ev in self.parsed_events if self._is_unmatched_event(ev)]

    # ==================== 例外匹配列表 支援 (channel_matching.md) ====================

    def _load_exception_pairs(self) -> list[tuple[str, str]]:
        """從專案根目錄 channel_matching.md 載入例外匹配對列表。
        支援格式： "NDF名 -> 匹配名" 或 : | = 分隔；自動 trim、略過空/#行。
        回傳保持使用者輸入大小寫的 (ndf, mapped) 順序列表（允許相同來源多筆映射）。
        打包後 (PyInstaller) 也會正確找到同層或 _internal 裡的檔案。
        """
        from pathlib import Path
        import sys
        if getattr(sys, "frozen", False):
            base = Path(sys.executable).parent
            if (base / "channel_matching.md").exists():
                root = base
            elif (base / "_internal" / "channel_matching.md").exists():
                root = base / "_internal"
            else:
                root = base
        else:
            root = Path(__file__).resolve().parent
        f = root / "channel_matching.md"
        pairs: list[tuple[str, str]] = []
        if f.exists():
            try:
                for raw_line in f.read_text(encoding="utf-8", errors="ignore").splitlines():
                    line = raw_line.strip()
                    if not line or line.startswith("#"):
                        continue
                    sep = None
                    for s in ["->", ":", "|", "="]:
                        if s in line:
                            sep = s
                            break
                    if not sep:
                        continue
                    parts = [x.strip() for x in line.split(sep, 1)]
                    if len(parts) != 2:
                        continue
                    k, v = parts
                    if k and v:
                        pairs.append((k, v))
            except Exception as e:
                print(f"[例外匹配] 讀取 channel_matching.md 失敗: {e}")
        return pairs

    def _get_exception_map(self) -> list[tuple[str, str]]:
        """回傳 (ndf, mapped) 順序列表（允許重複來源），快取以加速。匹配時依列表順序取第一個。"""
        if getattr(self, "_exception_map", None) is None:
            self._exception_map = self._load_exception_pairs()
        return self._exception_map

    def _get_exception_mapped_for_event(self, ev: dict) -> str | None:
        """若此 event 的 id (location/type) 在例外列表有映射 (ci)，回傳目標通道名 (保留原大小寫)。依列表順序取第一個匹配。"""
        if not ev:
            return None
        pairs = self._get_exception_map()
        ev_id = str(ev.get("location", "") or ev.get("type", "") or "").strip()
        if ev_id:
            el = ev_id.lower()
            for k, v in pairs:
                if k.lower() == el:
                    return v
        return None

    def _invalidate_exception_map_cache(self):
        """保存後呼叫，強制下次重新從檔載入。"""
        self._exception_map = None

    def _parse_and_store_events(self, force: bool = False):
        """載入 events 並計算相對時間，供 table + 圖表 by-channel markers 使用。
        只有在 _on_load_events_clicked 後才執行，確保「先解讀所有標記，在勾選時才繪製」。
        force=True 時即使已載入也重新解析（用於替換XLS/XLSX，兩者皆設 _source='xls' 走嚴格匹配）。
        """
        if not force and getattr(self, '_events_loaded', False) and getattr(self, 'parsed_events', None):
            return  # 已載入，避免重複
        self.parsed_events = []
        if not self.current_rec:
            return
        custom = getattr(self, '_custom_xls_path', None)
        raw_events = self.current_rec.get_events(include_xls=True, custom_xls_path=custom) or []
        for ev in raw_events:
            # xls 事件已預先計算好 rel_start/rel_end，直接使用（避開 filetime 轉換）
            if 'rel_start' in ev and ev.get('rel_start') is not None:
                rel_s = float(ev['rel_start'])
                rel_e = ev.get('rel_end')
                if rel_e is not None:
                    rel_e = float(rel_e)
            else:
                ts = ev.get("starts_at") or ev.get("starts") or 0
                te = ev.get("ends_at") or ev.get("ends") or 0
                rel_s = self._filetime_to_relative_seconds(ts)
                rel_e = self._filetime_to_relative_seconds(te) if te else None
                if rel_e is not None and rel_e < rel_s:
                    rel_e = None
            ev2 = dict(ev)
            ev2["rel_start"] = float(rel_s)
            ev2["rel_end"] = float(rel_e) if rel_e is not None else None
            self.parsed_events.append(ev2)

        # 嚴謹 enrich，必須在 parsed 完成後立即呼叫
        self._enrich_events_with_display_channels()

        # Dedup parsed_events (using same key as filter) to fix count vs table mismatch,
        # e.g. for D18 spo2: count showed 3 (with dups) but table showed 2 unique.
        # Same (rel_start, type, location) means duplicate event row (common in nef for device events).
        seen = set()
        deduped = []
        for ev in self.parsed_events:
            key = (ev.get('rel_start'), ev.get('type'), ev.get('location', ''))
            if key not in seen:
                seen.add(key)
                deduped.append(ev)
        self.parsed_events = deduped

    def _add_event_markers_to_plots(self):
        """在各通道 plot 上加入對應事件標記。
        嚴格遵守：
        - 只有當 event 有真實 end 資料時才畫 duration region（LinearRegionItem），否則只畫 start 的 InfiniteLine。
        - 無匹配 (無匹配) event：只有「在所有通道顯示」checkbox 勾選時，才畫淺灰 marker 在所有 p 上。
        - 真實匹配 event 與 例外匹配(匹配:xxx) event：預設只在對應 ch (或例外目標 ch) 畫綠色；checkbox 勾選時強制在所有 p。
        這修復了兩個 bug：
        1. 無匹配資料不再無視 checkbox 狀態就一直顯示在所有通道。
        2. 沒有結束時間的 event（表格顯示 - ）不會在波形上畫出假的開始-結束 band。
        """
        self.event_marker_items = []
        self.event_label_items = []
        if not getattr(self, 'parsed_events', None) or not self.plot_items or not self.visible_channels:
            return
        # 繪製所有已選通道的事件標記（不再截斷）；數量由使用者勾選的通道決定。
        # 只繪製目前視窗（含一個窗寬邊距）內的標記，平移時由 _perform_update_view 刷新。
        # 事件表仍顯示全部；此處僅減少 scene 上 Qt 物件數，大幅加速平移/縮放。
        to_mark = self._events_in_view(self._get_filtered_events())
        show_all = bool(
            hasattr(self, 'chk_display_all_channels') and
            self.chk_display_all_channels and
            self.chk_display_all_channels.isChecked()
        )
        # 像素級合併門檻（秒）：約 1px 對應的視窗秒數。縮放遠時門檻變大 → 密集標記合併，
        # 把原本上萬個重疊的 InfiniteLine/region 壓成數十個物件（視覺無損，放大自動還原細節）。
        merge_gap = self._marker_merge_gap()
        # 預先算每事件的共用屬性（不隨通道改變），避免在 plot 迴圈內重複計算。
        prepared = []
        for ev in to_mark:
            rs = ev.get("rel_start", -1)
            if rs < 0 or rs > self.max_duration:
                continue
            is_unm = self._is_unmatched_event(ev)
            has_end = self._event_has_meaningful_end(ev)
            re_clip = min(float(ev.get("rel_end")), self.max_duration) if has_end else None
            prepared.append((ev, rs, is_unm, has_end, re_clip))

        for i, p in enumerate(self.plot_items):
            ch_name = self.visible_channels[i] if i < len(self.visible_channels) else ""
            # 依視覺類別收集位置，稍後合併 + 批次繪製
            red_lines = []      # 無匹配無結束：淡紅實線 #ff9999
            gray_lines = []     # 無匹配有結束：灰點線 #888888（+灰區段）
            gray_regions = []   # [x0,x1]
            green_lines = []    # 匹配/全通道：綠虛線 #27ae60（+綠區段）
            green_regions = []  # [x0,x1]
            label_events = []   # (rs, ev) 供文字標籤（綠類）
            for ev, rs, is_unm, has_end, re_clip in prepared:
                if is_unm:
                    # 無匹配獨立 event：只有「在所有通道顯示」勾選時才畫（未勾選表格仍顯示）。
                    if not show_all:
                        continue
                    if not has_end:
                        red_lines.append(rs)
                    else:
                        gray_lines.append(rs)
                        gray_regions.append((rs, re_clip))
                # show_all 在前可短路，避免每通道對每事件做昂貴的 _event_matches_channel。
                elif show_all or self._event_matches_channel(ev, ch_name):
                    green_lines.append(rs)
                    if has_end:
                        green_regions.append((rs, re_clip))
                    label_events.append((rs, ev))

            # 批次繪製（線合併成單一 PlotCurveItem；區段合併重疊/相鄰後逐段畫）
            self._draw_marker_lines(p, red_lines, merge_gap, "#ff9999", 1.5, Qt.PenStyle.SolidLine)
            self._draw_marker_lines(p, gray_lines, merge_gap, "#888888", 1.0, Qt.PenStyle.DotLine)
            self._draw_marker_regions(p, gray_regions, merge_gap, (128, 128, 128, 18), "#888888")
            self._draw_marker_lines(p, green_lines, merge_gap, "#27ae60", 1.0, Qt.PenStyle.DashLine)
            self._draw_marker_regions(p, green_regions, merge_gap, (39, 174, 96, 22), "#27ae60")

            # 文字標籤（受「檢視 > 事件文字標籤」控制，預設關閉；密集時自動稀疏）
            if getattr(self, 'show_event_text_labels', False):
                last_label_time = -1e99
                min_sep = self.time_duration * 0.015
                for rs, ev in label_events:
                    if abs(rs - last_label_time) < min_sep:
                        continue
                    try:
                        label_text = str(ev.get("type", ""))[:10]
                        if ev.get("location"):
                            label_text += " @" + str(ev.get("location", ""))[:8]
                        ti = pg.TextItem(text=label_text, color="#1e8449", anchor=(0.0, 1.0))
                        vb = p.getViewBox()
                        yr = vb.viewRange()[1] if vb else (0, 1)
                        ytop = yr[1] - (yr[1] - yr[0]) * 0.12 if yr[1] > yr[0] else 0.8
                        ti.setPos(rs, ytop)
                        p.addItem(ti)
                        self.event_label_items.append(ti)
                        last_label_time = rs
                    except Exception:
                        pass

    def _marker_merge_gap(self) -> float:
        """像素級合併門檻（秒）：約 1 個繪圖像素對應的視窗秒數。
        視窗越寬（縮放越遠）門檻越大 → 密集標記合併；放大後門檻趨近 0 → 個別標記還原。"""
        px = 0
        try:
            if self.plot_items:
                vb = self.plot_items[0].getViewBox()
                px = int(vb.geometry().width()) if vb else 0
        except Exception:
            px = 0
        if px <= 0:
            px = 1500  # 取不到寬度時的保守預設
        dur = self.time_duration or 0.0
        if dur <= 0:
            return 0.0
        return dur / float(px)

    @staticmethod
    def _merge_points(xs, gap):
        """把相距 < gap 的點合併成單一代表點（已就視覺重疊，無損）。"""
        if not xs:
            return []
        xs = sorted(xs)
        if gap <= 0:
            return xs
        out = [xs[0]]
        for x in xs[1:]:
            if x - out[-1] >= gap:
                out.append(x)
        return out

    @staticmethod
    def _merge_intervals(intervals, gap):
        """合併重疊或相距 <= gap 的 [x0,x1] 區段。"""
        if not intervals:
            return []
        intervals = sorted(intervals)
        merged = [list(intervals[0])]
        for a, b in intervals[1:]:
            if a - merged[-1][1] <= gap:
                if b > merged[-1][1]:
                    merged[-1][1] = b
            else:
                merged.append([a, b])
        return merged

    def _draw_marker_lines(self, p, xs, gap, color, width, style):
        """把多條垂直起始線批次成單一 PlotCurveItem（connect='pairs'），先做像素合併。
        以 ±BIG 的垂直線段 + ignoreBounds 確保全高顯示且不影響 Y 自動範圍。"""
        pts = self._merge_points(xs, gap)
        if not pts:
            return
        BIG = 1e9
        arr = np.asarray(pts, dtype=float)
        n = arr.shape[0]
        X = np.empty(n * 2, dtype=float)
        Y = np.empty(n * 2, dtype=float)
        X[0::2] = arr
        X[1::2] = arr
        Y[0::2] = -BIG
        Y[1::2] = BIG
        curve = pg.PlotCurveItem(x=X, y=Y, connect='pairs',
                                 pen=pg.mkPen(color, width=width, style=style))
        p.addItem(curve, ignoreBounds=True)
        self.event_marker_items.append(curve)

    def _draw_marker_regions(self, p, intervals, gap, brush_rgba, pen_color):
        """合併重疊/相鄰區段後逐段畫 LinearRegionItem（合併後數量已少）。"""
        for a, b in self._merge_intervals(intervals, gap):
            region = pg.LinearRegionItem([a, b], movable=False,
                                         brush=pg.mkBrush(*brush_rgba),
                                         pen=pg.mkPen(pen_color, width=0.5))
            p.addItem(region)
            self.event_marker_items.append(region)

    def closeEvent(self, event):
        # PR4: 關閉前保存最後狀態（visible、time window）。（已取消 style/scale UI）
        self._save_current_prefs()
        event.accept()


def main():
    app = QApplication(sys.argv)
    app.setOrganizationName("PSG")
    app.setApplicationName("PSGviewer")
    app.setStyle("Fusion")  # 乾淨現代風格

    viewer = PSGViewer()
    viewer.show()

    # 啟動時自動嘗試載入專案 input/；無資料時靜默顯示空白，不跳錯誤對話框
    input_dir = Path(__file__).resolve().parent / "input"
    QTimer.singleShot(0, lambda: viewer._load_folder(input_dir, show_error=False))

    sys.exit(app.exec())


if __name__ == "__main__":
    main()