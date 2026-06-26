#!/usr/bin/env python3
"""Diagnose EDF files with multiple public libraries and binary header analysis."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def parse_edf_headers(edf_path: Path) -> dict:
    with open(edf_path, "rb") as f:
        main = f.read(256)
    n_sigs = int(main[252:256].decode("ascii", errors="ignore").strip() or 0)
    labels = []
    bad_phys = []
    s_per_record = []
    with open(edf_path, "rb") as f:
        f.read(256)
        for i in range(n_sigs):
            sh = f.read(256)
            lab = sh[0:16].decode("ascii", errors="replace").strip()
            phys_dim = sh[80:88].decode("ascii", errors="replace").strip()
            phys_min_s = sh[88:96].decode("ascii", errors="replace").strip()
            phys_max_s = sh[96:104].decode("ascii", errors="replace").strip()
            dig_min_s = sh[104:112].decode("ascii", errors="replace").strip()
            dig_max_s = sh[112:120].decode("ascii", errors="replace").strip()
            spr_s = sh[128:136].decode("ascii", errors="replace").strip()
            labels.append(lab)
            try:
                pmin = float(phys_min_s) if phys_min_s else 0.0
                pmax = float(phys_max_s) if phys_max_s else 0.0
                dmin = int(float(dig_min_s)) if dig_min_s else -32768
                dmax = int(float(dig_max_s)) if dig_max_s else 32767
                spr = int(spr_s.strip()) if spr_s.strip() else 0
                s_per_record.append(spr)
                issues = []
                if pmax <= pmin:
                    issues.append(f"phys_max<=phys_min ({pmin},{pmax})")
                if dmin >= dmax:
                    issues.append(f"dig_min>=dig_max ({dmin},{dmax})")
                if abs(pmin) > 1e9 or abs(pmax) > 1e9:
                    issues.append(f"extreme phys ({pmin},{pmax})")
                if issues:
                    bad_phys.append((i, lab, issues, pmin, pmax, dmin, dmax, spr))
            except Exception as e:
                bad_phys.append((i, lab, [str(e)], phys_min_s, phys_max_s, dig_min_s, dig_max_s, spr_s))

    header_bytes = int(main[184:192].decode("ascii", errors="ignore").strip() or 256)
    return {
        "version": main[0:8].decode("ascii", errors="replace").strip(),
        "patient": main[8:88].decode("ascii", errors="replace").strip(),
        "startdate": main[168:176].decode("ascii", errors="replace").strip(),
        "starttime": main[176:184].decode("ascii", errors="replace").strip(),
        "header_bytes": header_bytes,
        "n_records": int(main[236:244].decode("ascii", errors="ignore").strip() or 0),
        "dur_record": float(main[244:252].decode("ascii", errors="ignore").strip() or 1),
        "n_sigs": n_sigs,
        "labels": labels,
        "bad_phys": bad_phys,
        "s_per_record": s_per_record,
        "file_size": edf_path.stat().st_size,
    }


def try_pyedflib(edf_path: Path) -> str:
    try:
        import pyedflib

        r = pyedflib.EdfReader(str(edf_path))
        msg = f"SUCCESS n_sig={r.signals_in_file} duration={r.file_duration:.1f}s"
        for i in range(min(3, r.signals_in_file)):
            msg += f"\n      [{i}] {r.getLabel(i)!r} fs={r.getSampleFrequency(i)}"
        r.close()
        return msg
    except Exception as e:
        return f"FAILED: {e}"


def try_mne(edf_path: Path) -> str:
    try:
        import mne

        mne.set_log_level("ERROR")
        raw = mne.io.read_raw_edf(str(edf_path), preload=False, verbose=False)
        sfreq = raw.info["sfreq"]
        dur = raw.n_times / sfreq if sfreq else 0
        return f"SUCCESS n_ch={len(raw.ch_names)} duration={dur:.1f}s first3={raw.ch_names[:3]}"
    except ImportError:
        return "SKIP: mne not installed"
    except Exception as e:
        return f"FAILED: {e}"


def try_edfio(edf_path: Path) -> str:
    try:
        from edfio import read_edf

        edf = read_edf(edf_path)
        return f"SUCCESS n_sig={len(edf.signals)} duration={edf.duration:.1f}s"
    except ImportError:
        return "SKIP: edfio not installed"
    except Exception as e:
        return f"FAILED: {e}"


def try_noxreader(edf_path: Path) -> str:
    try:
        from noxreader.recording import NoxRecording

        patient_dir = edf_path.parent
        rec = NoxRecording(patient_dir)
        edf_ch = [c for c in rec.channels if rec.get_channel_info(c["name"]).get("source") == "edf"]
        return (
            f"patient={rec.name} ndf_ch={len(rec.channels)-len(edf_ch)} "
            f"edf_only_ch={len(edf_ch)} edf_path={rec.edf_path} duration={rec.duration_sec:.1f}s"
        )
    except Exception as e:
        return f"FAILED: {e}"


def main() -> None:
    input_dir = ROOT / "input"
    edf_files = sorted(input_dir.rglob("*.edf"))
    print(f"Found {len(edf_files)} EDF file(s)")
    for edf_path in edf_files:
        print("\n" + "=" * 80)
        print(f"FILE: {edf_path}")
        print(f"SIZE: {edf_path.stat().st_size:,} bytes")
        print("=" * 80)

        info = parse_edf_headers(edf_path)
        print(f"version={info['version']!r} patient={info['patient'][:50]!r}")
        print(
            f"start={info['startdate']} {info['starttime']} "
            f"header_bytes={info['header_bytes']} n_records={info['n_records']} "
            f"dur_record={info['dur_record']}s n_signals={info['n_sigs']}"
        )
        expected_data = info["header_bytes"] + info["n_records"] * sum(info["s_per_record"]) * 2
        print(f"expected_min_size={expected_data:,} actual={info['file_size']:,}")
        print(f"labels (first 5): {info['labels'][:5]}")
        print(f"labels (last 5): {info['labels'][-5:]}")
        print(f"bad_phys signals: {len(info['bad_phys'])}/{info['n_sigs']}")
        for item in info["bad_phys"][:8]:
            print(f"  sig[{item[0]}] {item[1]!r}: {item[2]}")

        print("\n[Method 1] pyedflib:", try_pyedflib(edf_path))
        print("[Method 2] mne:", try_mne(edf_path))
        print("[Method 3] edfio:", try_edfio(edf_path))
        print("[Method 4] NoxRecording:", try_noxreader(edf_path))


if __name__ == "__main__":
    main()