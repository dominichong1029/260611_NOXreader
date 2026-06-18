#!/usr/bin/env python3
"""
test_device_ini.py
驗證 _parse_device_ini 修復：手動 section 解析成功取得 [DeviceInfo]，device_info 非空。
使用 sample bytes / 暫存 ini 測試，不依賴真實資料。
"""

from pathlib import Path
import tempfile
import os
import sys

# 加入父目錄以 import
sys.path.insert(0, str(Path(__file__).parent.parent))

from noxreader.recording import NoxRecording, find_raw_data_dir


def _make_minimal_raw_dir(tmp: Path):
    """建立最小 raw dir 結構 + SETUP + DEVICE + 一個小 ndf（供 NoxRecording 成功 init）。"""
    raw = tmp / "raw"
    raw.mkdir()
    # SETUP.INI 簡單
    (raw / "SETUP.INI").write_text("EXG1=C3;EEG-C3;200;V\n", encoding="utf-8")
    # DEVICE.INI 含 [DeviceInfo] + [Channels] list（模擬真實導致 parse fail 的格式）
    dev_content = """[Analogboard]
Type=FWPIC7

[DeviceInfo]
Type=A1
SerialNumber=972903069
Licensee=Generic

[Channels]
Light 1Hz lx
Acceleration_X 25Hz g
EXG1 25Hz V
"""
    (raw / "DEVICE.INI").write_text(dev_content, encoding="utf-8")
    # 足夠 ndf（>5）讓 find_raw_data_dir 成功 + 小 c3
    import numpy as np

    data = np.arange(1000, dtype="<i2")
    (raw / "c3.ndf").write_bytes(data.tobytes())
    for i in range(5):
        d = np.arange(100, dtype="<i2")
        (raw / f"dummy{i}.ndf").write_bytes(d.tobytes())
    return raw


def test_device_ini_parses_reliable_sections():
    with tempfile.TemporaryDirectory() as td:
        patient = Path(td) / "DTEST"
        patient.mkdir()
        raw = _make_minimal_raw_dir(patient)
        # 強制 raw_dir
        rec = NoxRecording(patient)  # 會用 find 找到
        assert rec.raw_dir is not None and rec.raw_dir.exists()
        di = rec.device_info
        print("device_info keys:", list(di.keys()))
        assert "DeviceInfo" in di, "應成功解析 [DeviceInfo]"
        assert di["DeviceInfo"].get("Type") == "A1"
        assert di["DeviceInfo"].get("SerialNumber") == "972903069"
        assert (
            "Channels" not in di or len(di.get("Channels", {})) == 0
        ), "[Channels] list 應被跳過，device_info 非空"
        assert len(di) > 0, "device_info 應非空（至少 DeviceInfo）"
        print("test_device_ini_parses_reliable_sections: PASS")


if __name__ == "__main__":
    test_device_ini_parses_reliable_sections()
    print("All device_ini tests passed.")
