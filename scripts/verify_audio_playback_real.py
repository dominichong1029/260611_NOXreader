#!/usr/bin/env python3
"""真實播放驗證（需真實平台 + 音訊裝置 + D18 實際 wav）：執行緒 worker 版。

驗證：
  1. 解碼 + 後端就緒（用實際輸出裝置）
  2. 播放：紅線(playhead)以實際播放速率前進；起點正確（無預填超前）
  3. 暫停紅線靜止、續播繼續
  4. 即時變速 2x → 紅線約 2 倍速前進；即時增益不崩潰
  5. 導航後再播放：從新位置開始，不跳回（bug 修正）
  6. 【根因】主執行緒卡住 ~1.5s（模擬長軸重畫）期間，音訊 worker 仍持續推進 → 不斷音
  7. 結尾自動收尾
執行：PYTHONUTF8=1 python scripts/verify_audio_playback_real.py
"""
from __future__ import annotations
import os, sys, time, math
from pathlib import Path

os.environ.pop("QT_QPA_PLATFORM", None)
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from PyQt6.QtWidgets import QApplication, QMessageBox
import psg_viewer

D18 = ROOT / "input" / "D18_17923057_PSG_20260511"
REAL_WAV = D18 / "20260511_林承稻.wav"


def pump(app, ms):
    end = time.monotonic() + ms / 1000.0
    while time.monotonic() < end:
        app.processEvents(); time.sleep(0.005)


def main():
    if not REAL_WAV.exists():
        print("找不到實際 wav，略過：", REAL_WAV); return 0
    app = QApplication.instance() or QApplication(sys.argv)
    v = psg_viewer.PSGViewer()
    v._load_folder(str(D18), show_error=False)
    pump(app, 2500)
    if not v._backend_ready():
        print("無音訊裝置，略過"); return 0

    psg_viewer.QFileDialog.getOpenFileName = staticmethod(lambda *a, **k: (str(REAL_WAV), ""))
    psg_viewer.QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
    t0 = time.monotonic(); v._on_add_audio_channel(); pump(app, 600)
    fs = v._play_fs
    print("[1] decode+add %.1fs, backend=%s, audio=%.0fs, 錄音=%.0fs"
          % (time.monotonic() - t0, v._backend_ready(), v._play_total / fs, v.max_duration))

    # [2] 播放：起點正確 + 實際速率前進
    v.time_start, v.time_duration = 7900.0, 60.0; v._perform_update_view(); pump(app, 100)
    v._playhead_engaged = False
    start_t = v.time_start + 0.5 * v.time_duration  # 7930
    v._toggle_play()
    pump(app, 350)
    ph_start = v.playhead_time
    print("    起播後 playhead=%.2f（起點應≈%.1f；無 +1s 預填超前）" % (ph_start, start_t))
    assert abs(ph_start - start_t) < 0.45, "起點偏差過大（疑預填超前未修）"
    tw = time.monotonic(); pump(app, 1500); dwall = time.monotonic() - tw
    dph = v.playhead_time - ph_start
    print("    牆鐘 Δ=%.2fs 紅線 Δ=%.2fs（rate1 應≈1:1，速率偏差=%.0f%%）" % (dwall, dph, abs(dph - dwall) / dwall * 100))
    assert abs(dph - dwall) < 0.25, "紅線推進速率與實際播放不符"

    # [3] 暫停/續播
    v._toggle_play(); pump(app, 200)  # pause
    php = v.playhead_time; pump(app, 500)
    assert abs(v.playhead_time - php) < 0.05, "暫停時紅線應靜止"
    v._toggle_play(); pump(app, 700)  # resume
    assert v.playhead_time > php + 0.3, "續播應繼續前進"
    print("[3] 暫停靜止、續播繼續 ✓")

    # [4] 即時變速 + 增益
    v.speed_combo.setCurrentIndex(5)  # 2x
    v.gain_slider.setValue(30)
    tw = time.monotonic(); p0 = v.playhead_time; pump(app, 1200); dwall = time.monotonic() - tw
    dph = v.playhead_time - p0
    print("[4] 2x：牆鐘 Δ=%.2fs 紅線 Δ=%.2fs（應≈2x）" % (dwall, dph))
    assert dph > dwall * 1.5, "2x 倍速紅線未加速前進"
    v.speed_combo.setCurrentIndex(2)  # 回 1x
    print("[4] 即時變速 + 增益不中斷 ✓")

    # [5] 導航後再播放：不跳回
    v._toggle_play(); pump(app, 150)  # pause
    v.time_start, v.time_duration = 14000.0, 60.0; v._perform_update_view()
    v._release_playhead(); pump(app, 50)
    v._toggle_play(); pump(app, 500)
    print("[5] 導航到 14000s 後播放 → playhead=%.1f（不應跳回 ~7935）" % v.playhead_time)
    assert 14000 <= v.playhead_time <= 14100, "導航後應從新位置開始"
    print("[5] 導航後播放從新位置、不跳回 ✓")

    # [6] 根因：主執行緒卡住 ~1.5s（用 numpy 模擬重畫，會釋放 GIL ≈ pyqtgraph 行為），音訊 worker 仍持續
    import numpy as np
    p_before = v.playhead_time
    busy_end = time.monotonic() + 1.5
    big = np.random.rand(400000)
    while time.monotonic() < busy_end:   # 完全不 processEvents（模擬長軸重畫卡住主執行緒）
        _ = np.sin(big).sum()            # numpy 重運算釋放 GIL，讓 worker 執行緒可運作
    pump(app, 150)  # 卡住結束後處理累積的 worker 位置訊號
    p_after = v.playhead_time
    print("[6] 主執行緒卡 1.5s 後：playhead %.2f→%.2f（Δ=%.2fs，worker 持續推進）active=%s"
          % (p_before, p_after, p_after - p_before, v._play_active))
    assert v._play_active, "卡住後播放不應停止（worker 在獨立執行緒）"
    assert (p_after - p_before) > 1.2, "卡住期間 worker 應持續推進（>1.2s），證明未斷音"
    print("[6] 主執行緒卡住期間音訊持續、未斷 ✓")

    # [7] 結尾收尾
    v._release_playhead(); pump(app, 50)
    end_t = v.max_duration  # 音檔較長 → 錄音結尾
    v.time_start = v.max_duration - 60; v.time_duration = 60; v._perform_update_view()
    # 把紅線拖到接近結尾再播
    v._redline_frac = 1.0
    v._toggle_play(); pump(app, 100)
    # 直接快轉：等播到結尾（最多 wait 幾秒；起點已接近結尾）
    for _ in range(40):
        pump(app, 200)
        if not v._play_active:
            break
    print("[7] 收尾後 active=%s playhead=%.1f max=%.1f" % (v._play_active, v.playhead_time, v.max_duration))
    assert v._play_active is False, "應自動收尾停止"
    assert abs(v.playhead_time - end_t) < 1.5, "紅線應抵達錄音結尾"
    print("[7] 播到結尾自動停止、紅線抵達結尾 ✓")

    v._clear_audio_channels()
    print("\n完整真實播放驗證(執行緒版) PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
