#!/usr/bin/env python3
"""確認 D18 錄音尾段：NDF 感測通道在 ~04:57 結束、EDF 到 05:01，
以及超出 snore 資料末端的 Single Snore 事件。回答「snore 末段無資料是否正確」。
"""
from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from noxreader.recording import NoxRecording, _edf_duration_sec  # noqa: E402

PATIENT = ROOT / "input" / "D18_17923057_PSG_20260511"


def end_abs(origin, sec):
    return (origin + timedelta(seconds=float(sec))).strftime("%H:%M:%S.%f")[:-3]


def main() -> int:
    rec = NoxRecording(PATIENT)
    origin = rec.start_datetime

    print("=" * 78)
    print("各通道資料末端（全域 = offset + duration）")
    print("=" * 78)
    checks = ["snore", "audio volume", "spo2", "flow", "c3", "position",
              "Nasal Pressure", "Thermistor"]
    seen = set()
    for nm in checks:
        info = rec.get_channel_info(nm)
        if not info:
            print(f"  {nm:18s} (找不到)")
            continue
        fs = float(info.get("inferred_fs") or info.get("fs") or 0.0)
        samples = int(info.get("samples", 0))
        dur = samples / fs if fs > 0 else 0.0
        off = rec.channel_offset_sec(nm)
        src = info.get("source", "ndf")
        print(f"  {nm:18s} src={src:3s} fs={fs:8.3f} off={off:6.2f} "
              f"dur={dur:9.2f}s  末端={end_abs(origin, off + dur)}")

    edf_dur = _edf_duration_sec(rec.edf_path) if rec.edf_path else 0.0
    print(f"\n  EDF duration   = {edf_dur:.1f}s  末端={end_abs(origin, edf_dur)}")
    print(f"  total_span_sec = {rec.total_span_sec:.1f}s  末端={end_abs(origin, rec.total_span_sec)}")

    # snore 資料末端
    sinfo = rec.get_channel_info("snore")
    sfs = float(sinfo.get("inferred_fs") or sinfo.get("fs"))
    s_end = rec.channel_offset_sec("snore") + int(sinfo["samples"]) / sfs

    evs = rec.get_xls_events() or []
    snore_evs = [e for e in evs
                 if str(e.get("type", "")).strip().lower() == "single snore"]
    snore_evs.sort(key=lambda e: e.get("rel_start", 0.0))
    past = [(i + 1, e) for i, e in enumerate(snore_evs)
            if float(e.get("rel_start", 0.0)) > s_end]
    print("\n" + "=" * 78)
    print(f"snore 資料末端 = {end_abs(origin, s_end)}（全域 {s_end:.2f}s）")
    print(f"超出 snore 資料末端的 Single Snore 事件數 = {len(past)} / {len(snore_evs)}")
    print("=" * 78)
    for idx, e in past:
        rs = float(e.get("rel_start", 0.0))
        print(f"  #{idx}  {end_abs(origin, rs)}  (超出 snore 末端 {rs - s_end:+.1f}s, "
              f"但 < EDF 末端 {edf_dur:.0f}s? {'是' if rs < edf_dur else '否'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
