#!/usr/bin/env python3
"""Diagnose event XLS loading for D18 vs D20."""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from noxreader.recording import NoxRecording


def diagnose(name: str) -> None:
    rec = NoxRecording(ROOT / "input" / name)
    print("=" * 70)
    print(name)
    print("start_datetime:", rec.start_datetime)
    print("channels:", len(rec.channels))

    t0 = time.perf_counter()
    xls = rec._parse_xls_events()
    t1 = time.perf_counter()
    print(f"_parse_xls_events: {len(xls)} events in {t1 - t0:.2f}s")
    if xls:
        print("  first type:", xls[0].get("type"), "rel_start:", xls[0].get("rel_start"))

    t0 = time.perf_counter()
    all_ev = rec.get_events(include_xls=True)
    t1 = time.perf_counter()
    print(f"get_events: {len(all_ev)} total in {t1 - t0:.2f}s")

    raw_chans = [c["name"] for c in rec.channels]
    t0 = time.perf_counter()
    matched = 0
    unmatched = 0
    for ev in all_ev:
        if ev.get("_source") != "xls":
            continue
        xls_name = str(ev.get("location", "") or ev.get("type", "") or "").strip()
        if xls_name in raw_chans:
            matched += 1
        else:
            unmatched += 1
    t1 = time.perf_counter()
    print(f"xls exact channel match: matched={matched}, unmatched={unmatched} ({t1 - t0:.2f}s)")


if __name__ == "__main__":
    for pid in ("D18_17923057_PSG_20260511", "D20_18075135_PSG_20260512"):
        diagnose(pid)