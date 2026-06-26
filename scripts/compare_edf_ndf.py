#!/usr/bin/env python3
"""Compare EDF raw data with NDF memmap for same channel."""
from __future__ import annotations

import re
from pathlib import Path
import numpy as np

ROOT = Path(r"C:\Users\dominic.hong\Projects\260611EDFfilereader\input\D18_17923057_PSG_20260511")
edf = ROOT / "D18_17923057_PSG edf_20260511.edf"
raw_dir = ROOT / "D18_17923057_PSG raw data_20260511" / "20260511T220227 - 801f4"
setup = raw_dir / "SETUP.INI"

# Parse SETUP for fs
meta = {}
for line in setup.read_text(encoding="utf-8", errors="ignore").splitlines():
    if "=" not in line:
        continue
    k, v = line.split("=", 1)
    parts = [x.strip() for x in v.split(";")]
    disp = parts[0]
    try:
        fs = float(parts[2]) if len(parts) > 2 and parts[2] else 0.0
    except ValueError:
        fs = 0.0
    meta[disp.lower()] = fs

# Extract packed labels from EDF header
with open(edf, "rb") as f:
    main = f.read(256)
n_sigs = int(main[252:256].decode("ascii").strip())
header_bytes = int(main[184:192].decode("ascii").strip())
n_records = int(main[236:244].decode("ascii").strip())
dur_rec = float(main[244:252].decode("ascii").strip())

with open(edf, "rb") as f:
    f.read(256)
    sig_region = f.read(n_sigs * 256)

labels = []
for off in range(0, len(sig_region) - 15, 16):
    txt = sig_region[off : off + 16].decode("ascii", errors="replace").strip()
    if txt and any(c.isalpha() for c in txt) and not re.match(r"^[-0-9.]+$", txt):
        # skip pure numeric garbage and unit-only rows
        if txt not in labels and "Ohm" not in txt and "cmH2O" not in txt and "bpm" not in txt:
            if len(txt) >= 2 or txt.isdigit():
                labels.append(txt)

# filter impedance duplicates for signal channels only - keep all for layout
print(f"Packed labels found: {len(labels)}")
print("First 30:", labels[:30])

# Build spr from SETUP fs * dur_rec
spr = []
for lab in labels:
    fs = meta.get(lab.lower(), 0.0)
    if fs <= 0:
        # partial match
        for k, v in meta.items():
            if k in lab.lower() or lab.lower() in k:
                fs = v
                break
    if fs <= 0:
        if "imped" in lab.lower():
            fs = 1.0
        elif lab.lower() in ("c3", "c4", "e1", "e2", "flow", "ecg"):
            fs = 200.0
        elif "spo2" in lab.lower() or "saturation" in lab.lower():
            fs = 3.0
        elif "rip" in lab.lower() or "abdomen" in lab.lower() or "thor" in lab.lower():
            fs = 20.0
        else:
            fs = 1.0
    spr.append(int(round(fs * dur_rec)))

record_samples = sum(spr)
record_bytes = record_samples * 2
data_size = edf.stat().st_size - header_bytes
expected_records = data_size // record_bytes
print(f"\nComputed record_samples={record_samples} record_bytes={record_bytes}")
print(f"data_size={data_size} expected_records={expected_records} header_n_records={n_records}")

# Find C3 index in labels
c3_idx = None
for i, lab in enumerate(labels):
    if lab.lower() == "c3":
        c3_idx = i
        break
print(f"\nC3 index in packed labels: {c3_idx}, spr={spr[c3_idx] if c3_idx is not None else 'N/A'}")

if c3_idx is not None:
    ch_offset = sum(spr[:c3_idx]) * 2
    # read first record C3 slice
    with open(edf, "rb") as f:
        f.seek(header_bytes + ch_offset)
        edf_c3 = np.frombuffer(f.read(spr[c3_idx] * 2), dtype="<i2")

    ndf_c3 = raw_dir / "c3.ndf"
    ndf_raw = np.fromfile(ndf_c3, dtype="<i2", count=5000).astype(float)
    # skip header junk ~50-650
    for off in range(0, 500, 5):
        seg = ndf_raw[off : off + 200]
        if 5 < np.std(seg) < 400:
            ndf_start = off
            break
    else:
        ndf_start = 0
    ndf_c3 = np.fromfile(ndf_c3, dtype="<i2", offset=ndf_start * 2, count=len(edf_c3))

    print(f"EDF C3 first10: {edf_c3[:10]}")
    print(f"NDF C3 first10: {ndf_c3[:10]}")
    print(f"Correlation (first {min(2000,len(edf_c3))}): {np.corrcoef(edf_c3[:2000].astype(float), ndf_c3[:2000].astype(float))[0,1]:.4f}")
    print(f"Mean abs diff: {np.mean(np.abs(edf_c3[:2000].astype(float) - ndf_c3[:2000].astype(float))):.2f}")