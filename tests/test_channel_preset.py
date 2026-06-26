#!/usr/bin/env python3
"""Tests for DeepNYX default channel matching (case/space insensitive)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from psg_viewer import CLINICAL_DEFAULT_CHANNELS, channel_matches_preset
from noxreader import NoxRecording


def test_channel_matches_preset_case_insensitive():
    assert channel_matches_preset("Nasal Pressure", CLINICAL_DEFAULT_CHANNELS)
    assert channel_matches_preset("Thermistor", CLINICAL_DEFAULT_CHANNELS)
    assert channel_matches_preset("snore", CLINICAL_DEFAULT_CHANNELS)
    assert channel_matches_preset("SpO2", ["spo2"])
    assert not channel_matches_preset("C3", CLINICAL_DEFAULT_CHANNELS)


def test_d18_clinical_defaults_cover_edf_channels():
    root = Path("input/D18_17923057_PSG_20260511")
    if not root.is_dir():
        print("[SKIP] D18 input data not present")
        return
    rec = NoxRecording(root)
    names = [c["name"] for c in rec.channels]
    matched = [n for n in names if channel_matches_preset(n, CLINICAL_DEFAULT_CHANNELS)]
    for want in ("snore", "spo2"):
        assert any(channel_matches_preset(n, [want]) for n in names), f"missing {want}"
    assert any(channel_matches_preset(n, ["nasal pressure"]) for n in names)
    assert any(channel_matches_preset(n, ["thermistor"]) for n in names)
    assert len(matched) >= 4, f"expected >=4 DeepNYX channels, got {matched}"


if __name__ == "__main__":
    test_channel_matches_preset_case_insensitive()
    test_d18_clinical_defaults_cover_edf_channels()
    print("test_channel_preset: OK")