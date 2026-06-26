#!/usr/bin/env python3
"""嚴格審查 D18 snore(EDF) 與 snore(NDF) 的絕對時間對應是否正確。

現象：評分員用 EDF 標記，但標記在 NDF 上看起來較準、EDF 偏移。
方法：
1. 互相關：把 EDF/NDF snore 放到同一全域時間軸（各自 offset），求兩者最佳對齊 lag。
   lag≈0 → 兩者全域時間一致；lag≠0 → 其中一個被擺錯。
2. 對 Single Snore 事件（全域 rel_start），分別量 EDF / NDF 的包絡峰值位移（標記 vs 訊號）。
   位移小者=與標記較吻合。比較兩者差距即「EDF 相對標記的偏移」。
3. 印出兩通道 meta（offset / fs / 嵌入起始）佐證。
"""
from __future__ import annotations

import os
import sys
from datetime import timedelta
from pathlib import Path

import numpy as np

os.environ.setdefault("PYTHONIOENCODING", "utf-8")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from noxreader.recording import NoxRecording  # noqa: E402

D18 = ROOT / "input" / "D18_17923057_PSG_20260511"
EDF = "snore(EDF)"
NDF = "snore(NDF)"
ENV_HALF = 0.20
W = 5.0
STEP = 0.05


def load_global_env(rec, name):
    """讀整段資料，回傳放到『全域時間』的包絡取樣函式 + meta。"""
    info = rec.get_channel_info(name)
    fs = float(info.get("inferred_fs") or info.get("fs") or 0.0)
    off = rec.channel_offset_sec(name)
    data, _ = rec.get_data(name, start_sec=0.0, duration=None, as_float=True)
    data = np.asarray(data, dtype=np.float64)
    data -= np.median(data)
    half = max(1, int(round(ENV_HALF * fs)))
    return {"name": name, "fs": fs, "off": off, "data": data, "half": half,
            "samples": len(data), "src": info.get("source"),
            "start_dt": info.get("start_datetime"),
            "dur": len(data) / fs if fs > 0 else 0.0}


def env_at_global(ch, g):
    """全域時間 g 處的 RMS 包絡（local = g - offset）。"""
    c = int(round((g - ch["off"]) * ch["fs"]))
    a, b = c - ch["half"], c + ch["half"]
    if a < 0 or b > len(ch["data"]):
        return np.nan
    seg = ch["data"][a:b]
    return float(np.sqrt(np.mean(seg * seg)))


