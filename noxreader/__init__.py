"""
noxreader
最佳化的 Nox PSG (EDF/raw .ndf) 檔案讀取方案
支援兩個 input 資料夾的儀器檢測資料，以低記憶體、高效能方式開啟讀取。
"""

from .recording import NoxRecording, NoxEdfRecording, NoxStudy, export_to_standard_edf
from .nox_edf import NoxEdfHeader, parse_nox_edf, read_nox_edf_channel

__all__ = [
    "NoxRecording",
    "NoxEdfRecording",
    "NoxStudy",
    "NoxEdfHeader",
    "parse_nox_edf",
    "read_nox_edf_channel",
    "export_to_standard_edf",
]
__version__ = "0.1.0"