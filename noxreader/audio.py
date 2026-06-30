"""音頻通道支援：用 imageio_ffmpeg 內建的 ffmpeg 解碼主流音檔，供波形檢視器疊加顯示。

設計重點
--------
* **解碼**：一律走 ffmpeg（imageio_ffmpeg 自帶執行檔），支援 wav/mp3/ogg/m4a/aac/flac 等，
  離線可用、不新增 Python 相依。解碼成 16-bit PCM 臨時檔，再 memmap 讀取（與 NDF 同路徑、省記憶體）。
* **顯示 vs 播放分離**：顯示用降取樣（預設 8kHz）的 PCM；原始檔路徑保留在 ``path``，
  未來要做「捲動播放」可直接讀原檔全保真，不受顯示取樣率影響。
* **效能**：``read_envelope`` 分塊計算 min/max（peak）或 RMS 包絡，Fit 全長（數億點）時也不會
  把整段讀進記憶體，且 peak 模式保證不漏尖峰（醫療要求）。深度放大（點數少於門檻）時回傳原始樣本。

音頻沒有絕對時間：一律從全域 0 秒開始對齊，超過現有時間軸 max 的部分由呼叫端裁掉不顯示。
"""

from __future__ import annotations

import os
import re
import atexit
import tempfile
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np

try:
    import imageio_ffmpeg
except Exception:  # pragma: no cover - 部署缺套件時給明確錯誤
    imageio_ffmpeg = None


TARGET_FS_DEFAULT = 8000  # 顯示用降取樣率（打鼾/音量視覺化足夠；播放走原檔不受影響）
SUPPORTED_EXTS = (".wav", ".mp3", ".ogg", ".m4a", ".aac", ".flac", ".wma", ".opus")

# ffmpeg 聲道佈局字串 → 聲道數
_LAYOUT_NCH = {
    "mono": 1, "stereo": 2, "downmix": 2, "2.1": 3, "3.0": 3,
    "quad": 4, "4.0": 4, "4.1": 5, "5.0": 5, "5.1": 6, "5.1(side)": 6,
    "6.1": 7, "7.1": 8, "7.1(wide)": 8,
}

# 立體聲以上的聲道顯示標籤（與檔名一起組成 0_音頻_name_聲道）
_LABELS_BY_NCH = {
    1: [""],
    2: ["L", "R"],
}


def is_audio_file(path: os.PathLike | str) -> bool:
    return Path(path).suffix.lower() in SUPPORTED_EXTS


def _ffmpeg_exe() -> str:
    if imageio_ffmpeg is None:
        raise RuntimeError("缺少 imageio_ffmpeg，無法解碼音檔。請先安裝：pip install imageio-ffmpeg")
    return imageio_ffmpeg.get_ffmpeg_exe()


# 程式結束時清理所有臨時 PCM 檔
_TEMP_FILES: set[str] = set()


def _cleanup_temps() -> None:
    for p in list(_TEMP_FILES):
        try:
            os.remove(p)
        except Exception:
            pass
        _TEMP_FILES.discard(p)


atexit.register(_cleanup_temps)


