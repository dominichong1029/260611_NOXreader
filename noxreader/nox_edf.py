"""
noxreader.nox_edf
Nox A1 儀器匯出的 EDF 檔案解析器。

Nox 的 .edf 並非標準 EDF(+) 格式：
- signal header 0 起以 16-byte 間隔打包通道標籤（每 block 16 個，直到湊滿 n_signals）
- signal header 77 起以 8-byte ASCII 整數連續存放每通道 samples-per-record（可延伸至檔案
  最後一個 header block；例如 94 通道檔案的 SPR 會用到 header 93，而非早期假定的 80）
- 標準 EDF 的 phys_min/max 等欄位被通道名稱佔用，導致 pyedflib / MNE 拒絕開啟

資料區（data records）仍為標準 interleaved int16 little-endian 排列。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple, Union

import numpy as np

PathLike = Union[str, Path]

# Nox 於 signal header 區塊中開始存放 packed samples/record 的起始索引（非結束上限）
NOX_SPR_HEADER_START = 77


@dataclass
class NoxEdfHeader:
    path: Path
    version: str
    patient: str
    recording: str
    start_datetime: Optional[datetime]
    header_bytes: int
    n_records: int
    dur_record: float
    labels: List[str]
    samples_per_record: List[int]

    @property
    def n_signals(self) -> int:
        return len(self.labels)

    @property
    def record_bytes(self) -> int:
        return sum(self.samples_per_record) * 2

    @property
    def duration_sec(self) -> float:
        return self.n_records * self.dur_record

    def fs_for_channel(self, index: int) -> float:
        spr = self.samples_per_record[index]
        if self.dur_record <= 0:
            return 0.0
        return spr / self.dur_record

    def total_samples_for_channel(self, index: int) -> int:
        return self.samples_per_record[index] * self.n_records


def _parse_start_datetime(main: bytes) -> Optional[datetime]:
    startdate = main[168:176].decode("ascii", errors="ignore").strip()
    starttime = main[176:184].decode("ascii", errors="ignore").strip()
    for fmt in ("%d.%m.%y", "%d.%m.%Y"):
        try:
            d = datetime.strptime(startdate, fmt)
            break
        except ValueError:
            d = None
    if d is None:
        return None
    try:
        t = datetime.strptime(starttime, "%H.%M.%S")
        return d.replace(hour=t.hour, minute=t.minute, second=t.second)
    except ValueError:
        return d


def _signal_header_block_count(main: bytes, n_signals: int) -> int:
    """依 main header 的 header_bytes 決定要讀取多少個 256-byte signal header block。"""
    header_bytes = int(main[184:192].decode("ascii", errors="ignore").strip() or 256)
    blocks = max(0, (header_bytes - 256) // 256)
    return max(blocks, n_signals)


def _label_header_count(n_signals: int) -> int:
    """Nox packed 標籤：每個 header block 可放 16 個 label，且位於 SPR 區塊（header 77）之前。"""
    needed = (n_signals + 15) // 16
    return min(NOX_SPR_HEADER_START, needed)


def _decode_ascii_int(chunk: bytes) -> Optional[int]:
    text = chunk.decode("ascii", errors="replace").strip()
    if not text:
        return None
    try:
        val = int(float(text))
        return val if val > 0 else None
    except ValueError:
        return None


def _extract_labels_packed(signal_headers: List[bytes], n_signals: int) -> List[str]:
    labels: List[str] = []
    for si in range(_label_header_count(n_signals)):
        if si >= len(signal_headers):
            break
        sh = signal_headers[si]
        for off in range(0, 256, 16):
            labels.append(sh[off : off + 16].decode("ascii", errors="replace").strip())
            if len(labels) >= n_signals:
                return labels[:n_signals]
    return labels[:n_signals]


def _extract_labels_standard(signal_headers: List[bytes], n_signals: int) -> List[str]:
    labels: List[str] = []
    for si in range(min(n_signals, len(signal_headers))):
        labels.append(signal_headers[si][0:16].decode("ascii", errors="replace").strip())
    while len(labels) < n_signals:
        labels.append("")
    return labels[:n_signals]


def _extract_labels(signal_headers: List[bytes], n_signals: int) -> List[str]:
    packed = _extract_labels_packed(signal_headers, n_signals)
    while len(packed) < n_signals:
        packed.append("")
    packed_non_empty = sum(1 for lab in packed if lab)
    standard = _extract_labels_standard(signal_headers, n_signals)
    standard_non_empty = sum(1 for lab in standard if lab)
    labels = packed if packed_non_empty >= standard_non_empty else standard
    while len(labels) < n_signals:
        labels.append("")
    return labels[:n_signals]


def _extract_samples_per_record_packed(signal_headers: List[bytes]) -> List[int]:
    """掃描 header 77 到檔案最後一個 block，收集所有合法 SPR 值。"""
    spr: List[int] = []
    for si in range(NOX_SPR_HEADER_START, len(signal_headers)):
        sh = signal_headers[si]
        for off in range(0, 256, 8):
            val = _decode_ascii_int(sh[off : off + 8])
            if val is not None:
                spr.append(val)
    return spr


def _extract_samples_per_record_standard(signal_headers: List[bytes], n_signals: int) -> List[int]:
    spr: List[int] = []
    for si in range(min(n_signals, len(signal_headers))):
        val = _decode_ascii_int(signal_headers[si][128:136])
        spr.append(val if val is not None else 0)
    while len(spr) < n_signals:
        spr.append(0)
    return spr[:n_signals]


def _extract_samples_per_record(signal_headers: List[bytes], n_signals: int) -> List[int]:
    packed = _extract_samples_per_record_packed(signal_headers)
    if len(packed) >= n_signals and all(v > 0 for v in packed[:n_signals]):
        return packed[:n_signals]

    standard = _extract_samples_per_record_standard(signal_headers, n_signals)
    standard_valid = sum(1 for v in standard if v > 0)
    packed_valid = sum(1 for v in packed if v > 0)

    if len(packed) >= n_signals:
        chosen = packed[:n_signals]
    elif standard_valid >= packed_valid and standard_valid >= n_signals // 2:
        chosen = standard
    elif packed_valid > 0:
        chosen = packed
    else:
        chosen = standard

    if len(chosen) < n_signals:
        raise ValueError(
            f"Nox EDF samples-per-record 數量不足：找到 {len(chosen)}，預期 {n_signals}"
        )
    if any(v <= 0 for v in chosen[:n_signals]):
        bad = [i for i, v in enumerate(chosen[:n_signals]) if v <= 0]
        raise ValueError(
            f"Nox EDF samples-per-record 含無效值（索引 {bad[:8]}...）"
        )
    return chosen[:n_signals]


def parse_nox_edf(path: PathLike) -> NoxEdfHeader:
    """解析 Nox 專有 EDF header，回傳通道標籤與 record layout。"""
    p = Path(path)
    with open(p, "rb") as f:
        main = f.read(256)
        if len(main) < 256:
            raise ValueError(f"檔案過小，非有效 EDF：{p}")

    n_signals = int(main[252:256].decode("ascii", errors="ignore").strip() or 0)
    if n_signals <= 0:
        raise ValueError(f"無效的 n_signals：{p}")

    header_bytes = int(main[184:192].decode("ascii", errors="ignore").strip() or 256)
    n_records = int(main[236:244].decode("ascii", errors="ignore").strip() or 0)
    dur_record = float(main[244:252].decode("ascii", errors="ignore").strip() or 1.0)

    n_blocks = _signal_header_block_count(main, n_signals)
    with open(p, "rb") as f:
        f.read(256)
        signal_headers = [f.read(256) for _ in range(n_blocks)]
        if len(signal_headers) < n_signals:
            raise ValueError(
                f"signal header 區塊不足：{len(signal_headers)} blocks，預期至少 {n_signals}"
            )

    labels = _extract_labels(signal_headers, n_signals)
    spr = _extract_samples_per_record(signal_headers, n_signals)

    data_size = p.stat().st_size - header_bytes
    expected_records = data_size / (sum(spr) * 2) if spr else 0
    if n_records <= 0 or abs(expected_records - n_records) > 0.01:
        if spr and sum(spr) > 0:
            n_records = int(data_size // (sum(spr) * 2))

    return NoxEdfHeader(
        path=p.resolve(),
        version=main[0:8].decode("ascii", errors="replace").strip(),
        patient=main[8:88].decode("ascii", errors="replace").strip(),
        recording=main[88:168].decode("ascii", errors="replace").strip(),
        start_datetime=_parse_start_datetime(main),
        header_bytes=header_bytes,
        n_records=n_records,
        dur_record=dur_record,
        labels=labels,
        samples_per_record=spr,
    )


def read_nox_edf_channel(
    path: PathLike,
    header: NoxEdfHeader,
    channel_index: int,
    start_sample: int = 0,
    n_samples: Optional[int] = None,
) -> np.ndarray:
    """從 Nox EDF 讀取單一通道的 digital int16 樣本（跨 data records 拼接）。"""
    if channel_index < 0 or channel_index >= header.n_signals:
        raise IndexError(f"channel_index 超出範圍：{channel_index}")

    spr = header.samples_per_record[channel_index]
    if spr <= 0:
        return np.array([], dtype=np.int16)

    total = header.total_samples_for_channel(channel_index)
    start_sample = max(0, int(start_sample))
    if start_sample >= total:
        return np.array([], dtype=np.int16)

    if n_samples is None or n_samples <= 0:
        n_samples = total - start_sample
    else:
        n_samples = int(n_samples)
    end_sample = min(start_sample + n_samples, total)
    n_samples = end_sample - start_sample
    if n_samples <= 0:
        return np.array([], dtype=np.int16)

    ch_offset = sum(header.samples_per_record[:channel_index]) * 2
    record_bytes = header.record_bytes
    start_rec = start_sample // spr
    end_rec = (end_sample - 1) // spr

    chunks: List[np.ndarray] = []
    p = Path(path)
    with open(p, "rb") as f:
        for rec in range(start_rec, end_rec + 1):
            rec_base = header.header_bytes + rec * record_bytes + ch_offset
            in_rec_start = 0 if rec > start_rec else (start_sample % spr)
            in_rec_end = spr if rec < end_rec else ((end_sample - 1) % spr) + 1
            if in_rec_end <= in_rec_start:
                continue
            f.seek(rec_base + in_rec_start * 2)
            raw = f.read((in_rec_end - in_rec_start) * 2)
            if not raw:
                break
            chunks.append(np.frombuffer(raw, dtype="<i2"))

    if not chunks:
        return np.array([], dtype=np.int16)
    out = np.concatenate(chunks)
    return out[:n_samples]


def try_pyedflib_open(path: PathLike) -> Tuple[bool, str]:
    """嘗試用標準 pyedflib 開啟；若成功代表為標準 EDF 而非 Nox 專有格式。"""
    try:
        import pyedflib

        r = pyedflib.EdfReader(str(path))
        n = r.signals_in_file
        r.close()
        return True, f"standard EDF, n_signals={n}"
    except ImportError:
        return False, "pyedflib not installed"
    except Exception as e:
        return False, str(e)