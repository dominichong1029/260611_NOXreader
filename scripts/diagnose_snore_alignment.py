#!/usr/bin/env python3
"""診斷 D18 snore.ndf 與 Single Snore 事件標記的時間對齊。

驗證問題：事件表 #865~#869 的 Single Snore 標記，在波形上看起來與 snore 通道的
鼾聲爆發不吻合。需確認 snore 通道資料是否「完全與時間匹配且無問題」。

方法（量化、不靠目視）：
1. 印出 snore 通道 meta：fs / fs_f64 / samples / duration / 嵌入起始時間 / 全域 offset / dtype。
2. 取出所有 Single Snore 事件（依 rel_start 升冪），列出 #860~#872 的全域時間與 snore-local 時間。
3. 對每個 Single Snore 事件，在 snore 通道「標記時間」附近 ±W 秒掃描能量包絡，
   找出包絡峰值相對標記的位移：
     - 位移中位數 ≈ 0  → 對齊正確
     - 位移 = 固定常數  → 整體常數偏移（offset/header 問題）
     - 位移隨夜間時間線性增長 → fs 比例錯誤（取樣率不準，時間軸被拉伸/壓縮）
4. 額外健全性：snore duration vs 全域 span vs EDF duration。
"""
from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from noxreader.recording import NoxRecording  # noqa: E402

PATIENT = ROOT / "input" / "D18_17923057_PSG_20260511"
SNORE = "snore"
W = 6.0          # 掃描半窗（秒）
STEP = 0.05      # 掃描步長（秒）
ENV_HALF = 0.20  # 計算包絡能量的小窗半徑（秒）


def fmt_abs(origin, sec):
    if origin is None:
        return f"+{sec:.3f}s"
    return (origin + timedelta(seconds=float(sec))).strftime("%H:%M:%S.%f")[:-3]


