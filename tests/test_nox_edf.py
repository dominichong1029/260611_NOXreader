#!/usr/bin/env python3
"""Tests for Nox proprietary EDF parser."""

from pathlib import Path
import sys

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))

from noxreader.nox_edf import parse_nox_edf, read_nox_edf_channel
from noxreader import NoxEdfRecording, NoxRecording, NoxStudy

INPUT_EDF = (
    Path(__file__).parent.parent
    / "input"
    / "D18_17923057_PSG_20260511"
    / "D18_17923057_PSG edf_20260511.edf"
)
INPUT_D01_DIR = Path(__file__).parent.parent / "input" / "D01_00431266_PSG_20260413"
INPUT_D01_EDF = INPUT_D01_DIR / "D01_00431266_PSG edf_20260413.edf"
INPUT_D01_XLSX = INPUT_D01_DIR / "D01_00431266_PSG event_20260413.xlsx"
INPUT_D02_DIR = Path(__file__).parent.parent / "input" / "D02_16238392_PSG_20260413"
INPUT_D02_EDF = INPUT_D02_DIR / "D02_16238392_PSG edf_20260413.edf"
INPUT_D03_DIR = Path(__file__).parent.parent / "input" / "D03_18056741_PSG_20260423"


def test_parse_nox_edf_header():
    if not INPUT_EDF.exists():
        print("SKIP: input EDF not found")
        return
    hdr = parse_nox_edf(INPUT_EDF)
    assert hdr.n_signals == 92
    assert hdr.n_records == 2510
    assert abs(hdr.dur_record - 10.0) < 0.01
    assert sum(hdr.samples_per_record) == 80150
    assert "C3" in hdr.labels
    c3_idx = hdr.labels.index("C3")
    assert hdr.fs_for_channel(c3_idx) == 200.0
    print("test_parse_nox_edf_header: PASS")


def test_read_nox_edf_c3_has_signal():
    if not INPUT_EDF.exists():
        print("SKIP: input EDF not found")
        return
    hdr = parse_nox_edf(INPUT_EDF)
    c3_idx = hdr.labels.index("C3")
    # 讀取第 2 個 record 附近（避開開頭全零）
    start = hdr.samples_per_record[c3_idx] * 2
    data = read_nox_edf_channel(INPUT_EDF, hdr, c3_idx, start, 500)
    assert len(data) == 500
    assert np.std(data.astype(float)) > 10
    print("test_read_nox_edf_c3_has_signal: PASS")


def test_nox_edf_recording_standalone():
    if not INPUT_EDF.exists():
        print("SKIP: input EDF not found")
        return
    rec = NoxEdfRecording(INPUT_EDF)
    assert len(rec.channels) >= 80
    assert rec.duration_sec > 20000
    data, fs = rec.get_data("C3", start_sec=20, duration=1.0)
    assert fs == 200.0
    assert len(data) == 200
    print("test_nox_edf_recording_standalone: PASS")


def test_nox_recording_merges_edf_channels():
    patient = INPUT_EDF.parent
    if not patient.exists():
        print("SKIP: patient dir not found")
        return
    rec = NoxRecording(patient)
    assert rec.edf_path is not None
    edf_only = [
        c["name"]
        for c in rec.channels
        if rec.get_channel_info(c["name"]).get("source") == "edf"
    ]
    assert len(edf_only) > 0
    # EDF-only montage channel
    assert any("C3-M2" in n for n in edf_only)
    data, fs = rec.get_data("C3-M2", start_sec=100, duration=0.5)
    assert fs == 200.0
    assert len(data) == 100
    print(f"test_nox_recording_merges_edf_channels: PASS ({len(edf_only)} edf-only ch)")


def test_nox_edf_recording_loads_companion_xls():
    if not INPUT_EDF.exists():
        print("SKIP: input EDF not found")
        return
    rec = NoxEdfRecording(INPUT_EDF)
    evs = rec.get_xls_events()
    assert len(evs) > 1000, f"expected D18 event xls, got {len(evs)}"
    assert evs[0].get("_source") == "xls"
    print(f"test_nox_edf_recording_loads_companion_xls: PASS ({len(evs)} events)")


def test_nox_study_from_edf():
    if not INPUT_EDF.exists():
        print("SKIP")
        return
    study = NoxStudy.from_edf(INPUT_EDF)
    assert len(study) == 1
    rec = study.get(study.patients[0])
    assert isinstance(rec, NoxRecording), "D18 EDF in patient folder should load full NoxRecording"
    assert len(rec.list_channels()) > 50
    assert len(rec.get_xls_events()) > 1000
    print("test_nox_study_from_edf: PASS")


