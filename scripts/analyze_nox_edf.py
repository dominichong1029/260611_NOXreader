#!/usr/bin/env python3
"""Analyze Nox proprietary EDF header layout vs standard EDF."""
from __future__ import annotations

from pathlib import Path
import numpy as np

edf = Path(
    r"C:\Users\dominic.hong\Projects\260611EDFfilereader\input"
    r"\D18_17923057_PSG_20260511\D18_17923057_PSG edf_20260511.edf"
)

with open(edf, "rb") as f:
    main = f.read(256)

n_sigs = int(main[252:256].decode("ascii").strip())
header_bytes = int(main[184:192].decode("ascii").strip())
n_records = int(main[236:244].decode("ascii").strip())
dur_rec = float(main[244:252].decode("ascii").strip())

print(f"n_signals={n_sigs} header_bytes={header_bytes} n_records={n_records} dur_record={dur_rec}s")

# Standard EDF: labels are at 0:16 of EACH signal header
labels_std = []
spr_std = []
with open(edf, "rb") as f:
    f.read(256)
    for i in range(n_sigs):
        sh = f.read(256)
        labels_std.append(sh[0:16].decode("ascii", errors="replace").strip())
        spr_std.append(sh[128:136].decode("ascii", errors="replace").strip())

print(f"\nStandard-offset labels (0:16 per sig header), non-empty count={sum(1 for x in labels_std if x)}")
for i, (lab, spr) in enumerate(zip(labels_std, spr_std)):
    if lab:
        print(f"  [{i:2d}] label={lab!r:20s} spr={spr!r}")

# Scan ALL 16-byte chunks in combined signal header region for plausible channel names
with open(edf, "rb") as f:
    f.read(256)
    sig_region = f.read(n_sigs * 256)

print(f"\nAll non-empty 16-byte chunks in signal header region ({len(sig_region)} bytes):")
seen = set()
for off in range(0, len(sig_region) - 15, 16):
    chunk = sig_region[off : off + 16]
    txt = chunk.decode("ascii", errors="replace").strip()
    if txt and txt not in seen and any(c.isalpha() for c in txt):
        seen.add(txt)
        sig_idx = off // 256
        inner_off = off % 256
        print(f"  sig={sig_idx:2d} inner_off={inner_off:3d}: {txt!r}")

# Try reading first data record as int16
data_start = header_bytes
record_bytes = sum(int(x.strip() or 0) for x in spr_std) * 2
print(f"\nSum samples/record (from std offset 128:136) = {sum(int(x.strip() or 0) for x in spr_std)}")
print(f"Record bytes (if std spr valid) = {record_bytes}")

# Alternative: scan for spr values that are powers of common fs * dur_rec
print("\nSearching for numeric fields at standard offsets across all signal headers:")
with open(edf, "rb") as f:
    f.read(256)
    for i in range(n_sigs):
        sh = f.read(256)
        spr_s = sh[128:136].decode("ascii", errors="replace").strip()
        try:
            spr = int(spr_s)
            if spr > 0:
                print(f"  [{i}] spr={spr} label={labels_std[i]!r}")
        except ValueError:
            pass

# Read raw first 2000 int16 samples after header
raw = np.fromfile(edf, dtype="<i2", offset=data_start, count=2000)
print(f"\nFirst data record stats (from offset {data_start}):")
print(f"  n={len(raw)} min={raw.min()} max={raw.max()} mean={raw.mean():.2f} std={raw.std():.2f}")
print(f"  first 20: {raw[:20]}")