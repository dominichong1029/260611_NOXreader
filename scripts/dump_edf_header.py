#!/usr/bin/env python3
from pathlib import Path

edf = Path(r"C:\Users\dominic.hong\Projects\260611EDFfilereader\input\D18_17923057_PSG_20260511\D18_17923057_PSG edf_20260511.edf")
with open(edf, "rb") as f:
    f.read(256)
    sh = f.read(256)

print("=== Signal 0 raw header dump ===")
for off in range(0, 256, 8):
    chunk = sh[off : off + 8]
    asc = chunk.decode("ascii", errors="replace")
    print(f"{off:3d}-{off+7:3d}: {chunk!r}  | {asc!r}")

with open(edf, "rb") as f:
    f.read(256)
    for i in range(92):
        sh = f.read(256)
        lab = sh[0:16].decode("ascii", errors="replace").strip()
        if lab.lower() in ("c3", "flow", "spo2", "e1"):
            print(f"\n=== Signal {i} label={lab!r} ===")
            fields = [
                (0, "label"),
                (16, "transducer"),
                (80, "phys_dim"),
                (88, "phys_min"),
                (96, "phys_max"),
                (104, "dig_min"),
                (112, "dig_max"),
                (120, "prefilter"),
                (128, "samples/record"),
                (136, "reserved"),
            ]
            for off, name in fields:
                chunk = sh[off : off + 8]
                print(f"  {name:16s} @{off:3d}: {chunk.decode('ascii', errors='replace')!r}")