#!/usr/bin/env python3
"""Find samples-per-record values in Nox EDF header."""
from __future__ import annotations

import re
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
data_size = edf.stat().st_size - header_bytes

print(f"n_sigs={n_sigs} n_records={n_records} dur_rec={dur_rec} data_size={data_size}")
print(f"bytes/record if n_records correct = {data_size / n_records:.1f}")
print(f"samples/record total = {data_size / n_records / 2:.1f}")

# Scan every 8-byte field in signal headers for plausible spr integers
common_spr = {1, 3, 10, 20, 30, 75, 200, 250, 750, 1000, 2000}
found = []
with open(edf, "rb") as f:
    f.read(256)
    for si in range(n_sigs):
        sh = f.read(256)
        for off in range(0, 256, 8):
            chunk = sh[off : off + 8].decode("ascii", errors="replace").strip()
            try:
                val = int(float(chunk))
                if val in common_spr or (dur_rec > 0 and 0 < val <= 20000 and val % 10 == 0):
                    found.append((si, off, val, sh[0:16].decode("ascii", errors="replace").strip()))
            except ValueError:
                pass

print(f"\nPlausible numeric 8-byte fields: {len(found)}")
for item in found[:40]:
    print(f"  sig={item[0]:2d} off={item[1]:3d} val={item[2]:5d} label0={item[3]!r}")

# Try: use ONLY standard offset 216:224 or other offsets from EDF+ spec variants
# EDF+D uses different layout; check reserved field in main header
reserved = main[192:236].decode("ascii", errors="replace")
print(f"\nMain reserved field: {reserved!r}")

# Brute: try matching record size by scanning for factorization
target = int(data_size / n_records)
print(f"\nTarget record bytes = {target}, target samples = {target//2}")
# factor target/2 into common spr values?

# Read SETUP-based channel order from NDF file list
raw_dir = Path(
    r"C:\Users\dominic.hong\Projects\260611EDFfilereader\input\D18_17923057_PSG_20260511"
    r"\D18_17923057_PSG raw data_20260511\20260511T220227 - 801f4"
)
ndf_files = sorted(raw_dir.glob("*.ndf"))
print(f"\nNDF files: {len(ndf_files)}")

# Compare total ndf samples in one 10s window vs edf
# total ndf samples for 10s = sum(fs*10)
setup = raw_dir / "SETUP.INI"
fs_map = {}
for line in setup.read_text(encoding="utf-8", errors="ignore").splitlines():
    if "=" not in line:
        continue
    k, v = line.split("=", 1)
    parts = [x.strip() for x in v.split(";")]
    try:
        fs = float(parts[2]) if len(parts) > 2 and parts[2] else 0.0
    except ValueError:
        fs = 0.0
    fs_map[parts[0].lower()] = fs

ndf_spr_sum = 0
for nf in ndf_files:
    stem = nf.stem.lower()
    fs = fs_map.get(stem, 0)
    if fs <= 0:
        for k, v in fs_map.items():
            if k in stem or stem in k:
                fs = v
                break
    if fs <= 0:
        fs = 1.0
    ndf_spr_sum += int(fs * dur_rec)
print(f"SETUP+ndf estimated spr sum per record = {ndf_spr_sum}")
print(f"Expected record bytes = {ndf_spr_sum * 2}")
print(f"Match n_records? {data_size / (ndf_spr_sum*2):.2f} records")