"""
Position / categorical signal renderer for PR2 (minimal UI impact).
Implements PositionStepRenderer (stepMode, windowed segments, color bands via LinearRegionItem, TextItem annotations).
VizSettings simple container (in-memory only; per-rec persistence delegated to PrefsManager in viewer via QSettings + json, PR4).
PositionStyleDialog QDialog stub for mode/thresholds/labels/colors.
Follows design: default step_fill + reference labels from real D18 clusters; tunable.

PR5 polish / Open Questions addressed (no user input needed):
- Position labels: 使用臨床英文 (Supine/Left/Right/Prone/Upright) + 中文 tooltip（已在 banner/clinical + 此處 y ticks 顯示英文，tooltip 解釋）。
- Default palette: 保持目前 (Supine 使用 #d62728 紅色風險 accent 與 banner 一致；其他 categorical 標準色)。已在 banner stylesheet / ClinicalSummary bar / 此處 colors 實作並文件化。
- Fallback: 已在 PR1 get_patient_info + PR3 banner ( "報告未找到，僅顯示技術資訊" + rec.name proxy)。
- Persistence scope: global (scale) + per-rec (visible/time/viz) 已由 PR4 完整實作。
- Future annotation: 預留 hook（見下方 PositionStepRenderer 註解）；不實作 sidecar 覆寫。
- overview_plot: 維持簡化 time+events，僅加小 note 說明 position 詳細渲染在中央（PR5 已加）。
"""

import numpy as np
import pyqtgraph as pg

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QComboBox, QLineEdit,
    QPushButton, QDialogButtonBox
)
from PyQt6.QtCore import Qt


class VizSettings:
    """簡單 class（render_mode dict + thresholds 等）；memory only for PR2 session."""
    def __init__(self):
        self.render_modes: dict = {}
        self.pos_thresholds: dict = {}
        self.pos_labels: dict = {}
        self.pos_colors: dict = {}
        self.general_styles: dict = {}  # for all channels: {'color': , 'width': }


class PositionStepRenderer:
    """專用 position renderer。支援 stepMode、windowed segments、color bands、annotations。
    僅在 _update_view 傳入目前視窗資料，避免長時間錄音的效能問題。

    PR5 Future annotation hook (Open Q6): 
    # TODO future: 可在此加入 annotation layer，讓使用者在 position trace 上手動 override 姿勢。
    # 儲存為 per-rec sidecar json (e.g. { "overrides": [{"t": 123.4, "pos": "Left"}] })，載入時 merge 至 cls。
    # 不影響現有 raw data fidelity；dialog 可擴充 "Add manual annotation" 按鈕。
    """
    def __init__(self, plot_item: pg.PlotItem, ch_name: str, viz_settings: VizSettings):
        self.plot = plot_item
        self.ch_name = ch_name
        self.viz = viz_settings
        self.items: list = []

    def clear(self):
        for item in self.items:
            try:
                self.plot.removeItem(item)
            except Exception:
                pass
        self.items.clear()

    def _get_mode(self) -> str:
        return self.viz.render_modes.get(self.ch_name, "step_fill")

    def _get_thresholds(self) -> list:
        return self.viz.pos_thresholds.get(self.ch_name, [-800.0, -300.0, 50.0, 1500.0])

    def _get_labels(self) -> list:
        return self.viz.pos_labels.get(self.ch_name, ["Supine", "Left", "Right", "Prone", "Upright"])

    def _get_colors(self) -> list:
        # PR5 / design Open Q3: 預設 palette 保持 Supine 紅色風險 accent (#d62728) 與 banner 一致；其他 posture 用標準 categorical 色。
        # 可經 PositionStyleDialog 完全自訂並持久化（per-rec）。
        return self.viz.pos_colors.get(self.ch_name, ["#d62728", "#1f77b4", "#2ca02c", "#ff7f0e", "#9467bd"])

    def _classify(self, y: np.ndarray, thresholds: list) -> np.ndarray:
        if len(y) == 0:
            return np.array([], dtype=int)
        th = sorted(thresholds)
        cls = np.searchsorted(th, y)
        return cls

    def update(self, x: np.ndarray, y: np.ndarray, fs: float, t_start: float = 0.0):
        """Rebuild only for current window (called from _update_view)."""
        self.clear()
        if len(y) == 0 or len(x) == 0:
            return
        mode = self._get_mode()
        if mode == "line":
            curve = self.plot.plot(x, y, pen=pg.mkPen(color="#1f77b4", width=1))
            self.items.append(curve)
            return

        # categorical: discrete levels from thresholds
        thresholds = self._get_thresholds()
        labels = self._get_labels()
        colors = self._get_colors()
        cls = self._classify(y, thresholds)
        n_labels = len(labels)

        y_disc = cls.astype(float)

        # y ticks + range (discrete levels)
        try:
            tick_list = [(float(i), labels[i] if i < n_labels else str(i)) for i in range(n_labels)]
            self.plot.getAxis("left").setTicks([tick_list])
            self.plot.setYRange(-0.5, float(n_labels) - 0.5, padding=0)
        except Exception:
            pass

        if mode in ("step", "step_fill"):
            if mode == "step_fill":
                # windowed segments -> color bands (subtle fill for grouping)
                # 專業UX最佳實踐 (主流醫學訊號檢視器)：主要依賴左側 y-ticks 顯示類別標籤 (Supine/Left/...)
                # 移除或極小化內部 TextItem，避免與時間軸、紅線、左標籤重疊遮擋
                # 只在明顯寬的 segment 才加極小、右偏移的輔助標籤
                if len(cls) > 1:
                    diffs = np.diff(cls)
                    change_pts = np.where(diffs != 0)[0] + 1
                    seg_starts = np.concatenate(([0], change_pts))
                    seg_ends = np.concatenate((change_pts, [len(cls)]))
                    for s, e in zip(seg_starts, seg_ends):
                        if e <= s:
                            continue
                        cidx = int(cls[s])
                        col = colors[cidx % len(colors)] if colors else "#cccccc"
                        x0 = float(x[s])
                        xi = min(e, len(x) - 1)
                        x1 = float(x[xi])
                        if x1 <= x0:
                            x1 = x0 + (1.0 / max(fs, 1.0))
                        try:
                            region = pg.LinearRegionItem([x0, x1], movable=False, brush=pg.mkBrush(col + "20"))
                            self.plot.addItem(region)
                            self.items.append(region)
                            # 僅在 segment 寬度 > 0.8 且非首個時加極小標籤 + 偏移
                            if (x1 - x0) > 0.8 and s > 0:
                                lab = labels[cidx] if cidx < n_labels else "?"
                                txt = pg.TextItem(lab, color="#555555", anchor=(0, 0.5))
                                txt.setFont(pg.QtGui.QFont("Arial", 7))
                                txt.setPos(x0 + 0.08, float(cidx))
                                self.plot.addItem(txt)
                                self.items.append(txt)
                        except Exception:
                            pass
            # main step trace (windowed)
            try:
                step_pen = pg.mkPen(color="#222222", width=2)
                step_curve = self.plot.plot(x, y_disc, stepMode="center", connect="finite", pen=step_pen)
                self.items.append(step_curve)
            except Exception:
                # older pyqtgraph fallback
                step_curve = self.plot.plot(x, y_disc, connect="finite", pen=pg.mkPen(color="#222222", width=2))
                self.items.append(step_curve)
        else:
            curve = self.plot.plot(x, y, pen=pg.mkPen(color="#1f77b4", width=1))
            self.items.append(curve)


