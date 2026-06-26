#!/usr/bin/env python3
"""判斷 EDF 相對 NDF 的 -0.74s 偏移是否為『所有 EDF 通道共通』。
對多個 EDF/NDF 重疊通道做互相關，比較各自 lag。
共通 → EDF 全域時間基偏移（一次修）；不一 → 各通道問題。
"""
from __future__ import annotations
import os, sys
from pathlib import Path
import numpy as np

os.environ.setdefault("PYTHONIOENCODING", "utf-8")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from noxreader.recording import NoxRecording  # noqa: E402

D18 = ROOT / "input" / "D18_17923057_PSG_20260511"
BASES = ["snore", "flow", "c3", "audio volume", "rip flow", "mask pressure"]
GRID_FS = 50.0


def load(rec, name):
    info = rec.get_channel_info(name)
    if not info:
        return None
    fs = float(info.get("inferred_fs") or info.get("fs") or 0.0)
    off = rec.channel_offset_sec(name)
    data, _ = rec.get_data(name, start_sec=0.0, duration=None, as_float=True)
    data = np.asarray(data, dtype=np.float64) - np.median(data)
    return {"fs": fs, "off": off, "data": np.abs(data)}


def env(ch, g, half):
    c = int(round((g - ch["off"]) * ch["fs"]))
    a, b = c - half, c + half
    if a < 0 or b > len(ch["data"]):
        return np.nan
    return float(np.mean(ch["data"][a:b]))


def lag_between(a, b, g0=5000.0, span=200.0):
    half_a = max(1, int(0.2 * a["fs"]))
    half_b = max(1, int(0.2 * b["fs"]))
    gs = np.arange(g0, g0 + span, 1.0 / GRID_FS)
    ea = np.array([env(a, g, half_a) for g in gs])
    eb = np.array([env(b, g, half_b) for g in gs])
    ok = np.isfinite(ea) & np.isfinite(eb)
    if ok.sum() < len(gs) * 0.5:
        return None, None
    ea = np.nan_to_num(ea * ok); eb = np.nan_to_num(eb * ok)
    ea -= ea.mean(); eb -= eb.mean()
    corr = np.correlate(ea, eb, mode="full")
    lags = (np.arange(corr.size) - (len(eb) - 1)) / GRID_FS
    denom = (np.sqrt((ea**2).sum()) * np.sqrt((eb**2).sum())) or 1.0
    return lags[int(np.argmax(corr))], float(corr.max() / denom)


def main() -> int:
    rec = NoxRecording(D18)
    names = {c["name"].lower(): c["name"] for c in rec.channels}
    print(f"{'base':16} {'EDF相對NDF lag':>16} {'corr':>7}")
    print("-" * 44)
    for base in BASES:
        ne = names.get(f"{base}(edf)")
        nn = names.get(f"{base}(ndf)")
        if not ne or not nn:
            print(f"{base:16} (非重疊或不存在)")
            continue
        ce = load(rec, ne); cn = load(rec, nn)
        if not ce or not cn:
            print(f"{base:16} (讀取失敗)")
            continue
        lag, c = lag_between(ce, cn)
        if lag is None:
            print(f"{base:16} (資料不足)")
        else:
            print(f"{base:16} {lag:+16.3f} {c:7.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