def main() -> int:
    rec = NoxRecording(PATIENT)
    origin = rec.start_datetime
    info = rec.get_channel_info(SNORE)
    if info is None:
        print(f"找不到通道 {SNORE}，可用：{rec.list_channels()[:20]}")
        return 1

    fs = float(info.get("inferred_fs") or info.get("fs") or 0.0)
    samples = int(info.get("samples", 0))
    dur = samples / fs if fs > 0 else 0.0
    offset = rec.channel_offset_sec(SNORE)

    print("=" * 78)
    print("snore.ndf meta")
    print("=" * 78)
    print(f"  fs(採用)        = {fs}")
    print(f"  fs_f64(anchor)  = {info.get('fs_f64')}")
    print(f"  fs(<SamplingRate> 標籤) = {info.get('fs')}")
    print(f"  format/dtype    = {info.get('format')} / {info.get('np_dtype')} "
          f"({info.get('bytes_per_sample')}B)")
    print(f"  samples         = {samples}")
    print(f"  duration        = {dur:.3f}s  ({timedelta(seconds=dur)})")
    print(f"  data_start_byte = {info.get('data_start_byte')}")
    print(f"  嵌入 start_datetime = {info.get('start_datetime')}")
    print(f"  全域 origin     = {origin}")
    print(f"  start_offset_sec= {offset:.3f}s  (snore 第一筆樣本比全域原點晚這麼多)")
    print(f"  全域 total_span = {getattr(rec, 'total_span_sec', None)}")
    print(f"  snore 結束(全域)= {fmt_abs(origin, offset + dur)}")

    # ---- 取 Single Snore 事件 ----
    evs = rec.get_xls_events() or []
    snore_evs = [e for e in evs
                 if str(e.get("type", "")).strip().lower() == "single snore"]
    snore_evs.sort(key=lambda e: e.get("rel_start", 0.0))
    print("\n" + "=" * 78)
    print(f"Single Snore 事件數 = {len(snore_evs)}（全部 xls 事件 {len(evs)}）")
    print("=" * 78)
    print(f"{'#':>4} {'rel_start(全域s)':>16} {'絕對時間':>14} {'snore-local s':>14}")
    for i, e in enumerate(snore_evs[857:872], start=858):  # UI 1-indexed
        rs = float(e.get("rel_start", 0.0))
        print(f"{i:>4} {rs:>16.3f} {fmt_abs(origin, rs):>14} {rs - offset:>14.3f}")

    # ---- 預讀整段 snore 包絡（一次 memmap）----
    data, _ = rec.get_data(SNORE, start_sec=0.0, duration=None, as_float=True)
    data = np.asarray(data, dtype=np.float64)
    data -= np.median(data)  # 去 DC
    absd = np.abs(data)
    half = max(1, int(round(ENV_HALF * fs)))

    def env_rms(local_t):
        """snore-local 時間 local_t 附近 ±ENV_HALF 秒的 RMS（無資料回 nan）。"""
        c = int(round(local_t * fs))
        a, b = c - half, c + half
        if a < 0 or b > len(absd):
            return np.nan
        seg = data[a:b]
        return float(np.sqrt(np.mean(seg * seg)))

    # ---- 每事件最佳對齊位移 ----
    offsets_grid = np.arange(-W, W + 1e-9, STEP)
    composite = np.zeros_like(offsets_grid)
    comp_n = 0
    best_offs = []
    ev_times = []
    for e in snore_evs:
        rs = float(e.get("rel_start", 0.0))
        local = rs - offset
        prof = np.array([env_rms(local + d) for d in offsets_grid])
        if np.all(np.isnan(prof)):
            continue
        mx = np.nanmax(prof)
        if not (mx > 0):
            continue
        norm = np.nan_to_num(prof / mx, nan=0.0)
        composite += norm
        comp_n += 1
        best_offs.append(offsets_grid[int(np.nanargmax(prof))])
        ev_times.append(rs)

    best_offs = np.array(best_offs)
    ev_times = np.array(ev_times)
    print("\n" + "=" * 78)
    print(f"對齊掃描（{comp_n} 個事件，掃描窗 ±{W}s）")
    print("=" * 78)
    if comp_n:
        composite /= comp_n
        peak_off = offsets_grid[int(np.argmax(composite))]
        print(f"  複合包絡峰值位移 = {peak_off:+.3f}s （0 表示鼾聲爆發正落在標記時間）")
        print(f"  各事件最佳位移：中位數={np.median(best_offs):+.3f}s  "
              f"平均={np.mean(best_offs):+.3f}s  標準差={np.std(best_offs):.3f}s")
        within1 = np.mean(np.abs(best_offs) <= 1.0) * 100
        within2 = np.mean(np.abs(best_offs) <= 2.0) * 100
        print(f"  |位移|<=1s 的事件比例 = {within1:.1f}%   |位移|<=2s = {within2:.1f}%")
        # 線性趨勢（fs 漂移偵測）
        if len(ev_times) > 10:
            A = np.polyfit(ev_times, best_offs, 1)
            slope, intercept = A[0], A[1]
            print(f"  位移 vs 夜間時間線性擬合：slope={slope:.3e} s/s, "
                  f"截距={intercept:+.3f}s")
            print(f"    （slope×總時長 {dur:.0f}s ≈ {slope*dur:+.2f}s 端到端漂移；"
                  f"若顯著非 0 表示 fs 比例可能錯誤）")
        # 複合包絡形狀（粗略列印峰附近）
        print("\n  複合包絡（位移 → 平均正規化能量，峰值附近）:")
        for d, v in zip(offsets_grid, composite):
            if -3.0 <= d <= 3.0 and abs((d / STEP) % 10) < 1e-6:
                bar = "#" * int(v * 50)
                print(f"    {d:+5.1f}s | {v:5.3f} {bar}")

    # ---- 針對 #865~#869 的細節 ----
    print("\n" + "=" * 78)
    print("重點事件 #865~#869 細節（baseline=標記前 2~1s 的 RMS）")
    print("=" * 78)
    for i in range(864, 869):  # 0-indexed 864..868 = UI #865..#869
        if i >= len(snore_evs):
            break
        e = snore_evs[i]
        rs = float(e.get("rel_start", 0.0))
        re_ = e.get("rel_end")
        local = rs - offset
        at_mark = env_rms(local)
        baseline = np.nanmean([env_rms(local - 2 + k * 0.1) for k in range(10)])
        prof = np.array([env_rms(local + d) for d in offsets_grid])
        best = offsets_grid[int(np.nanargmax(prof))] if not np.all(np.isnan(prof)) else float("nan")
        ratio = (at_mark / baseline) if baseline and baseline > 0 else float("nan")
        print(f"  #{i+1}  {fmt_abs(origin, rs)}  局部dur={'' if re_ is None else f'{re_-rs:.2f}s'}")
        print(f"        RMS@標記={at_mark:.1f}  baseline={baseline:.1f}  "
              f"比值={ratio:.2f}  最佳對齊位移={best:+.2f}s")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
