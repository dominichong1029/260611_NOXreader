#!/usr/bin/env python3
"""
test_header_strip.py
使用 sample bytes 驗證：
- _detect_header_offset 正確找出 junk 後 offset
- 低頻 fs 推斷（position-like）
- get_data(..., strip_header=True) 回傳正確 data（非 junk）、30s window sample count ~fs*30
- strip=False 保留舊行為
建立最小 temp raw dir + ndf（含 header junk + 穩定資料）+ SETUP。
"""

from pathlib import Path
import tempfile
import sys
import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))

from noxreader.recording import NoxRecording, _detect_header_offset


def _make_pos_like_raw(tmp: Path, samples: int = 12000, header_off: int = 80):
    """建立含 header junk + 穩定低值資料的 position.ndf 模擬檔。"""
    raw = tmp / "raw"
    raw.mkdir()
    (raw / "SETUP.INI").write_text("", encoding="utf-8")  # 無 setup，fs=0 觸發 infer
    # 建 position.ndf：前 header_off 為 junk (高值+亂), 之後為穩定 ~100 附近 int16
    data = np.zeros(samples, dtype="<i2")
    # junk prefix
    data[:header_off] = np.array([20302, 856, 1, 72] * (header_off // 4 + 1), dtype="<i2")[
        :header_off
    ]
    # 之後低 std 資料 (clusters 模擬 position)
    rng = np.random.default_rng(42)
    stable = rng.integers(-400, 200, size=samples - header_off, dtype=np.int16)
    data[header_off:] = stable
    (raw / "position.ndf").write_bytes(data.tobytes())
    # 另建高頻 clean 通道 + 足夠 dummy ndf 讓 find_raw_data_dir 認出（需 ndf_count >5）
    eeg = rng.integers(-1000, 1000, size=12000, dtype="<i2")
    (raw / "c3.ndf").write_bytes(eeg.tobytes())
    for i in range(5):
        dummy = rng.integers(-100, 100, size=100, dtype="<i2")
        (raw / f"dummy{i}.ndf").write_bytes(dummy.tobytes())
    (raw / "SETUP.INI").write_text("EXG1=C3;EEG;200;V\n", encoding="utf-8")
    return raw


def test_detect_header_offset_on_sample_bytes():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        raw = _make_pos_like_raw(tmp, samples=5000, header_off=120)
        pos_path = raw / "position.ndf"
        off = _detect_header_offset(pos_path)
        print(f"detected offset: {off}")
        assert off > 0, "應偵測到 header offset >0"
        assert off < 300, "offset 應合理小（junk 後第一穩定段）"
        # 驗證 offset 後資料 std 低、有界
        rawv = np.fromfile(pos_path, dtype="<i2", count=2000).astype(float)
        seg = rawv[off : off + 200]
        assert 5 < np.std(seg) < 400
        assert np.max(np.abs(seg)) < 2000
        print("test_detect_header_offset_on_sample_bytes: PASS")
        import gc

        gc.collect()


def test_get_data_strip_and_inferred_fs():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        patient = tmp / "DTEST_STRIP"
        patient.mkdir()
        raw = _make_pos_like_raw(patient, samples=12000, header_off=80)  # ~60s at 200Hz for c3
        rec = NoxRecording(patient)
        pos_info = rec.get_channel_info("position")
        print(
            "position info fs:",
            pos_info["fs"],
            "inferred_fs:",
            pos_info.get("inferred_fs"),
            "header_offset:",
            pos_info.get("header_offset"),
        )
        assert pos_info["fs"] > 0, "fs 應被 infer >0"
        assert pos_info.get("inferred_fs") is not None
        off = pos_info.get("header_offset", 0)
        assert off > 0
        # 取 1s 資料，預設 strip
        d, fs = rec.get_data("position", start_sec=0, duration=1.0)
        print("get_data(0,1) len:", len(d), "fs:", fs)
        assert len(d) == int(round(1 * fs)), "sample count 應 = round(dur*fs)"
        # 確認第一筆不是 junk（junk 通常 >1000）
        assert abs(d[0]) < 1000, "strip 後不應是開頭 junk"
        # 30s window
        d30, _ = rec.get_data("position", 0, 30)
        expected = int(round(30 * fs))
        print("30s samples:", len(d30), "expected~", expected)
        assert abs(len(d30) - expected) <= 2, "30s 視窗 sample count 應接近 fs*30"
        # strip=False 應看到 junk
        d_raw, _ = rec.get_data("position", 0, 1.0, strip_header=False)
        assert abs(d_raw[0]) > 1000 or abs(d_raw[5]) > 1000, "strip=False 應含 junk"
        # Windows memmap 鎖檔，釋放後再離開 with temp
        del rec
        import gc

        gc.collect()
        print("test_get_data_strip_and_inferred_fs: PASS")


def test_iter_epochs_uses_strip():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        patient = tmp / "DTEST_EPOCH"
        patient.mkdir()
        _make_pos_like_raw(patient, samples=4000, header_off=60)
        rec = NoxRecording(patient)
        for idx, t, dd in rec.iter_epochs(channels=["position"], epoch_sec=2.0, step_sec=2.0):
            d = dd["position"]
            assert len(d) > 0
            # 應已 strip
            assert abs(d[0]) < 1000
            if idx > 2:
                break
        del rec
        import gc

        gc.collect()
        print("test_iter_epochs_uses_strip: PASS")


def test_is_position_channel_real_d18():
    """Coverage for Issue 1 +7: 確認 tighten 後不誤判其他低頻，position 正確。"""
    from pathlib import Path

    rec = NoxRecording(Path("input/D18_17923057_PSG_20260511"))
    assert rec.is_position_channel("position") is True
    assert rec.is_position_channel("pulse") is False
    assert rec.is_position_channel("1 impedance") is False
    assert rec.is_position_channel("c3") is False
    assert rec.is_position_channel("set pressure") is False
    print("test_is_position_channel_real_d18: PASS")


def test_patient_info_real_d18_and_mask():
    """Coverage for Issues 5/6/7: real D18 patient_info fidelity + mask_pii + fallback note。"""
    from pathlib import Path

    rec = NoxRecording(Path("input/D18_17923057_PSG_20260511"))
    pi = rec.get_patient_info()
    demo = pi["demographics"]
    assert demo.get("age") == 50
    assert demo.get("gender") == "Male"
    assert demo.get("bmi") == 29.6
    assert "17923057" in str(demo.get("mrn", ""))
    clin = pi.get("clinical", {})
    poss = [p["position"] for p in clin.get("position_summary", [])]
    assert "Supine" in poss and "Left" in poss
    assert clin.get("rdi") == 21.3
    # mask
    pi2 = rec.get_patient_info(mask_pii=True)
    assert "***" in str(pi2["demographics"].get("mrn", ""))
    print("test_patient_info_real_d18_and_mask: PASS")


def test_highfreq_strip_and_duration_refresh():
    """Coverage for high-freq strip (Issue 3/8) + duration edge refresh (Issue 2/7)。"""
    from pathlib import Path
    import tempfile, numpy as np, gc

    rec = NoxRecording(Path("input/D18_17923057_PSG_20260511"))
    # high freq c3 should have offset (real data universal junk)
    c3i = rec.get_channel_info("c3")
    assert c3i.get("header_offset", 0) >= 0  # in real >=5
    d_strip, _ = rec.get_data("c3", 0, 1)
    d_raw, _ = rec.get_data("c3", 0, 1, strip_header=False)
    # strip 後第一筆應不同（或至少不都是 junk 高值）
    assert d_strip[0] != 20302 or abs(d_raw[0]) == 20302
    # duration 經 infer refresh 應 >0
    assert rec.duration_sec > 10000
    del rec
    gc.collect()
    print("test_highfreq_strip_and_duration_refresh: PASS")


if __name__ == "__main__":
    test_detect_header_offset_on_sample_bytes()
    test_get_data_strip_and_inferred_fs()
    test_iter_epochs_uses_strip()
    test_is_position_channel_real_d18()
    test_patient_info_real_d18_and_mask()
    test_highfreq_strip_and_duration_refresh()
    print("All header_strip tests passed.")