class PositionStyleDialog(QDialog):
    """QDialog 原型（支援 mode/thresholds/labels/colors 編輯，暫存於 memory）。
    持久化已由 PR4 PrefsManager (QSettings) 實作；dialog accept 後會觸發 save。
    """
    def __init__(self, parent, ch_name: str, mode: str, thresholds: list, labels: list, colors: list):
        super().__init__(parent)
        self.setWindowTitle(f"Position Style - {ch_name} (PR2 stub)")
        self.ch_name = ch_name
        self._result = None

        lay = QVBoxLayout(self)

        lay.addWidget(QLabel("Render Mode:"))
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["line", "step", "step_fill"])
        if mode in ["line", "step", "step_fill"]:
            self.mode_combo.setCurrentText(mode)
        lay.addWidget(self.mode_combo)

        lay.addWidget(QLabel("Thresholds (逗號分隔，由低至高):"))
        self.th_edit = QLineEdit(", ".join(str(float(t)) for t in thresholds))
        lay.addWidget(self.th_edit)

        lay.addWidget(QLabel("Labels (逗號分隔):"))
        self.lab_edit = QLineEdit(", ".join(str(l) for l in labels))
        lay.addWidget(self.lab_edit)

        lay.addWidget(QLabel("Colors (逗號分隔 hex):"))
        self.col_edit = QLineEdit(", ".join(str(c) for c in colors))
        lay.addWidget(self.col_edit)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self._on_accept)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    def _on_accept(self):
        try:
            mode = self.mode_combo.currentText()
            th = [float(x.strip()) for x in self.th_edit.text().split(",") if x.strip()]
            lb = [x.strip() for x in self.lab_edit.text().split(",") if x.strip()]
            cl = [x.strip() for x in self.col_edit.text().split(",") if x.strip()]
            self._result = (mode, th, lb, cl)
            self.accept()
        except Exception as ex:
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.warning(self, "Parse error", str(ex))

    def get_settings(self):
        return self._result


class SimpleChannelStyleDialog(QDialog):
    """簡易樣式對話框 for 非 position 通道：顏色 + 線寬。
    與 PositionStyleDialog 類似，支援即時預覽與持久化。
    """
    def __init__(self, parent, ch_name: str, color: str, width: float):
        super().__init__(parent)
        self.setWindowTitle(f"通道樣式 - {ch_name}")
        self.ch_name = ch_name
        self._result = None
        self.current_color = color

        lay = QVBoxLayout(self)

        lay.addWidget(QLabel("線條顏色:"))
        self.color_btn = QPushButton()
        self.color_btn.setFixedWidth(80)
        self.color_btn.setStyleSheet(f"background-color: {color}; border: 1px solid #888;")
        self.color_btn.clicked.connect(self._choose_color)
        lay.addWidget(self.color_btn)

        lay.addWidget(QLabel("線寬:"))
        self.width_spin = QDoubleSpinBox()
        self.width_spin.setRange(0.5, 5.0)
        self.width_spin.setSingleStep(0.5)
        self.width_spin.setValue(width)
        lay.addWidget(self.width_spin)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self._on_accept)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    def _choose_color(self):
        from PyQt6.QtWidgets import QColorDialog
        from PyQt6.QtGui import QColor
        col = QColorDialog.getColor(QColor(self.current_color), self, "選擇線條顏色")
        if col.isValid():
            self.current_color = col.name()
            self.color_btn.setStyleSheet(f"background-color: {self.current_color}; border: 1px solid #888;")

    def _on_accept(self):
        self._result = (self.current_color, self.width_spin.value())
        self.accept()

    def get_settings(self):
        return self._result
