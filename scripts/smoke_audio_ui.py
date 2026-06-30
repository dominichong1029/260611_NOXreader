#!/usr/bin/env python3
"""Headless 煙霧測試：驗證「加入音頻通道」功能端到端正確性。

涵蓋：
  1. ffmpeg 解碼立體聲音檔 → 拆成 L/R 兩通道，名稱 0_音頻_<檔名>_<聲道>
  2. 音頻通道排在通道表/波形最上方，且預設勾選、curve 有資料
  3. 音頻獨立偏移：位移後波形位置正確、視窗落在音頻外回空（超過 max 不顯示）
  4. 波形型態切換 peak/RMS 後重繪不崩潰、curve 仍有資料
  5. _clear_audio_channels 釋放臨時 PCM 檔
不依賴真人操作（monkeypatch 檔案/詢問對話框）。
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
from PyQt6.QtWidgets import QApplication, QFileDialog, QMessageBox  # noqa: E402
import psg_viewer  # noqa: E402

D18 = ROOT / "input" / "D18_17923057_PSG_20260511"


def pump(app, ms=1500):
    end = time.monotonic() + ms / 1000.0
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


def make_test_audio(dirpath: Path) -> Path:
    """合成 30 秒立體聲測試音檔（左 400Hz、右 1000Hz）。"""
    exe = imageio_ffmpeg.get_ffmpeg_exe()
    out = dirpath / "smoke_tone_stereo.wav"
    subprocess.run(
        [exe, "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", "sine=frequency=400:duration=30",
         "-f", "lavfi", "-i", "sine=frequency=1000:duration=30",
         "-filter_complex", "[0:a][1:a]amerge=inputs=2[a]", "-map", "[a]", "-ac", "2",
         str(out)],
        check=True,
    )
    return out


def audio_curve_indices(v):
    return [i for i, ch in enumerate(v.visible_channels) if v._is_audio_channel(ch)]


def curve_len(v, i):
    if i >= len(v.curves) or v.curves[i] is None:
        return 0
    xy = v.curves[i].getData()
    return len(xy[0]) if xy[0] is not None else 0


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    v = psg_viewer.PSGViewer()
    v._load_folder(str(D18), show_error=False)
    pump(app, 2500)
    assert v.current_rec is not None, "錄音未載入"
    assert v.act_add_audio_channel.isEnabled(), "載入後『加入音頻通道』應啟用"

    tone = make_test_audio(ROOT / "scripts")
    print(f"[setup] 測試音檔 {tone.name} ({tone.stat().st_size} bytes), 錄音長 {v.max_duration:.0f}s")

    # monkeypatch：檔案對話框回傳測試音檔、多聲道詢問選「是」（拆分）
    psg_viewer.QFileDialog.getOpenFileName = staticmethod(lambda *a, **k: (str(tone), ""))
    psg_viewer.QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)

    # === 1. 加入音頻通道 ===
    v._on_add_audio_channel()
    pump(app, 1500)

    assert len(v.audio_channels) == 2, f"立體聲應拆 2 通道，實得 {len(v.audio_channels)}"
    names = list(v.audio_channels.keys())
    print(f"[1] 音頻通道：{names}")
    assert all(n.startswith("0_音頻_") for n in names), "音頻通道名應以 0_音頻_ 開頭"
    assert any(n.endswith("_L") for n in names) and any(n.endswith("_R") for n in names), "應有 L/R 聲道"

    # === 2. 排最上 + 勾選 + 有資料 ===
    aidx = audio_curve_indices(v)
    print(f"[2] 音頻在 visible 的索引={aidx}（應為最前面的 0,1）")
    assert aidx == [0, 1], f"音頻通道應排最上方，實得索引 {aidx}"
    for i in aidx:
        n = curve_len(v, i)
        print(f"    curve[{i}] 點數={n}")
        assert n > 0, f"音頻 curve[{i}] 無資料"

    # === 3. 獨立偏移 ===
    # 視窗 [0,300]：偏移 +100s 後音頻應出現在 ~100s 處（仍有資料）
    v.source_offset_adjust["audio"] = 100.0
    v.time_start, v.time_duration = 0.0, 300.0
    v._perform_update_view()
    pump(app, 500)
    i0 = aidx[0]
    x_data = v.curves[i0].getData()[0]
    assert x_data is not None and len(x_data) > 0, "偏移後 [0,300] 視窗音頻應有資料"
    x_min = float(x_data.min())
    print(f"[3] 偏移+100s 後音頻起點 x~{x_min:.1f}（應~100）")
    assert 95.0 <= x_min <= 140.0, f"偏移後音頻起點 {x_min:.1f} 不在預期 ~100s"

    # 視窗 [0,50]：完全落在音頻（100~130s）之前 → 應無資料（超範圍不顯示）
    v.time_start, v.time_duration = 0.0, 50.0
    v._perform_update_view()
    pump(app, 400)
    n_before = curve_len(v, i0)
    print(f"[3] 視窗[0,50]（音頻在100s後）音頻點數={n_before}（應為 0）")
    assert n_before == 0, "落在音頻範圍外的視窗不應顯示音頻"
    v.source_offset_adjust["audio"] = 0.0

    # === 4. 波形型態切換 ===
    v.time_start, v.time_duration = 0.0, 30.0
    v._perform_update_view()
    pump(app, 400)
    peak_len = curve_len(v, i0)
    v._on_audio_mode_changed("rms")
    pump(app, 500)
    rms_len = curve_len(v, i0)
    print(f"[4] 切換型態：peak 點數={peak_len}  rms 點數={rms_len}（皆應>0）")
    assert v.audio_display_mode == "rms", "型態未切到 rms"
    assert rms_len > 0, "RMS 模式音頻無資料"
    v._on_audio_mode_changed("peak")
    pump(app, 400)
    assert curve_len(v, i0) > 0, "切回 peak 音頻無資料"

    # === 5. 清理臨時檔 ===
    tmp_paths = [s._tmp_path for s in v.audio_sources if s._tmp_path]
    assert tmp_paths and all(os.path.exists(p) for p in tmp_paths), "解碼臨時檔應存在"
    v._clear_audio_channels()
    assert all(not os.path.exists(p) for p in tmp_paths), "清理後臨時 PCM 檔應已刪除"
    assert not v.audio_channels and not v.audio_sources, "清理後音頻狀態應清空"
    print(f"[5] 已清理 {len(tmp_paths)} 個臨時 PCM 檔")

    try:
        tone.unlink()
    except Exception:
        pass

    print("\n音頻功能煙霧測試 PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
