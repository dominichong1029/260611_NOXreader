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
    rows = v.events_proxy.rowCount()           # 虛擬化：透過 proxy 取列數
    src_rows = v.events_model.rowCount()
    print(f"[D] 過濾後事件數={total_filtered}  模型列數={src_rows}  proxy列數={rows}")
    assert rows == total_filtered, f"D: 表格列數({rows}) != 全部事件({total_filtered})，疑似截斷"
    assert src_rows == total_filtered, f"D: 模型列數({src_rows}) != 全部事件({total_filtered})"
    assert total_filtered > 500, f"D: 測試前提失敗，事件數應 >500（實際 {total_filtered}）"

    # 虛擬化驗證：排序正確（依 rel_s 升冪）+ 雙擊跳轉用精確秒數
    from PyQt6.QtCore import Qt as _Qt
    src0 = v.events_proxy.mapToSource(v.events_proxy.index(0, 1))
    src1 = v.events_proxy.mapToSource(v.events_proxy.index(1, 1))
    r0 = v.events_model.rel_start_at(src0.row())
    r1 = v.events_model.rel_start_at(src1.row())
    print(f"[D] 排序檢查：第1列rel={r0:.1f}  第2列rel={r1:.1f}（應遞增）")
    assert r0 is not None and r1 is not None and r0 <= r1, "D: 預設未依時間升冪排序"
    # 雙擊第一列應跳轉到該事件時間（用模型的精確 rel_start）
    v._on_event_row_activated(v.events_proxy.index(0, 0))
    pump(app, 300)
    print(f"[D] 雙擊第1列後 time_start={v.time_start:.1f}（目標事件rel={r0:.1f}）")

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

    # E: 虛擬化模型其餘正確性 —— 降冪排序、佔位訊息、資料角色（背景色/tooltip/排序鍵）
    from PyQt6.QtCore import Qt as _Qt2
    v.events_proxy.sort(1, _Qt2.SortOrder.DescendingOrder)
    d0 = v.events_model.rel_start_at(v.events_proxy.mapToSource(v.events_proxy.index(0, 1)).row())
    d1 = v.events_model.rel_start_at(v.events_proxy.mapToSource(v.events_proxy.index(1, 1)).row())
    assert d0 >= d1, "E: 降冪排序錯誤"
    print(f"[E] 降冪排序 OK（第1列rel={d0:.1f} >= 第2列rel={d1:.1f}）")

    # 資料角色：時間欄右對齊、tooltip 非空、排序鍵為數值；找一筆有背景色(無匹配/例外)的列
    idx_disp = v.events_model.index(0, 1)
    assert v.events_model.data(idx_disp, _Qt2.ItemDataRole.DisplayRole), "E: 顯示字串為空"
    assert v.events_model.data(idx_disp, psg_viewer._EventTableModel.SORT_ROLE) is not None, "E: 排序鍵缺失"
    assert v.events_model.data(v.events_model.index(0, 5), _Qt2.ItemDataRole.ToolTipRole), "E: tooltip 為空"
    n_bg = sum(1 for r in range(v.events_model.rowCount())
               if v.events_model.data(v.events_model.index(r, 0), _Qt2.ItemDataRole.BackgroundRole) is not None)
    print(f"[E] 帶背景色(無匹配/例外)的列數={n_bg}（全選通常含無匹配事件，應>0）")
    assert n_bg > 0, "E: 無任何背景色列，疑似無匹配/例外著色遺失"

    # 佔位訊息狀態不崩潰
    v._show_events_message("測試訊息")
    assert v.events_model.rowCount() == 1, "E: 佔位訊息列數應為 1"
    assert v.events_model.data(v.events_model.index(0, 0), _Qt2.ItemDataRole.DisplayRole) == "測試訊息"
    print("[E] 佔位訊息狀態 OK")

    # F: 全長 + 顯示於所有通道（最重情境）—— 像素合併應把物件數壓到遠低於 事件×通道×2
    v.events_proxy.sort(1, _Qt2.SortOrder.AscendingOrder)
    if hasattr(v, 'chk_display_all_channels') and v.chk_display_all_channels:
        v.chk_display_all_channels.setChecked(True)
    v.time_start = 0.0
    v.time_duration = v.max_duration  # Fit 全長
    t0 = time.monotonic()
    v._perform_update_view()
    pump(app, 1500)
    dt = time.monotonic() - t0
    n_vis = len(v.visible_channels)
    full_in_view = len(v._events_in_view(v._get_filtered_events()))
    marker_full = len(getattr(v, 'event_marker_items', []))
    naive = full_in_view * n_vis * 2  # 不合併時的數量級（線+區段）
    print(f"[F] 全長+show_all：事件={full_in_view} 通道={n_vis} → 合併後物件={marker_full} "
          f"(naive≈{naive}, 壓縮比≈{marker_full/max(naive,1)*100:.1f}%, 重繪{dt*1000:.0f}ms)")
    assert marker_full > 0, "F: 全長下無標記，疑似漏畫"
    assert marker_full < naive * 0.25, f"F: 合併未生效（{marker_full} 未顯著小於 naive {naive}）"
    # 蒙層（背景黃色 band）全長+show_all 也應合併到遠低於 naive
    v.time_start = 0.0
    v.time_duration = v.max_duration
    v.show_event_background_overlay = True
    v._add_event_time_overlays()
    pump(app, 800)
    ov = len(getattr(v, 'event_overlay_regions', []))
    print(f"[F] 蒙層全長+show_all：合併後 region={ov}（naive≈{full_in_view*n_vis}）")
    assert 0 < ov < full_in_view * n_vis * 0.5, f"F: 蒙層合併未生效（{ov}）"
    v.show_event_background_overlay = False
    v._clear_event_overlays()

    # 放大到 30s：個別標記應還原（縮放細節變化）
    v.time_duration = 30.0
    v.time_start = v.max_duration * 0.5
    v._perform_update_view()
    pump(app, 500)
    print(f"[F] 放大30s 後物件={len(getattr(v, 'event_marker_items', []))}（縮放細節隨之變化）")

    print("\n全部煙霧測試 PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