class AudioSource:
    """一個音檔（解碼後）。一份來源可含多個聲道，每聲道對外是一個「通道」。

    典型用法::

        src = AudioSource("snore.mp3")
        src.probe()                 # 取得 src_fs / n_channels / duration
        src.decode(mode="split")    # 解碼成臨時 PCM 並 memmap
        y, fs_eff, dur, start = src.read_envelope(ch_index=0, start_sec=0, dur_sec=600)
    """

    def __init__(self, path: os.PathLike | str, target_fs: int = TARGET_FS_DEFAULT):
        self.path = str(path)
        self.target_fs = int(target_fs)
        self.src_fs: Optional[int] = None      # 原檔取樣率（僅資訊用）
        self.n_channels: int = 0               # 原檔聲道數
        self.duration_sec: float = 0.0         # 解碼後時長
        self.out_channels: int = 0             # 解碼輸出聲道數（split=原始；mono=1）
        self.channel_labels: List[str] = []    # 每個輸出聲道的顯示標籤
        self._tmp_path: Optional[str] = None
        self._mm: Optional[np.memmap] = None   # shape (frames, out_channels) int16

    # ------------------------------------------------------------------ probe
    def probe(self) -> None:
        """用 ffmpeg 解析原檔的取樣率、聲道數、時長（解析 stderr banner）。"""
        exe = _ffmpeg_exe()
        # 只給輸入、不指定輸出 → ffmpeg 印出串流資訊後以非零碼結束，stderr 含 banner
        proc = subprocess.run(
            [exe, "-hide_banner", "-i", self.path],
            capture_output=True, text=True, errors="replace",
        )
        err = proc.stderr or ""

        m_audio = re.search(r"Audio:\s*[^\n]*?,\s*(\d+)\s*Hz,\s*([^,\n]+)", err)
        if not m_audio:
            raise RuntimeError(f"ffmpeg 無法辨識音訊串流：{Path(self.path).name}")
        self.src_fs = int(m_audio.group(1))
        layout = m_audio.group(2).strip()
        self.n_channels = self._layout_to_nch(layout)

        m_dur = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", err)
        if m_dur:
            h, mnt, s = m_dur.groups()
            self.duration_sec = int(h) * 3600 + int(mnt) * 60 + float(s)

    @staticmethod
    def _layout_to_nch(layout: str) -> int:
        key = layout.strip().lower()
        if key in _LAYOUT_NCH:
            return _LAYOUT_NCH[key]
        m = re.search(r"(\d+)\s*channels?", key)
        if m:
            return int(m.group(1))
        return 1  # 保守退回單聲道

    # ----------------------------------------------------------------- decode
    def decode(self, mode: str = "split") -> None:
        """解碼成臨時 16-bit PCM 並 memmap。

        mode="split"：保留原檔所有聲道（各成一個通道）。
        mode="mono" ：混成單聲道（-ac 1）。
        """
        if self.n_channels == 0:
            self.probe()
        exe = _ffmpeg_exe()
        out_nch = 1 if mode == "mono" else max(1, self.n_channels)

        fd, tmp_path = tempfile.mkstemp(suffix=".pcm", prefix="psg_audio_")
        os.close(fd)
        _TEMP_FILES.add(tmp_path)

        cmd = [exe, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
               "-i", self.path, "-vn",
               "-ar", str(self.target_fs)]
        if mode == "mono":
            cmd += ["-ac", "1"]
        cmd += ["-f", "s16le", "-acodec", "pcm_s16le", tmp_path]

        proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
        if proc.returncode != 0 or not os.path.exists(tmp_path) or os.path.getsize(tmp_path) == 0:
            self._discard_temp(tmp_path)
            raise RuntimeError(f"ffmpeg 解碼失敗：{(proc.stderr or '').strip()[:300]}")

        mm = np.memmap(tmp_path, dtype=np.int16, mode="r")
        frames = len(mm) // out_nch
        if frames <= 0:
            self._discard_temp(tmp_path)
            raise RuntimeError("解碼結果為空。")
        mm = mm[: frames * out_nch].reshape(frames, out_nch)

        self._tmp_path = tmp_path
        self._mm = mm
        self.out_channels = out_nch
        self.duration_sec = frames / float(self.target_fs)
        self.channel_labels = _LABELS_BY_NCH.get(out_nch, [str(i + 1) for i in range(out_nch)])

    # ------------------------------------------------------------------- read
    def read_envelope(
        self,
        ch_index: int,
        start_sec: float,
        dur_sec: float,
        max_points: int = 8000,
        mode: str = "peak",
    ) -> Tuple[np.ndarray, float, float, float]:
        """讀取 [start_sec, start_sec+dur_sec) 區段，回傳 (y, fs_eff, got_dur, start_sec_used)。

        - 點數 <= max_points：回傳原始樣本（深度放大時看真實波形）。
        - 否則分塊計算包絡：mode="peak" → 每桶 min/max 交錯（不漏尖峰）；
          mode="rms" → 每桶 RMS（音量大小）。
        - **分桶邊界對齊固定 sample 格線**（起點 snap 到 block 整數倍）→ 捲動播放時同一段聲音
          永遠落在同一桶，波形只平移不隨幀重新分桶 → 消除「線條閃爍/抖動」。代價：起點可能略早於
          要求視窗(<1桶)，故回傳實際起點 start_sec_used 供呼叫端定位 x。
        - fs_eff = len(y)/got_dur，讓呼叫端用 linspace 還原正確 x 軸。
        - 區段落在資料範圍外時回傳空陣列。
        """
        if self._mm is None:
            return np.array([], dtype=np.float32), float(self.target_fs), 0.0, float(start_sec)
        fs = self.target_fs
        total = self._mm.shape[0]
        i0 = max(0, int(round(start_sec * fs)))
        i1 = min(total, int(round((start_sec + dur_sec) * fs)))
        if i1 <= i0:
            return np.array([], dtype=np.float32), float(fs), 0.0, float(start_sec)

        n = i1 - i0
        col = self._mm[:, ch_index]

        if n <= max_points:
            y = np.asarray(col[i0:i1], dtype=np.float32)
            gd = n / float(fs)
            return y, (len(y) / gd if gd > 0 else float(fs)), gd, i0 / float(fs)

        buckets = max(1, max_points if mode == "rms" else max_points // 2)
        block = int(np.ceil(n / buckets))
        # 對齊固定格線：起點 snap 到 block 的整數倍（依視窗長度 n 決定 block，捲動中 n 不變→格線穩定）
        a0 = (i0 // block) * block
        nb = int(np.ceil((i1 - a0) / block))

        if mode == "rms":
            out = np.empty(nb, dtype=np.float32)
            for b in range(nb):
                a = a0 + b * block
                z = min(total, a + block)
                seg = np.asarray(col[a:z], dtype=np.float32)
                out[b] = float(np.sqrt(np.mean(seg * seg))) if seg.size else 0.0
            y = out
        else:
            mins = np.empty(nb, dtype=np.float32)
            maxs = np.empty(nb, dtype=np.float32)
            for b in range(nb):
                a = a0 + b * block
                z = min(total, a + block)
                seg = np.asarray(col[a:z], dtype=np.float32)
                if seg.size:
                    mins[b] = seg.min()
                    maxs[b] = seg.max()
                else:
                    mins[b] = maxs[b] = 0.0
            y = np.empty(nb * 2, dtype=np.float32)
            y[0::2] = mins
            y[1::2] = maxs

        got_dur = (nb * block) / float(fs)   # y 涵蓋的時間跨度（自對齊起點起算）
        fs_eff = len(y) / got_dur if got_dur > 0 else float(fs)
        return y, fs_eff, got_dur, a0 / float(fs)

    # ---------------------------------------------------------------- cleanup
    def _discard_temp(self, path: Optional[str]) -> None:
        if not path:
            return
        try:
            os.remove(path)
        except Exception:
            pass
        _TEMP_FILES.discard(path)

    def close(self) -> None:
        """釋放 memmap 並刪除臨時 PCM 檔。"""
        self._mm = None
        self._discard_temp(self._tmp_path)
        self._tmp_path = None
