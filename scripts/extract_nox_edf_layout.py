#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import numpy as np

edf = Path(
    r"C:\Users\dominic.hong\Projects\260611EDFfilereader\input"
    r"\D18_17923057_PSG_20260511\D18_17923057_PSG edf_20260511.edf"
)
raw_dir = Path(
    r"C:\Users\dominic.hong\Projects\260611EDFfilereader\input\D18_17923057_PSG_20260511"
    r"\D18_17923057_PSG raw data_20260511\20260511T220227 - 801f4"
)

with open(edf, "rb") as f:
    main = f.read(256)
n_sigs = int(main[252:256].decode("ascii").strip())
header_bytes = int(main[184:192].decode("ascii").strip())
n_records = int(main[236:244].decode("ascii").strip())
dur_rec = float(main[244:252].decode("ascii").strip())

with open(edf, "rb") as f:
    f.read(256)
    headers = [f.read(256) for _ in range(n_sigs)]

# Labels: 16-byte slots in first 6 signal headers
labels = []
for si in range(6):
    sh = headers[si]
    for off in range(0, 256, 16):
        lab = sh[off : off + 16].decode("ascii", errors="replace").strip()
        labels.append(lab)
labels = labels[:n_sigs]
while len(labels) < n_sigs:
    labels.append("")

# SPR: sequential numeric fields in sig 77-80
spr = []
for si in range(77, 81):
    sh = headers[si]
    for off in range(0, 256, 8):
        chunk = sh[off : off + 8].decode("ascii", errors="replace").strip()
        try:
            val = int(float(chunk))
            if val > 0:
                spr.append(val)
        except ValueError:
            pass
spr = spr[:n_sigs]

print(f"labels={len(labels)} spr={len(spr)} spr_sum={sum(spr)}")
for i, (lab, s) in enumerate(zip(labels, spr)):
    fs = s / dur_rec if dur_rec else 0
    print(f"  [{i:2d}] {lab:22s} spr={s:5d} fs={fs:7.2f}")

# Verify record size
record_bytes = sum(spr) * 2
data_size = edf.stat().st_size - header_bytes
print(f"\nrecord_bytes={record_bytes} data_records={data_size/record_bytes:.2f} (header says {n_records})")

# Find C3 and compare with NDF
c3_idx = next(i for i, l in enumerate(labels) if l.lower() == "c3")
ch_off = sum(spr[:c3_idx]) * 2
with open(edf, "rb") as f:
    f.seek(header_bytes + ch_off)
    edf_c3 = np.frombuffer(f.read(spr[c3_idx] * 2), dtype="<i2")

# NDF c3 with header strip
ndf_path = raw_dir / "c3.ndf"
raw = np.fromfile(ndf_path, dtype="<i2", count=8000).astype(float)
off = 0
for i in range(0, len(raw) - 200, 5):
    seg = raw[i : i + 200]
    if 5 < np.std(seg) < 400 and np.max(np.abs(seg)) < 2000:
        off = i
        break
ndf_c3 = np.fromfile(ndf_path, dtype="<i2", offset=off * 2, count=len(edf_c3))
# skip zeros at start of edf
nz = np.argmax(edf_c3 != 0) if np.any(edf_c3 != 0) else 0
print(f"\nC3 idx={c3_idx} edf_nz_start={nz}")
edf_seg = edf_c3[nz : nz + 2000].astype(float)
ndf_seg = ndf_c3[:2000].astype(float)
print(f"EDF first nonzero10: {edf_c3[nz:nz+10]}")
print(f"NDF first10: {ndf_c3[:10]}")
if len(edf_seg) == len(ndf_seg):
    corr = np.corrcoef(edf_seg, ndf_seg)[0, 1]
    print(f"corr={corr:.4f} mad={np.mean(np.abs(edf_seg-ndf_seg)):.2f}")