def main() -> int:
    rec = NoxRecording(D18)
    origin = rec.start_datetime
    e = load_global_env(rec, EDF)
    n = load_global_env(rec, NDF)

    print("=" * 78)
    print("通道 meta")
    print("=" * 78)
    for ch in (e, n):
        print(f"  {ch['name']:12} src={str(ch['src'] or 'ndf'):4} fs={ch['fs']:.4f} "
              f"off={ch['off']:.3f}s samples={ch['samples']} dur={ch['dur']:.2f}s "
              f"嵌入start={ch['start_dt']}")
    print(f"  全域 origin = {origin}")

    # ---- 1. 多視窗互相關：判斷常數偏移 vs fs 漂移 ----
    grid_fs = 50.0

    def xcorr_lag(g0, g1):
        gs = np.arange(g0, g1, 1.0 / grid_fs)
        ev_e = np.array([env_at_global(e, g) for g in gs])
        ev_n = np.array([env_at_global(n, g) for g in gs])
        ok = np.isfinite(ev_e) & np.isfinite(ev_n)
        if ok.sum() < len(gs) * 0.5:
            return None, None
        ev_e = np.nan_to_num(ev_e * ok); ev_n = np.nan_to_num(ev_n * ok)
        ev_e -= ev_e.mean(); ev_n -= ev_n.mean()
        corr = np.correlate(ev_e, ev_n, mode="full")
        lags = (np.arange(corr.size) - (len(ev_n) - 1)) / grid_fs
        denom = (np.sqrt(np.sum(ev_e**2)) * np.sqrt(np.sum(ev_n**2))) or 1.0
        return lags[int(np.argmax(corr))], float(np.max(corr) / denom)

    print("\n" + "=" * 78)
    print("多視窗互相關（EDF 相對 NDF 的 lag；lag<0 = EDF 被擺早）")
    print("=" * 78)
    lag_pts = []
    for g0 in (1000, 5000, 10000, 15000, 20000, 24000):
        lag, c = xcorr_lag(float(g0), float(g0) + 120.0)
        if lag is not None and c and c > 0.5:
            lag_pts.append((g0, lag))
            print(f"  全域 {g0:6d}s : lag={lag:+.3f}s  corr={c:.3f}")
        else:
            print(f"  全域 {g0:6d}s : (資料不足/相關過低)")
    if len(lag_pts) >= 2:
        gs_arr = np.array([p[0] for p in lag_pts], dtype=float)
        lg_arr = np.array([p[1] for p in lag_pts], dtype=float)
        slope, intercept = np.polyfit(gs_arr, lg_arr, 1)
        print(f"  lag 隨時間線性擬合：slope={slope:.3e} s/s、截距={intercept:+.3f}s")
        print(f"    端到端漂移≈{slope*25100:+.2f}s。若 slope≈0 → 常數偏移(可加固定 offset 修)；"
              f"若顯著 → fs 比例問題。")

    # ---- 2. 事件對齊：Single Snore 對 EDF vs NDF 的包絡峰值位移 ----
    evs = rec.get_xls_events() or []
    snore_evs = [x for x in evs if str(x.get("type", "")).strip().lower() == "single snore"]
    snore_evs.sort(key=lambda x: x.get("rel_start", 0.0))
    grid = np.arange(-W, W + 1e-9, STEP)

    def composite_peak(ch):
        comp = np.zeros_like(grid)
        cnt = 0
        best_list = []
        for ev in snore_evs:
            rs = float(ev.get("rel_start", 0.0))
            prof = np.array([env_at_global(ch, rs + d) for d in grid])
            if np.all(np.isnan(prof)):
                continue
            mx = np.nanmax(prof)
            if not (mx > 0):
                continue
            comp += np.nan_to_num(prof / mx, nan=0.0)
            cnt += 1
            best_list.append(grid[int(np.nanargmax(prof))])
        if cnt == 0:
            return None
        comp /= cnt
        return {"n": cnt, "peak": grid[int(np.argmax(comp))],
                "median": float(np.median(best_list)),
                "within1": float(np.mean(np.abs(best_list) <= 1.0) * 100)}

    pe = composite_peak(e)
    pn = composite_peak(n)
    print("\n" + "=" * 78)
    print("Single Snore 事件對齊（位移=訊號峰值相對標記；0=吻合）")
    print("=" * 78)
    for tag, p in (("snore(EDF)", pe), ("snore(NDF)", pn)):
        if p:
            print(f"  {tag}: n={p['n']} 複合峰值位移={p['peak']:+.3f}s "
                  f"中位數={p['median']:+.3f}s |位移|<=1s={p['within1']:.1f}%")
    if pe and pn:
        print(f"\n  → EDF 複合位移 {pe['peak']:+.2f}s vs NDF {pn['peak']:+.2f}s；"
              f"差距 {pe['peak'] - pn['peak']:+.2f}s")
        print(f"  → 若 EDF 位移明顯非 0 而 NDF≈0，代表 EDF 絕對時間被擺偏，"
              f"應修正 EDF 來源 offset。")

    # ---- 3. 重點事件 #505~#508 細節 ----
    print("\n" + "=" * 78)
    print("重點事件 #505~#508（與截圖同段）：標記時刻 EDF/NDF 比值 + 最佳位移")
    print("=" * 78)
    for i in range(504, 508):
        if i >= len(snore_evs):
            break
        rs = float(snore_evs[i].get("rel_start", 0.0))
        abst = (origin + timedelta(seconds=rs)).strftime("%H:%M:%S.%f")[:-3]
        out = f"  #{i+1} {abst}"
        for ch in (e, n):
            prof = np.array([env_at_global(ch, rs + d) for d in grid])
            best = grid[int(np.nanargmax(prof))] if not np.all(np.isnan(prof)) else float("nan")
            at = env_at_global(ch, rs)
            base = np.nanmean([env_at_global(ch, rs - 2 + k * 0.1) for k in range(10)])
            ratio = at / base if base and base > 0 else float("nan")
            out += f"   {ch['name']}:比值={ratio:.2f} 位移={best:+.2f}s"
        print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
