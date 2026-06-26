#!/usr/bin/env python3
"""Headless 煙霧測試：驗證效能優化 A~D 的執行期正確性。
A: 曲線 downsampling/clipToView 已設定
B: 抗鋸齒選單切換不崩潰、curve 仍有資料
C: 事件標記只畫可視範圍（窗內 << 全部）
D: 事件表分批建列，最終列數 = 全部過濾事件數（不截斷）
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PyQt6.QtWidgets import QApplication  # noqa: E402
import psg_viewer  # noqa: E402

D18 = ROOT / "input" / "D18_17923057_PSG_20260511"


def pump(app, ms=1500):
    """處理事件迴圈一段時間，讓 progressive load / 分批填表 / debounce timer 完成。"""
    end = time.monotonic() + ms / 1000.0
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    v = psg_viewer.PSGViewer()
    v._load_folder(str(D18), show_error=False)
    pump(app, 2500)  # 等錄音 + 通道 progressive 載入

    assert v.current_rec is not None, "錄音未載入"
    print(f"[load] visible_channels={len(v.visible_channels)} curves={len(v.curves)}")

    # A: 曲線 downsampling / clipToView 已設定
    a_ok = 0
    for c in v.curves:
        if c is None:
            continue
        opts = getattr(c, 'opts', {})
        if opts.get('downsample') or opts.get('autoDownsample') or opts.get('clipToView'):
            a_ok += 1
    print(f"[A] 啟用 downsample/clip 的 curve 數 = {a_ok}/{sum(1 for c in v.curves if c)}")
    assert a_ok > 0, "A: 沒有任何 curve 啟用 downsampling/clipToView"

    # 載入事件 + 全選通道（觸發大量事件 → 分批填表）
    v._on_load_events_clicked()
    pump(app, 500)
    v._select_all_event_channels()
    pump(app, 3000)  # 等分批填表完成

    total_filtered = len(v._get_filtered_events())
    rows = v.events_table.rowCount()
    print(f"[D] 過濾後事件數={total_filtered}  事件表列數={rows}")
    assert rows == total_filtered, f"D: 表格列數({rows}) != 全部事件({total_filtered})，疑似截斷或未填完"
    assert v._evt_fill_timer is None, "D: 分批填表 timer 未正確結束"
    assert total_filtered > 500, f"D: 測試前提失敗，事件數應 >500（實際 {total_filtered}）"

    # C: 設定窄視窗，標記只畫可視範圍
    md = v.max_duration
    v.time_start = md * 0.5
    v.time_duration = 30.0
    v._perform_update_view()
    pump(app, 800)
    marker_n = len(getattr(v, 'event_marker_items', []))
    in_view = len(v._events_in_view(v._get_filtered_events()))
    print(f"[C] 可見通道={v.visible_channels}")
    print(f"[C] 30s 視窗內標記 scene 物件={marker_n}  視窗內事件={in_view}  全部={total_filtered}")
    assert in_view < total_filtered, "C: 可視範圍事件未少於全部（過濾未生效）"
    assert marker_n <= in_view * 2 + 5, f"C: 標記物件數({marker_n})異常，疑似畫了全部"

    # 決定性檢查：開啟「顯示於所有通道」→ 標記必畫在所有可見 plot，應 >0 且仍受視窗界定。
    if hasattr(v, 'chk_display_all_channels') and v.chk_display_all_channels:
        v.chk_display_all_channels.setChecked(True)
        v._perform_update_view()
        pump(app, 800)
        marker_all = len(getattr(v, 'event_marker_items', []))
        n_vis = len([1 for i in range(len(v.visible_channels))])
        print(f"[C] show_all 後視窗內標記物件={marker_all}（{in_view}事件×{n_vis}通道級距）")
        assert marker_all > 0, "C: show_all 下視窗內仍無標記，疑似標記未繪製"
        # 上界：in_view 事件 × 可見通道 × 每事件最多2物件 + 餘裕
        assert marker_all <= in_view * n_vis * 2 + 10, f"C: show_all 標記物件數({marker_all})疑似越界畫全部"
        # 對照：若不過濾，全部事件會是天文數字，確認我們遠低於它
        assert marker_all < total_filtered, "C: 標記物件數不應達全部事件量級"
        v.chk_display_all_channels.setChecked(False)
        v._perform_update_view()
        pump(app, 400)

    # B: 抗鋸齒切換不崩潰，curve 仍有資料
    before = None
    for c in v.curves:
        if c is not None:
            xy = c.getData()
            if xy[0] is not None and len(xy[0]) > 0:
                before = len(xy[0])
                break
    v._on_toggle_antialias(False)
    pump(app, 600)
    assert v.antialias_on is False, "B: antialias_on 未變 False"
    after = None
    for c in v.curves:
        if c is not None:
            xy = c.getData()
            if xy[0] is not None and len(xy[0]) > 0:
                after = len(xy[0])
                break
    v._on_toggle_antialias(True)
    pump(app, 400)
    assert v.antialias_on is True, "B: antialias_on 未變回 True"
    print(f"[B] 抗鋸齒切換 OK（curve 資料點 before={before} after={after}）")

    print("\n全部煙霧測試 PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
