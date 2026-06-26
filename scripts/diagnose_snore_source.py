#!/usr/bin/env python3
"""比較 Single Snore 事件對「snore」vs「audio volume(db)」通道的對齊品質。

目的：#866~#868 在 snore.ndf 上看起來無能量，但 snore 通道時間軸已證實正確。
若這些事件對音訊通道對得更好，即證明事件源自音訊偵測，snore.ndf 沒有時間問題。
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
CANDIDATES = ["snore", "audio volume db", "audio volume"]
W = 4.0
STEP = 0.05
ENV_HALF = 0.20


def build_env(rec, name):
    info = rec.get_channel_info(name)
    if info is None:
        return None
    fs = float(info.get("inferred_fs") or info.get("fs") or 0.0)
    if fs <= 0:
        return None
    off = rec.channel_offset_sec(name)
    data, _ = rec.get_data(name, start_sec=0.0, duration=None, as_float=True)
    data = np.asarray(data, dtype=np.float64)
    data -= np.median(data)
    half = max(1, int(round(ENV_HALF * fs)))
    return {"fs": fs, "off": off, "data": data, "half": half,
            "fmt": info.get("format"), "samples": len(data)}


def env_rms(ch, local_t):
    c = int(round(local_t * ch["fs"]))
    a, b = c - ch["half"], c + ch["half"]
    if a < 0 or b > len(ch["data"]):
        return np.nan
    seg = ch["data"][a:b]
    return float(np.sqrt(np.mean(seg * seg)))


def main() -> int:
    rec = NoxRecording(PATIENT)
    origin = rec.start_datetime
    evs = rec.get_xls_events() or []
    snore_evs = [e for e in evs
                 if str(e.get("type", "")).strip().lower() == "single snore"]
    snore_evs.sort(key=lambda e: e.get("rel_start", 0.0))

    chans = {}
    for nm in CANDIDATES:
        ch = build_env(rec, nm)
        if ch is None:
            print(f"[略過] 找不到通道或無資料：{nm}")
            continue
        chans[nm] = ch
        print(f"[通道] {nm:18s} fs={ch['fs']:.3f} off={ch['off']:.2f}s "
              f"fmt={ch['fmt']} samples={ch['samples']}")

    grid = np.arange(-W, W + 1e-9, STEP)

    print("\n" + "=" * 78)
    print("整體對齊品質（1258 個 Single Snore 事件）")
    print("=" * 78)
    print(f"  指標：複合包絡峰值位移、|位移|<=1s 比例、比值>1.5(明顯高於背景)比例")
    for nm, ch in chans.items():
        composite = np.zeros_like(grid)
        n = 0
        within1 = 0
        strong = 0
        for e in snore_evs:
            local = float(e.get("rel_start", 0.0)) - ch["off"]
            prof = np.array([env_rms(ch, local + d) for d in grid])
            if np.all(np.isnan(prof)):
                continue
            mx = np.nanmax(prof)
            if not (mx > 0):
                continue
            n += 1
            composite += np.nan_to_num(prof / mx, nan=0.0)
            best = grid[int(np.nanargmax(prof))]
            if abs(best) <= 1.0:
                within1 += 1
            at_mark = env_rms(ch, local)
            base = np.nanmean([env_rms(ch, local - 2 + k * 0.1) for k in range(10)])
            if base and base > 0 and (at_mark / base) > 1.5:
                strong += 1
        if n:
            composite /= n
            peak = grid[int(np.argmax(composite))]
            print(f"  {nm:18s} n={n:4d}  峰值位移={peak:+.2f}s  "
                  f"|位移|<=1s={within1/n*100:5.1f}%  比值>1.5={strong/n*100:5.1f}%")

    print("\n" + "=" * 78)
    print("重點事件 #865~#869：各通道標記時刻 RMS / baseline 比值")
    print("=" * 78)
    header = "  #    絕對時間     " + "".join(f"{nm[:12]:>14}" for nm in chans)
    print(header)
    for i in range(864, 869):
        if i >= len(snore_evs):
            break
        e = snore_evs[i]
        rs = float(e.get("rel_start", 0.0))
        cells = ""
        for nm, ch in chans.items():
            local = rs - ch["off"]
            at_mark = env_rms(ch, local)
            base = np.nanmean([env_rms(ch, local - 2 + k * 0.1) for k in range(10)])
            ratio = (at_mark / base) if base and base > 0 else float("nan")
            cells += f"{ratio:>14.2f}"
        abst = (origin + timedelta(seconds=rs)).strftime("%H:%M:%S.%f")[:-3]
        print(f"  #{i+1} {abst}{cells}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
