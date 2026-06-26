#!/usr/bin/env python3
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PyQt6.QtWidgets import QApplication
from noxreader.recording import NoxRecording
from psg_viewer import PSGViewer


def run(pid: str) -> None:
    app = QApplication([])
    viewer = PSGViewer()
    rec = NoxRecording(ROOT / "input" / pid)
    viewer.current_rec = rec
    viewer.max_duration = rec.duration_sec

    t0 = time.perf_counter()
    viewer._parse_and_store_events()
    t1 = time.perf_counter()
    parsed = len(viewer.parsed_events)
    counts = viewer._compute_event_counts()
    print(f"{pid}: parsed={parsed} display_groups={len(counts)} enrich_time={t1 - t0:.2f}s")
    top = sorted(counts.items(), key=lambda kv: -kv[1])[:6]
    for k, v in top:
        print(f"  {k}: {v}")
    app.quit()


if __name__ == "__main__":
    for pid in ("D18_17923057_PSG_20260511", "D20_18075135_PSG_20260512"):
        run(pid)