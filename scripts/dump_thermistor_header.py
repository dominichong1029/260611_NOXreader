#!/usr/bin/env python3
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from noxreader.nox_edf import NOX_SPR_HEADER_START, parse_nox_edf

FIELDS = [
    (0, 16, "label"),
    (16, 80, "transducer"),
    (80, 88, "phys_dim"),
    (88, 96, "phys_min"),
    (96, 104, "phys_max"),
    (104, 112, "dig_min"),
    (112, 120, "dig_max"),
    (120, 128, "prefilter"),
    (128, 136, "samples/record"),
    (136, 256, "reserved"),
]


def dump_signal_header(edf_path: Path, sig_idx: int, parsed_label: str) -> None:
    with open(edf_path, "rb") as f:
        f.read(256)
        sh = b""
        for i in range(sig_idx + 1):
            sh = f.read(256)

    print(f"  signal_index: {sig_idx}")
    print(f"  parsed_label (Nox packed): {parsed_label!r}")
    print("  --- standard EDF field layout (256-byte block) ---")
    for start, end, name in FIELDS:
        chunk = sh[start:end]
        asc = chunk.decode("ascii", errors="replace")
        print(f"  {name:16s} @{start:3d}-{end-1:3d}: {chunk!r}")
        print(f"                    ascii: {asc!r}")
    print("  --- full raw hex (16 bytes/line) ---")
    for off in range(0, 256, 16):
        chunk = sh[off : off + 16]
        hexs = " ".join(f"{b:02x}" for b in chunk)
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        print(f"  {off:03d}: {hexs}  |{asc}|")


def main() -> None:
    input_root = ROOT / "input"
    for pd in sorted(input_root.iterdir()):
        if not pd.is_dir():
            continue
        print("=" * 80)
        print(f"PATIENT FOLDER: {pd.name}")
        edfs = sorted(pd.glob("*.edf"))
        if not edfs:
            print("  (no .edf file in this folder)")
            continue
        for edf in edfs:
            print(f"EDF FILE: {edf.name}")
            hdr = parse_nox_edf(edf)
            matches = [
                (i, lab)
                for i, lab in enumerate(hdr.labels)
                if lab.strip().lower() == "thermistor" or "therm" in lab.lower()
            ]
            if not matches:
                print("  thermistor channel NOT FOUND")
                continue
            for i, lab in matches:
                fs = hdr.fs_for_channel(i)
                spr = hdr.samples_per_record[i]
                print(f"--- Thermistor: index={i}, label={lab!r} ---")
                print(
                    f"  samples_per_record={spr}, dur_record={hdr.dur_record}, "
                    f"fs={fs:.4f} Hz, total_samples={hdr.total_samples_for_channel(i)}"
                )
                dump_signal_header(edf, i, lab)
                label_hdr_idx = i // 16
                label_off = (i % 16) * 16
                spr_hdr_idx = NOX_SPR_HEADER_START + (i // 32)
                spr_off = (i % 32) * 8
                with open(edf, "rb") as f:
                    f.read(256)
                    headers = [f.read(256) for _ in range(hdr.n_signals)]
                label_chunk = headers[label_hdr_idx][label_off : label_off + 16]
                spr_chunk = headers[spr_hdr_idx][spr_off : spr_off + 8]
                print("  --- Nox packed label block (actual label storage) ---")
                print(f"  signal header #{label_hdr_idx}, bytes {label_off}-{label_off + 15}")
                print(f"  raw: {label_chunk!r}")
                print(f"  ascii: {label_chunk.decode('ascii', errors='replace')!r}")
                print("  --- Nox packed samples/record block (actual SPR storage) ---")
                print(f"  signal header #{spr_hdr_idx}, bytes {spr_off}-{spr_off + 7}")
                print(f"  raw: {spr_chunk!r}")
                print(f"  ascii: {spr_chunk.decode('ascii', errors='replace')!r}")
            print()


if __name__ == "__main__":
    main()