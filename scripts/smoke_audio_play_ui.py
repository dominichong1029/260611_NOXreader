#!/usr/bin/env python3
"""Headless 煙霧測試：驗證「音頻播放面板 + 播放游標(紅線)」的 UI 與捲動數學。

涵蓋：
  1. 概觀下方有播放面板，預設顯示；播放/音量/倍速控制存在
  2. 加入音頻後播放控制啟用；檢視選單開關可隱藏/顯示面板（不是用 enable）
  3. 倍速選項＝0.25/0.5/1/1.25/1.5/2/2.5/3
  4. 播放游標 _set_playhead(t) 的核心交互：
       - 開頭區（t<半視窗）：視窗不動，紅線從左往右（可播開頭那幾秒）
       - 中段：視窗向左捲動、紅線置中
       - 結尾：視窗到底後紅線續往右抵達 max
  5. 所有軸（各通道細紅線 + 概觀紅線）位置一致且＝播放游標
  6. 未播放（未 engage）時紅線＝視窗中心
不依賴真人操作，也不需音訊輸出裝置（播放數學與媒體解耦）。
"""
from __future__ import annotations

import os
import sys
import time
import subprocess
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import imageio_ffmpeg  # noqa: E402
from PyQt6.QtWidgets import QApplication, QMessageBox  # noqa: E402
import psg_viewer  # noqa: E402

D18 = ROOT / "input" / "D18_17923057_PSG_20260511"


def pump(app, ms=600):
    end = time.monotonic() + ms / 1000.0
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


def make_test_audio(dirpath: Path) -> Path:
    exe = imageio_ffmpeg.get_ffmpeg_exe()
    out = dirpath / "smoke_play_tone.wav"
    subprocess.run(
        [exe, "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=20",
         "-ac", "1", str(out)],
        check=True,
    )
    return out


def red_positions(v):
    """各通道細紅線位置（略過 None）。"""
    return [round(float(l.value()), 3) for l in v.plot_red_lines if l is not None]