def test_d02_edf_94ch_thermistor():
    """D02 EDF 為 94 通道 Nox 格式，SPR 區塊較長，需能解析 Thermistor。"""
    if not INPUT_D02_EDF.exists():
        print("SKIP: D02 EDF not found")
        return
    hdr = parse_nox_edf(INPUT_D02_EDF)
    assert hdr.n_signals == 94
    assert "Thermistor" in hdr.labels
    ti = hdr.labels.index("Thermistor")
    assert hdr.fs_for_channel(ti) == 200.0
    data = read_nox_edf_channel(INPUT_D02_EDF, hdr, ti, hdr.samples_per_record[ti] * 10, 200)
    assert len(data) == 200
    assert np.std(data.astype(float)) > 1
    print("test_d02_edf_94ch_thermistor: PASS")


def test_d02_ndf_recording_and_xlsx_events():
    if not INPUT_D02_DIR.exists():
        print("SKIP: D02 input not found")
        return
    study = NoxStudy(INPUT_D02_DIR)
    rec = study.get(study.patients[0])
    assert isinstance(rec, NoxRecording)
    assert rec.duration_sec > 20000
    evs = rec.get_events(include_xls=True)
    assert len(evs) >= 200, f"expected D02 get_events, got {len(evs)}"
    ch_names = [c.lower() for c in rec.list_channels()]
    assert "thermistor" in ch_names, f"D02 should expose Thermistor from EDF, got {rec.list_channels()}"
    data, fs = rec.get_data("Thermistor", start_sec=100, duration=0.5)
    assert len(data) > 0 and fs == 200.0
    data, fs = rec.get_data(rec.list_channels()[0], start_sec=60, duration=0.5)
    assert len(data) > 0 and fs > 0
    print(f"test_d02_ndf_recording_and_xlsx_events: PASS ({len(evs)} events)")


def test_d03_ndf_with_edf_merge_and_xlsx_events():
    if not INPUT_D03_DIR.exists():
        print("SKIP: D03 input not found")
        return
    study = NoxStudy(INPUT_D03_DIR)
    rec = study.get(study.patients[0])
    assert isinstance(rec, NoxRecording)
    assert rec.duration_sec > 3000
    evs = rec.get_xls_events()
    assert len(evs) >= 100, f"expected D03 xlsx events, got {len(evs)}"
    ch = "C3-M2" if "C3-M2" in rec.list_channels() else rec.list_channels()[0]
    data, fs = rec.get_data(ch, start_sec=60, duration=0.5)
    assert len(data) > 0 and fs > 0
    print(f"test_d03_ndf_with_edf_merge_and_xlsx_events: PASS ({len(evs)} events)")


def test_d01_standard_edf_duration_and_xlsx_events():
    """D01：標準 EDF + 損壞 openpyxl 的 xlsx 事件檔。"""
    if not INPUT_D01_EDF.exists() or not INPUT_D01_XLSX.exists():
        print("SKIP: D01 input not found")
        return
    rec = NoxEdfRecording(INPUT_D01_EDF)
    assert rec.duration_sec > 20000, f"D01 EDF duration too short: {rec.duration_sec}"
    assert rec.start_datetime is not None
    evs = rec.get_xls_events()
    assert len(evs) >= 200, f"expected D01 xlsx events, got {len(evs)}"
    assert evs[0].get("_source") == "xls"
    study = NoxStudy(INPUT_D01_DIR)
    rec2 = study.get(study.patients[0])
    assert rec2.duration_sec > 20000
    print(f"test_d01_standard_edf_duration_and_xlsx_events: PASS ({len(evs)} events)")


if __name__ == "__main__":
    test_parse_nox_edf_header()
    test_read_nox_edf_c3_has_signal()
    test_nox_edf_recording_standalone()
    test_nox_edf_recording_loads_companion_xls()
    test_nox_recording_merges_edf_channels()
    test_nox_study_from_edf()
    test_d02_edf_94ch_thermistor()
    test_d02_ndf_recording_and_xlsx_events()
    test_d03_ndf_with_edf_merge_and_xlsx_events()
    test_d01_standard_edf_duration_and_xlsx_events()
    print("All nox_edf tests passed.")