#!/usr/bin/env python3
"""Headless 煙霧測試：設定偏移量對話框（拆分欄位 + 音頻相加模型）。

涵蓋：
  1. _SplitOffsetWidget：正負/時/分/秒/毫秒 獨立欄位，value()/setValue() 往返、可分別調整
  2. 對話框：EDF/NDF/事件/音頻偏移 皆為拆分欄位；調整即套用 source_offset_adjust / event_offset_adjust
  3. 音頻：有效偏移 = 起始絕對時間(base) + 偏移調整(extra)，兩者互不影響
  4. 重設歸零：全部回 0、起始絕對時間回原點
"""
from __future__ import annotations
import os, sys, time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import subprocess, imageio_ffmpeg
from PyQt6.QtWidgets import QApplication, QMessageBox, QFormLayout, QTimeEdit, QSpinBox
import psg_viewer
from psg_viewer import _SplitOffsetWidget

D18 = ROOT / "input" / "D18_17923057_PSG_20260511"


def pump(app, ms=300):
    end = time.monotonic() + ms / 1000.0
    while time.monotonic() < end:
        app.processEvents(); time.sleep(0.01)


def approx(a, b, t=1e-6):
    return abs(a - b) <= t


def main():
    app = QApplication.instance() or QApplication(sys.argv)

    # === 1. _SplitOffsetWidget 單元 ===
    w = _SplitOffsetWidget()
    w.setValue(-3661.5)  # -(1h 1m 1s 500ms)
    assert w.sign.currentIndex() == 1 and w.hh.value() == 1 and w.mm.value() == 1 and w.ss.value() == 1 and w.ms.value() == 500, "setValue 拆分錯誤"
    assert approx(w.value(), -3661.5, 1e-3), f"value 往返錯誤 {w.value()}"
    w.sign.setCurrentIndex(0); w.hh.setValue(0); w.mm.setValue(2); w.ss.setValue(0); w.ms.setValue(0)
    assert approx(w.value(), 120.0), "獨立調整分鐘錯誤"
    print("[1] _SplitOffsetWidget 拆分/往返/獨立調整 ✓")

    # === 載入 + 加入音頻 ===
    v = psg_viewer.PSGViewer()
    v._load_folder(str(D18), show_error=False)
    pump(app, 2000)
    origin = v.current_rec.start_datetime
    tone = ROOT / "scripts" / "smoke_offset_tone.wav"
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=10", "-ac", "1", str(tone)], check=True)
    psg_viewer.QFileDialog.getOpenFileName = staticmethod(lambda *a, **k: (str(tone), ""))
    psg_viewer.QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
    v._on_add_audio_channel(); pump(app, 800)

    # === 開對話框，用 QFormLayout 取出各欄位 ===
    v._show_offset_dialog()
    dlg = v._offset_dialog
    form = dlg.findChild(QFormLayout)
    FR = QFormLayout.ItemRole.FieldRole
    edf_w = form.itemAt(0, FR).widget()
    ndf_w = form.itemAt(1, FR).widget()
    evt_w = form.itemAt(2, FR).widget()
    abs_w = form.itemAt(3, FR).widget()
    aud_w = form.itemAt(4, FR).widget()
    time_edit = abs_w.findChild(QTimeEdit)
    day_spin = abs_w.findChild(QSpinBox)
    assert all(isinstance(x, _SplitOffsetWidget) for x in (edf_w, ndf_w, evt_w, aud_w)), "偏移欄位應為拆分式"
    print("[2] 對話框欄位皆為拆分式（EDF/NDF/事件/音頻偏移）✓")

    # === 2. 調整即套用 ===
    edf_w.mm.setValue(1)  # +1 分
    pump(app, 50)
    assert approx(v.source_offset_adjust["edf"], 60.0), f"EDF 偏移未套用 {v.source_offset_adjust['edf']}"
    ndf_w.sign.setCurrentIndex(1); ndf_w.ss.setValue(2)  # -2 秒
    pump(app, 50)
    assert approx(v.source_offset_adjust["ndf"], -2.0), "NDF 偏移未套用"
    evt_w.ms.setValue(250)  # +250ms
    pump(app, 50)
    assert approx(v.event_offset_adjust, 0.25), "事件偏移未套用"
    print("[2] EDF=+60s / NDF=-2s / 事件=+0.25s 即時套用 ✓")

    # === 3. 音頻相加：base(起始絕對時間) + extra(偏移調整)，互不影響 ===
    # 設額外偏移 +3 秒
    aud_w.ss.setValue(3)
    pump(app, 50)
    base0 = v._audio_base_sec
    assert approx(base0, 0.0), "起始預設 base 應為 0（＝概觀 min）"
    assert approx(v.source_offset_adjust["audio"], 0.0 + 3.0), "音頻=base+extra 應為 3"
    t_before = time_edit.time()
    # 改起始絕對時間 +1 天 → base=86400；不應動到 extra(仍 3 秒)
    day_spin.setValue(1)
    pump(app, 50)
    assert approx(v._audio_base_sec, 86400.0), f"base 應＝+1天, 實得 {v._audio_base_sec}"
    assert approx(v.source_offset_adjust["audio"], 86400.0 + 3.0), "音頻應＝base(86400)+extra(3)"
    assert approx(aud_w.value(), 3.0), "改起始絕對時間不應影響偏移調整"
    print(f"[3] 音頻 base(+1天)+extra(+3s) → 有效={v.source_offset_adjust['audio']:.0f}s；兩者互不影響 ✓")
    # 反向：改 extra 不應動到起始絕對時間
    aud_w.ss.setValue(5)
    pump(app, 50)
    assert time_edit.time() == time_edit.time() and approx(v._audio_base_sec, 86400.0), "改偏移調整不應影響 base"
    assert approx(v.source_offset_adjust["audio"], 86400.0 + 5.0), "音頻應＝base+新extra"
    print("[3] 改偏移調整不動起始絕對時間 ✓")

    # === 4. 重設歸零 ===
    # 找重設按鈕
    from PyQt6.QtWidgets import QPushButton
    btn_reset = next(b for b in dlg.findChildren(QPushButton) if b.text() == "重設歸零")
    btn_reset.click()
    pump(app, 50)
    assert approx(v.source_offset_adjust["edf"], 0.0) and approx(v.source_offset_adjust["ndf"], 0.0)
    assert approx(v.event_offset_adjust, 0.0)
    assert approx(v._audio_base_sec, 0.0) and approx(v.source_offset_adjust["audio"], 0.0)
    assert day_spin.value() == 0
    print("[4] 重設歸零：全部回 0、起始絕對時間回原點 ✓")

    dlg.accept()
    v._clear_audio_channels()
    try:
        tone.unlink()
    except Exception:
        pass
    print("\n設定偏移量對話框煙霧測試 PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