def approx(a, b, tol=1e-3):
    return abs(a - b) <= tol


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    v = psg_viewer.PSGViewer()
    v._load_folder(str(D18), show_error=False)
    pump(app, 2000)
    assert v.current_rec is not None, "錄音未載入"

    # === 1. 面板存在、預設顯示、控制齊全 ===
    assert getattr(v, "audio_play_panel", None) is not None, "找不到音頻播放面板"
    assert v.show_audio_play_panel is True, "面板預設應顯示"
    # headless 下頂層視窗未 show()，用 isHidden() 檢查「顯示意圖」（非 isVisible）
    assert not v.audio_play_panel.isHidden(), "面板預設應顯示（未被隱藏）"
    assert hasattr(v, "btn_play") and hasattr(v, "vol_slider") and hasattr(v, "speed_combo"), "播放控制不齊全"
    print("[1] 播放面板存在且預設顯示，控制齊全")

    # 加入音頻前：播放鍵置灰（面板仍顯示，不隱藏）
    assert not v.btn_play.isEnabled(), "未加音頻時播放鍵應置灰"

    # === 3. 倍速選項 ===
    speeds = [v.speed_combo.itemData(i) for i in range(v.speed_combo.count())]
    print(f"[3] 倍速選項={speeds}")
    assert speeds == [0.25, 0.5, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0], "倍速選項不符規格"
    assert approx(v.speed_combo.currentData(), 1.0), "預設倍速應為 1x"

    # === 3b. 增益控制（即時放大，dB→倍率）===
    assert hasattr(v, "gain_slider"), "缺增益控制"
    assert (v.gain_slider.minimum(), v.gain_slider.maximum()) == (0, 48), "增益範圍應 0~48dB"
    assert approx(v._gain_from_db(0), 1.0), "0dB 應為 1x"
    assert approx(v._gain_from_db(20), 10.0, 1e-2), "+20dB 應≈10x"
    assert approx(v._gain_from_db(6), 1.995, 1e-2), "+6dB 應≈2x"
    print(f"[3b] 增益範圍 0~48dB；預設 {v.gain_slider.value()}dB → {v._gain_from_db(v.gain_slider.value()):.2f}x")

    # === 加入音頻 ===
    tone = make_test_audio(ROOT / "scripts")
    psg_viewer.QFileDialog.getOpenFileName = staticmethod(lambda *a, **k: (str(tone), ""))
    psg_viewer.QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
    v._on_add_audio_channel()
    pump(app, 1000)
    assert len(v.audio_channels) >= 1, "音頻未加入"

    # === 2. 加入後播放控制啟用（若後端可用）；選單開關隱藏/顯示面板 ===
    backend = v._backend_ready()
    print(f"[2] QtMultimedia 後端可用={backend}")
    if backend:
        assert v.btn_play.isEnabled(), "加入音頻後播放鍵應啟用"
    v.act_audio_play_panel.setChecked(False)
    assert v.audio_play_panel.isHidden(), "取消勾選後面板應隱藏（visibility 非 enable）"
    v.act_audio_play_panel.setChecked(True)
    assert not v.audio_play_panel.isHidden(), "重新勾選後面板應顯示"
    print("[2] 選單開關正確切換面板顯示/隱藏")

    # === 4/5/6. 播放游標捲動數學（與媒體位置解耦）===
    md = v.max_duration
    v.time_start, v.time_duration = 0.0, 60.0
    v._perform_update_view()
    pump(app, 200)

    # 未 engage：紅線＝視窗中心 (30)
    v._playhead_engaged = False
    v._sync_red_markers()
    rp = red_positions(v)
    print(f"[6] 未播放時紅線={rp[:3]}（應≈視窗中心 30）")
    assert all(approx(x, 30.0) for x in rp), "未播放時紅線應在視窗中心"

    # engage 播放游標
    v._playhead_engaged = True

    # 開頭區 t=10 < 半視窗30 → 視窗不動(0)，紅線=10
    v._set_playhead(10.0)
    pump(app, 100)
    assert approx(v.time_start, 0.0), f"開頭區視窗不應捲動，time_start={v.time_start}"
    assert approx(v._red_line_pos(), 10.0), "紅線應在 10"
    assert all(approx(x, 10.0) for x in red_positions(v)), "各通道紅線應＝10（從左往右）"
    assert approx(float(v.overview_vline.value()), 10.0), "概觀紅線應＝10"
    print("[4a] 開頭區：視窗不動、紅線從左(10)往右 ✓")

    # 中段 t=200 → 視窗捲動使紅線置中：time_start=200-30=170
    v._set_playhead(200.0)
    pump(app, 100)
    assert approx(v.time_start, 170.0), f"中段視窗應置中捲動，time_start={v.time_start}"
    assert approx(v._red_line_pos(), 200.0), "紅線應在 200"
    # 紅線在視窗中心
    assert approx(v._red_line_pos() - v.time_start, v.time_duration / 2.0), "中段紅線應置中"
    assert approx(float(v.overview_vline.value()), 200.0), "概觀紅線應＝200"
    print("[4b] 中段：視窗向左捲動、紅線置中 ✓")

    # 結尾 t=max → 視窗到底(max-60)，紅線抵達 max（離開中心到右緣）
    v._set_playhead(md + 100.0)  # 超過會被夾到 md
    pump(app, 100)
    assert approx(v.playhead_time, md), "播放游標應夾在 max"
    assert approx(v.time_start, md - 60.0), f"結尾視窗應到底，time_start={v.time_start}"
    assert approx(v._red_line_pos(), md), "結尾紅線應抵達 max"
    assert all(approx(x, md) for x in red_positions(v)), "各通道紅線應抵達 max"
    print("[4c] 結尾：視窗到底、紅線續往右抵達結尾 ✓")

    # === 5. 所有軸紅線一致 ===
    rps = red_positions(v)
    assert len(set(rps)) <= 1, f"所有通道紅線應一致，實得 {set(rps)}"
    assert approx(rps[0], float(v.overview_vline.value())), "概觀紅線應與通道紅線一致"
    print(f"[5] 所有軸紅線嚴格一致（{rps[0]:.1f}）")

    # 釋放：手動導航應暫停並讓紅線回中心
    v._release_playhead()
    assert v._playhead_engaged is False, "釋放後不應再 engage"
    pump(app, 100)

    # === 7. _AudioWorker._pull 即時增益（放大/限幅）+ 48k 升取樣倍速（純 numpy，免音訊裝置）===
    import numpy as np
    from psg_viewer import _AudioWorker, _AUDIO_OUT_FS
    w = _AudioWorker()
    w.configure(np.full((2000, 1), 100, dtype=np.int16), 8000)  # 安靜訊號 值=100
    w._end = 2000
    w._cursor = 0.0; w._rate = 1.0; w._gain = 1.0
    raw = np.frombuffer(w._pull(200 * 2), dtype="<i2")
    w._cursor = 0.0; w._gain = 8.0
    amp = np.frombuffer(w._pull(200 * 2), dtype="<i2")
    print(f"[7] 增益 1x→{int(raw.mean())}  8x→{int(amp.mean())}（應≈800）")
    assert abs(raw.mean() - 100) < 1, "1x 應保持原值"
    assert abs(amp.mean() - 800) < 2, "8x 應放大約 8 倍"
    w._cursor = 0.0; w._gain = 10000.0
    clip = np.frombuffer(w._pull(200 * 2), dtype="<i2")
    assert clip.max() <= 32767 and clip.min() >= -32768, "限幅應防溢位"
    assert clip.max() == 32767, "極大增益應被硬限幅到上限"
    print(f"[7] 極大增益硬限幅至 {clip.max()}（無溢位）✓")
    # 倍速 + 48k 升取樣：每輸出 frame 來源前進 (src_fs/OUT_FS)*rate
    w._cursor = 0.0; w._gain = 1.0; w._rate = 2.0
    w._pull(100 * 2)
    expect = 100 * (8000 / _AUDIO_OUT_FS) * 2.0
    print(f"[7] 倍速2x+48k升取樣：100輸出frame後來源游標={w._cursor:.2f}（應≈{expect:.2f}）")
    assert approx(w._cursor, expect, 0.5), "倍速/升取樣游標前進不符"

    # === 8. 相對位置紅線：閒置相對、平移不跳回、拖曳設定、左半起播先掃後捲 ===
    v._playhead_engaged = False
    v.time_start, v.time_duration = 1000.0, 60.0
    v._perform_update_view(); pump(app, 80)
    assert approx(v._red_line_pos(), 1030.0), "閒置紅線應在視窗中心(frac0.5)"
    v.time_start = 5000.0; v._perform_update_view(); pump(app, 80)
    assert approx(v._red_line_pos(), 5030.0), "平移後紅線應在新視窗中心(相對不變)→播放不跳回舊位"
    print("[8] 平移後紅線跟到新位置(相對位置不變) ✓")

    class _FakeLine:
        def __init__(self, x): self._x = x
        def value(self): return self._x

    v.time_start, v.time_duration = 5000.0, 60.0
    v._on_redline_dragged(_FakeLine(5015.0))  # 距左緣 15/60 = 0.25
    assert approx(v._redline_frac, 0.25), f"拖曳後 frac 應 0.25，實得 {v._redline_frac}"
    assert approx(v._red_line_pos(), 5015.0), "拖曳後紅線應在 5015"
    assert all(approx(x, 5015.0) for x in red_positions(v)), "拖曳後各軸紅線應同步"
    print(f"[8] 拖曳紅線 → frac={v._redline_frac}，各軸同步 ✓")

    v._playhead_engaged = True
    v.time_start, v.time_duration = 5000.0, 60.0
    v._set_playhead(5020.0)  # off=20<=half(30) 左半 → 視窗靜止
    assert approx(v.time_start, 5000.0), f"左半播放視窗應靜止，time_start={v.time_start}"
    v._set_playhead(5040.0)  # off=40>30 過中心 → 捲動 desired=5010
    assert approx(v.time_start, 5010.0), f"過中心才捲動，time_start={v.time_start}"
    print("[8] 左1/4起播：先靜止往右掃、過中心才捲動 ✓")

    # === 9. Ctrl+拖曳音頻波形微調偏移（不動起始絕對時間 base）===
    from psg_viewer import _WaveViewBox
    aidx = next(i for i, ch in enumerate(v.visible_channels) if v._is_audio_channel(ch))
    vb = v.plot_items[aidx].getViewBox()
    assert isinstance(vb, _WaveViewBox) and vb._is_audio and vb._ctrl_drag_cb is not None, "音頻 plot 應為可 Ctrl 拖曳的 _WaveViewBox"
    # 非音頻通道的 viewbox 不應啟用 ctrl 拖曳
    nidx = next((i for i, ch in enumerate(v.visible_channels) if not v._is_audio_channel(ch)), None)
    if nidx is not None:
        assert not v.plot_items[nidx].getViewBox()._is_audio, "非音頻通道不應啟用 Ctrl 拖曳"
    v.source_offset_adjust["audio"] = 0.0; v._audio_base_sec = 0.0
    v._on_audio_ctrl_drag(2.5, False)   # 往右拖 +2.5s
    assert approx(v.source_offset_adjust["audio"], 2.5), f"Ctrl拖曳應 +2.5s, 實得 {v.source_offset_adjust['audio']}"
    v._on_audio_ctrl_drag(-1.0, True)   # 往左拖 -1s
    assert approx(v.source_offset_adjust["audio"], 1.5), "再 -1s → 1.5s"
    assert approx(v._audio_base_sec, 0.0), "Ctrl拖曳不應動到起始絕對時間(base)"
    assert "Ctrl" in v.plot_items[aidx].toolTip(), "音頻 plot 應有 Ctrl 拖曳 tooltip"
    # mouseDragEvent 分派：Ctrl+左鍵 → 觸發 callback（用假事件）
    from PyQt6.QtCore import Qt as _Qt, QPointF
    rec = {"called": False}
    vb._ctrl_drag_cb = lambda dx, fin: rec.update(called=True)

    class _Ev:
        def modifiers(self): return _Qt.KeyboardModifier.ControlModifier
        def button(self): return _Qt.MouseButton.LeftButton
        def accept(self): pass
        def scenePos(self): return QPointF(100.0, 0.0)
        def lastScenePos(self): return QPointF(50.0, 0.0)
        def isFinish(self): return False
    vb.mouseDragEvent(_Ev())
    assert rec["called"], "Ctrl+左鍵拖曳應分派到 callback"
    print("[9] Ctrl+拖曳微調音頻偏移(+2.5→-1=1.5s)、不動 base、tooltip、事件分派 ✓")

    # === 10. 焦點在音頻軸時 Space → toggle 播放 ===
    assert vb.focusPolicy() == _Qt.FocusPolicy.ClickFocus, "音頻軸應可點擊取得焦點"
    assert vb._space_cb is not None, "音頻軸應綁定 Space 回呼"
    rec2 = {"n": 0}
    vb._space_cb = lambda: rec2.__setitem__("n", rec2["n"] + 1)

    class _KeyEv:
        def key(self): return _Qt.Key.Key_Space
        def accept(self): pass
    vb.keyPressEvent(_KeyEv())
    assert rec2["n"] == 1, "焦點在音頻軸按 Space 應觸發播放切換"
    # 非音頻軸不綁 Space
    if nidx is not None:
        assert v.plot_items[nidx].getViewBox()._space_cb is None, "非音頻軸不應綁 Space"
    print("[10] 焦點在音頻軸 Space → toggle 播放；非音頻軸不綁 ✓")

    v._clear_audio_channels()
    try:
        tone.unlink()
    except Exception:
        pass

    print("\n音頻播放面板煙霧測試 PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
