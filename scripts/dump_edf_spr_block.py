#!/usr/bin/env python3
from pathlib import Path

edf = Path(
    r"C:\Users\dominic.hong\Projects\260611EDFfilereader\input"
    r"\D18_17923057_PSG_20260511\D18_17923057_PSG edf_20260511.edf"
)

with open(edf, "rb") as f:
    main = f.read(256)
n_sigs = int(main[252:256].decode("ascii").strip())

with open(edf, "rb") as f:
    f.read(256)
    headers = [f.read(256) for _ in range(n_sigs)]

for si in range(70, n_sigs):
    sh = headers[si]
    print(f"\n=== Signal header {si} ===")
    print(f"label@0: {sh[0:16].decode('ascii', errors='replace')!r}")
    nums = []
    for off in range(0, 256, 8):
        chunk = sh[off : off + 8].decode("ascii", errors="replace").strip()
        try:
            val = int(float(chunk))
            nums.append((off, val))
        except ValueError:
            pass
    print("numeric 8-byte fields:", nums)
    if nums:
        print("sum:", sum(v for _, v in nums))

# Try sig 77-79 as spr source: collect all numeric values sequentially
all_spr = []
for si in range(77, 82):
    sh = headers[si]
    for off in range(0, 256, 8):
        chunk = sh[off : off + 8].decode("ascii", errors="replace").strip()
        try:
            val = int(float(chunk))
            if val > 0:
                all_spr.append(val)
        except ValueError:
            pass
print(f"\nSequential nums sig77-81: {all_spr}")
print(f"sum={sum(all_spr)} count={len(all_spr)}")