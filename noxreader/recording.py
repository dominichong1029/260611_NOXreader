"""
noxreader.recording
核心讀取實作：使用 numpy.memmap 達成最佳化開檔與區段讀取。
支援 Nox PSG 儀器產生的 raw .ndf + SETUP.INI 為主的資料格式。
"""

from __future__ import annotations
import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union
import numpy as np
import sqlite3
import configparser
from datetime import datetime, timedelta

PathLike = Union[str, Path]


def _normalize(name: str) -> str:
    """將通道名稱正規化，用於比對檔名與 SETUP 描述。"""
    n = name.lower().strip()
    n = re.sub(r"[\s_\-]+", "", n)  # 移除空白、底線、連字號
    n = n.replace("impedance", "imp")
    n = n.replace("ambientlight", "light")
    n = n.replace("audio", "audio")
    return n


def find_raw_data_dir(patient_dir: PathLike) -> Optional[Path]:
    """在病患資料夾中找到包含 SETUP.INI 與大量 .ndf 的 raw data 子目錄。"""
    p = Path(patient_dir)
    candidates = []
    # 常見結構: patient/raw data_xxx/timestamp - hash/
    for sub in p.rglob("*"):
        if sub.is_dir():
            setup = sub / "SETUP.INI"
            if setup.exists():
                ndf_count = len(list(sub.glob("*.ndf")))
                if ndf_count > 5:
                    candidates.append((ndf_count, sub))
    if not candidates:
        return None
    # 選 ndf 最多的
    candidates.sort(reverse=True)
    return candidates[0][1]


def parse_setup_ini(setup_path: PathLike) -> Dict[str, Dict]:
    """
    解析 SETUP.INI。
    回傳： {internal_key: {'display': , 'desc':, 'fs': float, 'unit': str, 'raw_line': }}
    例如 EXG8 -> C3 / EEG-C3 / 200 / V
    """
    setup_path = Path(setup_path)
    meta: Dict[str, Dict] = {}
    if not setup_path.exists():
        return meta
    with open(setup_path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip()
            if not val:
                continue
            parts = [x.strip() for x in val.split(";")]
            display = parts[0] if parts else key
            desc = parts[1] if len(parts) > 1 else ""
            try:
                fs = float(parts[2]) if len(parts) > 2 and parts[2] else 0.0
            except ValueError:
                fs = 0.0
            unit = parts[3] if len(parts) > 3 else ""
            meta[key] = {
                "display": display,
                "desc": desc,
                "fs": fs,
                "unit": unit,
                "raw": val,
            }
    return meta


def build_channel_index(raw_dir: Path, setup_meta: Dict[str, Dict]) -> Dict[str, Dict]:
    """
    掃描 raw_dir 內所有 .ndf，建立可存取的 channel 索引。
    優先使用 SETUP 裡的 display 名稱對應，否則退回使用檔名 stem。
    每個 entry 包含: path, fs, unit, desc, samples, duration_sec, internal_key
    """
    index: Dict[str, Dict] = {}
    ndf_files = sorted(raw_dir.glob("*.ndf"))
    # 建立反向查詢：正規化後的 display 名稱 -> meta
    display_map: Dict[str, Dict] = {}
    for int_key, m in setup_meta.items():
        disp = m["display"]
        nk = _normalize(disp)
        display_map[nk] = {**m, "internal_key": int_key}
        # 也支援簡短如 c3, spo2（spo2 類通道現在優先由 ndf 內 header 提供正確 fs=3 全長）
        short = _normalize(disp.split()[0] if " " in disp else disp)
        if short and short not in display_map:
            display_map[short] = {**m, "internal_key": int_key}

    for ndf in ndf_files:
        stem = ndf.stem  # e.g. "c3", "abdomen rip", "ecg"
        nk = _normalize(stem)
        entry = {
            "path": ndf,
            "stem": stem,
            "fs": 0.0,
            "unit": "",
            "desc": stem,
            "internal_key": None,
            "samples": 0,
            "duration_sec": 0.0,
            "header_offset": 0,
            "inferred_fs": None,
        }

        # 優先從 .ndf 自身 embedded header 解析（最準確，尤其 Nonin oximeter 的 spo2 / spo2 b-b / pleth / pulse）
        # 可避免 SETUP partial match 錯誤（例如 "2"  substring 誤配進 "spo2" 拿到 200Hz）
        ndf_meta = _parse_ndf_channel_meta(ndf)
        if ndf_meta.get("fs") is not None:
            entry["fs"] = ndf_meta["fs"]
            if ndf_meta.get("unit"):
                entry["unit"] = ndf_meta["unit"]
            if ndf_meta.get("label"):
                entry["desc"] = ndf_meta["label"]
            if ndf_meta.get("data_start_hint"):
                entry["_data_start_hint"] = ndf_meta["data_start_hint"]
            if ndf_meta.get("scale") is not None:
                entry["scale"] = ndf_meta["scale"]
            if ndf_meta.get("type"):
                entry["type"] = ndf_meta["type"]

        # 嘗試從 setup 取得精確資訊（作為補充，特別是 display name 對應 EXGxx）
        if entry["fs"] <= 0 and nk in display_map:
            m = display_map[nk]
            entry.update(
                {
                    "fs": m["fs"],
                    "unit": m["unit"] or entry.get("unit", ""),
                    "desc": m.get("desc", stem) or entry.get("desc", stem),
                    "internal_key": m.get("internal_key"),
                }
            )
        elif entry["fs"] <= 0:
            # 嚴格化部分比對：排除極短 k（如 "2","1"）或純數字，避免 "2" in "spo2" 之類誤配
            for k, m in display_map.items():
                if (k in nk or nk in k) and len(k) >= 3 and not k.isdigit():
                    entry.update(
                        {
                            "fs": m["fs"],
                            "unit": m["unit"] or entry.get("unit", ""),
                            "desc": m.get("desc", stem) or entry.get("desc", stem),
                            "internal_key": m.get("internal_key"),
                        }
                    )
                    break

        # 從檔案大小推算 samples 與 duration（即使 fs 未知也先記錄）
        # 初始用 fsize//2；真正的扣 header 校正留給 _apply_header_offsets（那時 hint + 改善 detect 會決定最終 off）
        try:
            fsize = ndf.stat().st_size
            samples = fsize // 2
            entry["samples"] = samples
            if entry["fs"] > 0:
                entry["duration_sec"] = samples / entry["fs"]
        except Exception:
            pass

        # 以 stem 與正規化後名稱同時註冊，方便使用者用不同方式存取
        index[stem] = entry
        if nk and nk not in index:
            index[nk] = entry
        # 也用 desc 的一部分
        if entry["desc"] and entry["desc"] not in index:
            index[entry["desc"]] = entry

    return index


def compute_overall_duration(ch_index: Dict[str, Dict]) -> float:
    """取各通道推算 duration 的中位數作為 recording 總時長。"""
    durs = [e["duration_sec"] for e in ch_index.values() if e.get("duration_sec", 0) > 60]
    if not durs:
        return 0.0
    durs.sort()
    return durs[len(durs) // 2]


def _detect_header_offset(path: PathLike) -> int:
    """
    偵測 .ndf 檔案開頭的 XML junk header 偏移量（以 int16 samples 計）。
    掃描低 std + 有界值（避開開頭高值 junk 如 20302），適用 position 等低頻道。
    增強版：若原條件（高 std）在低變異通道（如 spo2 @3Hz，值穩定在 900 左右，std 小）抓不到，
    則退而尋找第一個「合理訊號區」（連續 32+ 筆 |v|<2000 的起始），確保 spo2 / pleth 等不被當短 segment。
    回傳第一個穩定/合理段的起始 offset，否則 0。
    """
    p = Path(path)
    try:
        raw = np.fromfile(p, dtype="<i2", count=5000).astype(float)
        # 策略1: 原低 std 條件（適合有變異的 EEG/position）
        for i in range(0, len(raw) - 200, 5):
            seg = raw[i : i + 200]
            stdv = float(np.std(seg))
            mx = float(np.max(np.abs(seg)))
            if 5 < stdv < 400 and mx < 2000:
                return i
        # 策略2: 低變異但合理範圍資料起點（oximeter spo2/pleth/pulse 關鍵；值多在 ±2000 內）
        for i in range(0, len(raw) - 64, 4):
            seg = raw[i : i + 64]
            if np.all(np.abs(seg) < 2000):
                # 再看後 64 筆也合理，確認已過渡到真實資料
                if i + 128 < len(raw) and np.all(np.abs(raw[i + 64 : i + 128]) < 2000):
                    return i
        return 0
    except Exception:
        return 0


def _parse_ndf_channel_meta(path: PathLike) -> Dict:
    """從 .ndf 檔頭的 embedded channel header (UTF-16 混 binary) 解析真實 fs / label / unit。
    這是 spo2 / pleth / pulse 等 oximeter 通道最可靠的來源（SETUP.INI 常缺或靠易錯的 partial match）。
    同時嘗試提供 data_start 提示以利之後校正 samples 與 header skip。
    回傳例: {'fs': 3.0, 'label': 'SpO2', 'unit': '%', 'scale': 0.1, 'data_start_hint': 1316}
    找不到或失敗回傳 {} 。
    """
    p = Path(path)
    res: Dict = {}
    try:
        b = p.read_bytes()[:8192]
        t = b.decode("utf-16-le", errors="ignore")
        m = re.search(r"<Channel>(.*?)</Channel>", t, re.S | re.I)
        if m:
            xml = m.group(1)
            for tag, key in [
                ("SamplingRate", "fs"),
                ("Unit", "unit"),
                ("Label", "label"),
                ("Scale", "scale"),
                ("Type", "type"),
            ]:
                mm = re.search(r"<" + tag + r">([^<]+)</" + tag + r">", xml, re.I)
                if mm:
                    val = mm.group(1).strip()
                    if key == "fs":
                        try:
                            res[key] = float(val)
                        except Exception:
                            pass
                    elif key in ("scale",):
                        try:
                            res[key] = float(val)
                        except Exception:
                            pass
                    else:
                        res[key] = val
        # 找 </Channel> 之後的資料起點提示（注意 header 內多為 UTF-16 wide chars）
        ch_end_pat = b"<\x00/\x00C\x00h\x00a\x00n\x00n\x00e\x00l\x00>\x00"
        ch_end = b.find(ch_end_pat)
        if ch_end > 0:
            # 跳過後續 ,timestampSq@: 及過渡 binary，保守 margin
            res["data_start_hint"] = ch_end + 200
        else:
            # 備援：直接找 timestamp 模式（UTF-16）
            ts_pat = b"2\x000\x002\x006"
            ts = b.find(ts_pat)
            if ts > 100:
                res["data_start_hint"] = ts + 300
    except Exception:
        pass
    return res


class NoxRecording:
    """
    單一病患錄音的最佳化讀取器。
    開啟成本極低（僅解析 meta + memmap 標頭），讀取指定時間區段時才真正載入資料。
    """

    def __init__(self, patient_dir: PathLike):
        self.patient_dir = Path(patient_dir).resolve()
        self.name = self.patient_dir.name
        self.raw_dir = find_raw_data_dir(self.patient_dir)
        if self.raw_dir is None:
            raise FileNotFoundError(
                f"找不到 raw data 目錄（含 SETUP.INI + .ndf）：{self.patient_dir}"
            )

        self.setup_meta = parse_setup_ini(self.raw_dir / "SETUP.INI")
        self.device_info = self._parse_device_ini()
        self.ch_index = build_channel_index(self.raw_dir, self.setup_meta)
        self.duration_sec = compute_overall_duration(self.ch_index)
        self.start_datetime = self._guess_start_time()

        # PR1: 套用 header_offset 偵測 + 低頻 fs 推斷（position 等 fs=0 問題 critical 修正）
        self._apply_header_offsets()
        self._infer_lowfreq_fs()
        # 刷新 duration_sec（Issue 2 修正）：infer 後部分 entry duration 更新，需重算 overall 以處理全低頻或 edge 情況
        self.duration_sec = compute_overall_duration(self.ch_index) or self.duration_sec

        # 快取已開啟的 memmap（key = 正規化名稱）
        self._mmap_cache: Dict[str, np.memmap] = {}

    def _parse_device_ini(self) -> Dict:
        """
        手動 section 解析 DEVICE.INI，只取具 key=val 的可靠區塊（如 [DeviceInfo]）。
        避開 [Channels] 純 list 格式（"Light 1Hz lx" 等無 =），否則 configparser 失敗導致 device_info={}。
        """
        ini_path = self.raw_dir / "DEVICE.INI"
        info: Dict = {}
        if not ini_path.exists():
            return info
        try:
            content = ini_path.read_text(encoding="utf-8", errors="ignore")
            lines = content.splitlines()
            i = 0
            while i < len(lines):
                line = lines[i].strip()
                if line.startswith("[") and line.endswith("]"):
                    sect = line[1:-1]
                    sect_dict = {}
                    j = i + 1
                    while j < len(lines):
                        l = lines[j].strip()
                        if l.startswith("[") and l.endswith("]"):
                            break
                        if "=" in l:
                            k, v = l.split("=", 1)
                            sect_dict[k.strip()] = v.strip()
                        j += 1
                    if sect_dict:  # 只保留有 key=val 的 section，跳過 [Channels] list
                        info[sect] = sect_dict
                    i = j
                    continue
                i += 1
        except Exception:
            pass
        return info

    def _guess_start_time(self) -> Optional[datetime]:
        # 從資料夾名稱或 raw 資料夾名稱猜（例如 20260511T220227）
        folder = self.raw_dir.name
        m = re.search(r"(\d{8})T(\d{6})", folder)
        if m:
            try:
                return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
            except Exception:
                pass
        # 嘗試從 edf 檔名
        for edf in self.patient_dir.glob("*.edf"):
            m = re.search(r"(\d{8})", edf.name)
            if m:
                try:
                    return datetime.strptime(m.group(1), "%Y%m%d")
                except Exception:
                    pass
        return None

    def _apply_header_offsets(self):
        """對 ch_index 內每個唯一 .ndf 套用 _detect_header_offset（所有 .ndf 均套用；D18/D20 實測 junk prefix 普遍存在於 position + spo2 + c3 等）。
        同時校正 samples（扣除 header 解讀 phantom），確保 spo2 等低頻通道的 duration 與 sample count 正確（避免被當成只有幾分鐘的短 segment）。
        """
        seen = set()
        for e in self.ch_index.values():
            pstr = str(e.get("path", ""))
            if pstr in seen:
                continue
            seen.add(pstr)
            try:
                off = _detect_header_offset(e["path"])
                hint = e.get("_data_start_hint")
                # 若 hint 提供的 byte offset 換算的 sample off 更大更準，採用之
                if hint:
                    hint_off = max(0, hint // 2)
                    if hint_off > off:
                        off = hint_off
                e["header_offset"] = off
                # 只校正 duration（使用 view samples - off），保留 samples 為 mmap view 長度（get_data 內部 slice 依賴它）
                # 這樣 get_data(duration=None) 會回傳 (view_len - off) 筆真實資料，時間軸正確。
                view_samp = e.get("samples", 0) or 0
                if off > 0 and view_samp > off and e.get("fs", 0) > 0:
                    e["duration_sec"] = (view_samp - off) / e["fs"]
            except Exception:
                e["header_offset"] = 0

    def _infer_lowfreq_fs(self):
        """
        低頻 fs inference（critical）：position-like (stem=="position" 或 fs==0 + 大 samples)
        從 overall duration 反推 fs，或對 position 使用 ~20Hz。
        存 "inferred_fs" 並更新 "fs"；get_data 內 offset 總以 sample 單位。
        """
        overall = self.duration_sec or 0.0
        for e in self.ch_index.values():
            stem = str(e.get("stem", "")).lower()
            fs = e.get("fs", 0.0) or 0.0
            samples = e.get("samples", 0) or 0
            is_pos_like = stem == "position" or (
                fs <= 0
                and samples > 10000
                and "axis" not in stem
                and "impedance" not in stem
                and "imp" not in stem
            )
            if is_pos_like:
                if overall > 60 and samples > 0:
                    inf = samples / overall
                    e["inferred_fs"] = round(inf, 4)
                    e["fs"] = e["inferred_fs"]
                    if e["fs"] > 0:
                        e["duration_sec"] = samples / e["fs"]
                elif stem == "position":
                    e["inferred_fs"] = 20.0
                    e["fs"] = 20.0
                    e["duration_sec"] = samples / 20.0 if samples > 0 else 0.0

    def is_position_channel(self, name: str) -> bool:
        """判斷是否 position 類別通道（僅 stem 或 name/desc 含 "position"；移除寬鬆低 fs 判斷以避免誤判 pulse / *impedance / set pressure 等）。"""
        info = self.get_channel_info(name)
        if not info:
            return False
        nm = _normalize(name)
        stem = str(info.get("stem", "")).lower()
        desc = str(info.get("desc", "")).lower()
        return (
            stem == "position"
            or "position" in nm
            or "position" in desc
        )

    def get_patient_info(self, mask_pii: bool = False) -> Dict:
        """
        使用 python-docx 從 word_*.docx 解析 demographics + clinical tables。
        特別提取 Age/Gender/BMI、BODY POSITION SUMMARY / RDI per posture。
        搜尋 patient_dir 優先 *word*.docx，fallback raw_dir。
        無 docx 時 fallback 顯示技術資訊。
        回傳: {"demographics": {...}, "clinical": {"rdi":.., "position_summary": [...] , ...}, "source": ...}
        """
        info = {"demographics": {}, "clinical": {}, "source": None}
        candidates = []
        for base in (self.patient_dir, self.raw_dir):
            if base and base.exists():
                for pat in ("*word*.docx", "*PSG*.docx", "*.docx"):
                    for f in base.glob(pat):
                        if f.is_file():
                            candidates.append(f)
        if not candidates:
            info["demographics"] = {"mrn": self.name, "note": "報告未找到，僅顯示技術資訊"}
            return info
        # 選最佳（含 word 或 match name）
        docx_path = None
        for c in candidates:
            if "word" in c.name.lower() or self.name[:6] in c.name:
                docx_path = c
                break
        if docx_path is None:
            docx_path = candidates[0]
        info["source"] = str(docx_path)
        try:
            from docx import Document
            doc = Document(str(docx_path))
            demo = {}
            full_text = "\n".join(p.text for p in doc.paragraphs)
            # 從 paras 提取（Age:50\t , Gender:Male , Height:175.0 cm , Weight:90.7kg , BMI:29.6 , MR# , Recording Date）
            m = re.search(r"Age:\s*(\d+)", full_text, re.IGNORECASE)
            if m:
                demo["age"] = int(m.group(1))
            m = re.search(r"Gender:\s*(Male|Female|男|女)", full_text, re.IGNORECASE)
            if m:
                demo["gender"] = m.group(1)
            m = re.search(r"Height:\s*([\d.]+)", full_text, re.IGNORECASE)
            if m:
                demo["height_cm"] = float(m.group(1))
            m = re.search(r"Weight:\s*([\d.]+)", full_text, re.IGNORECASE)
            if m:
                demo["weight_kg"] = float(m.group(1))
            m = re.search(r"BMI:\s*([\d.]+)", full_text, re.IGNORECASE)
            if m:
                demo["bmi"] = float(m.group(1))
            m = re.search(r"MR#:\s*([A-Za-z0-9\-]+)", full_text, re.IGNORECASE)
            if m:
                demo["mrn"] = m.group(1)
            m = re.search(r"Recording Date:\s*([\d/]+)", full_text, re.IGNORECASE)
            if m:
                demo["study_date"] = m.group(1)
            m = re.search(r"Date of Birth:\s*([\d/]+)", full_text, re.IGNORECASE)
            if m:
                demo["dob"] = m.group(1)
            # 強化從 split paras
            for p in doc.paragraphs:
                t = p.text
                if "\t" in t or ":" in t:
                    if "Age:" in t and "age" not in demo:
                        try:
                            val = t.split("Age:")[1].split()[0].split("Y")[0].strip()
                            demo["age"] = int(val)
                        except Exception:
                            pass
                    if "BMI:" in t and "bmi" not in demo:
                        try:
                            val = t.split("BMI:")[1].split()[0].strip()
                            demo["bmi"] = float(val)
                        except Exception:
                            pass
            if not demo.get("mrn"):
                demo["mrn"] = self.name
            if mask_pii:
                if demo.get("mrn"):
                    demo["mrn"] = str(demo["mrn"])[:5] + "***"
                if demo.get("dob"):
                    demo["dob"] = "****"
            info["demographics"] = demo

            # clinical + position summary (Table with Supine/Left etc + RDI)
            clin = {}
            pos_sum = []
            seen_pos = set()
            for table in doc.tables:
                rtxt = [[c.text.strip().replace("\n", " ") for c in r.cells] for r in table.rows]
                # 僅處理有 'min' 或 'Time in Position' 的 BODY POSITION SUMMARY 類表格，避免 RDI-only 表重複
                is_body_pos = any(
                    "Time in Position" in " ".join(row)
                    or ("min" in " ".join(row).lower() and "RDI" in " ".join(row))
                    for row in rtxt[:2]
                )
                if is_body_pos or any(
                    row[0] in ("Supine", "Left", "Right", "Pron", "Up")
                    and "min" in " ".join(row).lower()
                    for row in rtxt
                ):
                    # 最小 robustness 改進：由 header 動態找 time/RDI col（而非硬 index 1/3），避免未來表格順序變動
                    header = rtxt[0] if rtxt else []
                    tcol = next((i for i, h in enumerate(header) if "time" in h.lower() or "min" in h.lower()), 1)
                    rdicol = next((i for i, h in enumerate(header) if "rdi" in h.lower()), 3)
                    for row in rtxt:
                        pos = row[0]
                        if pos in ("Supine", "Left", "Right", "Pron", "Up") and pos not in seen_pos:
                            try:
                                tmin = float(row[tcol]) if tcol < len(row) and row[tcol] else 0.0
                                rdi_str = row[rdicol] if rdicol < len(row) and row[rdicol] else ""
                                rdi = float(rdi_str) if rdi_str else 0.0
                                pos_sum.append(
                                    {"position": pos, "time_min": tmin, "rdi_per_hr": rdi}
                                )
                                seen_pos.add(pos)
                            except Exception:
                                pass
                # RDI from tables (只取一次)
                for row in rtxt:
                    j = " ".join(row)
                    if ("RDI" in j or "AHI" in j) and re.search(r"(\d+\.?\d*)\s*/?hr", j, re.I):
                        m = re.search(r"(\d+\.?\d*)\s*/?hr", j, re.I)
                        if m and "rdi" not in clin:
                            try:
                                clin["rdi"] = float(m.group(1))
                            except Exception:
                                pass
            if pos_sum:
                clin["position_summary"] = pos_sum
            # extra from paras (Total Recording etc)
            for p in doc.paragraphs:
                t = p.text
                if "Total Recording Time" in t:
                    m = re.search(r"([\d.]+)hrs", t)
                    if m:
                        clin["duration_h"] = float(m.group(1))
                if "Sleep Efficiency" in t:
                    m = re.search(r"([\d.]+)%", t)
                    if m:
                        clin["sleep_efficiency_pct"] = float(m.group(1))
            info["clinical"] = clin
        except ImportError:
            info["demographics"]["note"] = "報告解析失敗: python-docx 未安裝或 import 失敗"
            if not info["demographics"].get("mrn"):
                info["demographics"]["mrn"] = self.name
        except Exception as ex:
            info["demographics"]["note"] = f"報告解析失敗: {ex}"
            if not info["demographics"].get("mrn"):
                info["demographics"]["mrn"] = self.name
        if not info.get("demographics"):
            info["demographics"] = {"mrn": self.name, "note": "報告未找到，僅顯示技術資訊"}
        return info

    @property
    def channels(self) -> List[Dict]:
        """回傳所有獨特通道的摘要列表（以 stem 為主）。
        注意：'samples' 與 'duration_sec' 基於原始檔案 bytes//2（可能含 header junk bytes）；'header_offset' 已暴露可用來計算 effective samples = max(0, samples - header_offset)。對 fidelity 無影響（off << total）。
        """
        seen = set()
        out = []
        for k, e in self.ch_index.items():
            stem = e["stem"]
            if stem in seen:
                continue
            seen.add(stem)
            out.append(
                {
                    "name": stem,
                    "fs": e["fs"],
                    "unit": e["unit"],
                    "desc": e["desc"],
                    "samples": e["samples"],
                    "duration_sec": e["duration_sec"],
                    "path": str(e["path"]),
                    "header_offset": e.get("header_offset", 0),
                    "inferred_fs": e.get("inferred_fs"),
                }
            )
        out.sort(key=lambda x: (-x["fs"], x["name"]))
        return out

    def list_channels(self, include_imp: bool = False) -> List[str]:
        """簡易列出人類可讀的通道名稱。"""
        names = []
        for c in self.channels:
            nm = c["name"]
            if not include_imp and "imp" in _normalize(nm):
                continue
            names.append(nm)
        return names

    def get_channel_info(self, name: str) -> Optional[Dict]:
        nk = _normalize(name)
        if nk in self.ch_index:
            return self.ch_index[nk].copy()
        # 模糊
        for k, e in self.ch_index.items():
            if _normalize(k) == nk or nk in _normalize(k):
                return e.copy()
        return None

    def _get_mmap(self, entry: Dict) -> np.memmap:
        key = str(entry["path"])
        if key in self._mmap_cache:
            return self._mmap_cache[key]
        # 假設 int16 little endian（Nox 儀器常見）
        mm = np.memmap(entry["path"], dtype="<i2", mode="r")
        self._mmap_cache[key] = mm
        return mm

    def get_data(
        self,
        name: str,
        start_sec: float = 0.0,
        duration: Optional[float] = None,
        as_float: bool = True,
        *,
        strip_header: bool = True,
    ) -> Tuple[np.ndarray, float]:
        """
        讀取單一通道指定時間區段的資料。
        回傳 (data_array, fs)

        - 使用 memmap 切片，記憶體效率極高。
        - start_sec: 從錄音開始算起的秒數（以秒為單位，內部轉 sample）。
        - duration: 要讀取的秒數，None 表示到結尾。
        - strip_header: 預設 True，自動移除 .ndf 開頭 XML junk header（所有 .ndf；position 等典型 offset 5-80 samples，D18 pos=5, c3/spo2~50~650 視 header 大小；現在有改善偵測 + ndf header 解析）。
          偏移量以 sample 為單位套用：effective_start = round(start_sec * fs) + header_offset。
          即使 fs fallback 仍正確（使用 inferred_fs 優先）。
          設 False 則維持舊 raw 行為（含 header），供對照或相容。
        - 向後相容：無關鍵字參數呼叫預設 strip=True。
        - 所有 .ndf 均自動 strip（D18/D20 實測開頭 junk prefix 普遍存在，position/c3/spo2 等 offset 通常 5-80 samples，spo2 類因 header 結構較大；改善偵測 + ndf 解析後時間軸正確）。
        - 低頻道（position）使用 inferred_fs 確保 30s 視窗 sample count 正確（~ fs*30），時間軸精準。
        - export_to_standard_edf 使用 stripped 計算 pmin/pmax 更正確，避免 junk 污染 position 等特別受益。
        """
        info = self.get_channel_info(name)
        if info is None:
            raise KeyError(f"找不到通道：{name}，可用：{self.list_channels()[:10]}...")

        fs = info.get("inferred_fs") or info["fs"]
        if fs <= 0:
            fs = 1.0
        samples = info["samples"]
        header_off = (info.get("header_offset", 0) or 0) if strip_header else 0

        start_sample = int(round(start_sec * fs)) + header_off
        if start_sample < 0:
            start_sample = 0
        if start_sample >= samples:
            return np.array([], dtype=np.float32 if as_float else np.int16), fs

        if duration is None or duration <= 0:
            end_sample = samples
        else:
            end_sample = start_sample + int(round(duration * fs))
            if end_sample > samples:
                end_sample = samples

        mm = self._get_mmap(info)
        raw = mm[start_sample:end_sample]
        if as_float:
            data = raw.astype(np.float32)
        else:
            data = raw
        return data, fs

    def get_events(self, include_xls: bool = True, custom_xls_path: Optional[str] = None) -> List[Dict]:
        """
        嘗試從 DeviceEvents.nef (SQLite) 取得事件。
        若無或解析失敗則回傳空 list。
        注意：此 DB 主要記錄裝置通知，完整臨床事件（AHI、階段等）通常在 event_*.xls / *.xlsx / docx 中。
        現在無 LIMIT，顯示所有資料（20個就20個，200個就200個）。
        include_xls=True 時會自動嘗試合併 event_*.xls / *.xlsx 裡的分析事件作為預設資料來源。
        若提供 custom_xls_path，則使用指定 XLS/XLSX 替換預設的 event 檔（用於 UI「替換XLS」功能）。
        效能優化：第一次呼叫後快取結果，避免重複解析大型 Excel 檔。
        無論來源是 .xls 或 .xlsx，解析成功後一律設定 _source='xls'，後續嚴格 exact 匹配。
        """
        cache_key = (include_xls, custom_xls_path)
        if hasattr(self, '_events_cache') and cache_key in getattr(self, '_events_cache', {}):
            return list(self._events_cache[cache_key])
        nef = self.raw_dir / "DeviceEvents.nef"
        if not nef.exists():
            return []
        try:
            conn = sqlite3.connect(str(nef))
            cur = conn.cursor()
            cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%marker%'"
            )
            markers = []
            for (tbl,) in cur.fetchall():
                cur.execute(f"SELECT * FROM [{tbl}] LIMIT 1")
                cols = [d[0] for d in cur.description]
                if 'starts_at' not in cols and 'starts' not in cols:
                    continue
                cur.execute(f"SELECT * FROM [{tbl}]")
                for row in cur.fetchall():
                    markers.append(dict(zip(cols, row)))
            conn.close()
            if include_xls:
                xls_evs = self._parse_xls_events(xls_path=custom_xls_path)
                markers.extend(xls_evs)
            if not hasattr(self, '_events_cache'):
                self._events_cache = {}
            self._events_cache[cache_key] = markers
            return list(markers)
        except Exception:
            return []

    def get_xls_events(self, xls_path: Optional[str] = None) -> List[Dict]:
        """公開 API：取得 event_*.xls / *.xlsx 裡的分析事件（Single Snore, LM, 體位等）。
        作為預設加入 events 資料，或由 UI 提供手動選擇匯入。
        若提供 xls_path，則從指定檔案解析（用於「替換XLS」功能）。
        解析成功後一律設定 _source='xls'（嚴格 exact 匹配）。
        """
        return self._parse_xls_events(xls_path=xls_path)

    def _parse_xls_events(self, xls_path: Optional[str] = None) -> list:
        """解析 event_*.xls / *.xlsx (來自分析軟體的詳細事件記錄，如 Single Snore, LM, 體位變化等)。
        同時支援舊版 .xls (xlrd) 與現代 .xlsx (openpyxl)。
        成功解析後一律設定 _source='xls'，後續在 viewer 的 _event_matches_channel 會走嚴格 exact match
        （只有 event_name.strip() 後完全符合 raw channel 名稱才算匹配，不使用任何 substring/alias 猜測）。非 xls 事件走 viewer 的點分隔部分精確匹配（或例外列表）。
        如果檔案損壞、格式錯誤或不存在，回傳空 list。
        回傳格式與 get_events 相容，並預先計算 rel_start / rel_end (相對於錄音開始)。
        若提供 xls_path，則優先使用指定檔案（用於替換 XLS/XLSX 功能）。
        """
        xls_candidates = []
        if xls_path:
            p = Path(xls_path)
            if p.exists():
                xls_candidates = [p]
        else:
            for base in (self.patient_dir, self.raw_dir):
                if base and base.exists():
                    for pat in ('*event*.xls', '*Event*.xls', '*EVENT*.xls',
                                '*event*.xlsx', '*Event*.xlsx', '*EVENT*.xlsx'):
                        xls_candidates.extend(list(base.glob(pat)))
        if not xls_candidates:
            return []
        xls_path = xls_candidates[0]
        try:
            rec_start = self.start_datetime
            if rec_start is None:
                rec_start = self._guess_start_time() or datetime(2026, 5, 12, 21, 44, 23)
            excel_epoch = datetime(1899, 12, 30)
            events = []

            ext = Path(xls_path).suffix.lower()
            if ext in ('.xlsx', '.xlsm'):
                # 現代 Excel 格式
                try:
                    import openpyxl
                except ImportError:
                    # 沒有 openpyxl 時直接放棄這個 candidate（不影響其他 .xls）
                    return []
                wb = openpyxl.load_workbook(str(xls_path), data_only=True)
                sheet = wb.active
                if sheet.max_row < 3:
                    wb.close()
                    return []
                # openpyxl: row 1 = header, row 2 = units, row 3+ = data
                for r_idx, row in enumerate(sheet.iter_rows(min_row=3, values_only=True), start=2):
                    try:
                        event_name = str(row[0] or '').strip()
                        if not event_name or event_name.lower().startswith('analysis'):
                            continue
                        dur = float(row[1] or 0)
                        start_val = row[2]
                        end_val = row[3]
                        # 彈性處理：可能是 datetime 物件，或 Excel serial number (float)
                        if isinstance(start_val, datetime):
                            start_dt = start_val
                        elif isinstance(start_val, (int, float)):
                            start_dt = excel_epoch + timedelta(days=float(start_val))
                        else:
                            continue
                        if isinstance(end_val, datetime):
                            end_dt = end_val
                        elif isinstance(end_val, (int, float)):
                            end_dt = excel_epoch + timedelta(days=float(end_val))
                        else:
                            end_dt = start_dt
                        rel_start = (start_dt - rec_start).total_seconds()
                        rel_end = None
                        # 直接用已解析的 datetime 判斷是否有有效結束時間（比對 dur）
                        if end_dt and (end_dt - start_dt).total_seconds() > 0.0005:
                            computed = (end_dt - rec_start).total_seconds()
                            if computed > rel_start + 0.0005:
                                rel_end = computed
                        if rel_start < 0:
                            rel_start = 0
                        location = event_name
                        ev = {
                            'id': f'xls_{r_idx}',
                            'starts_at': 0,
                            'ends_at': 0,
                            'type': event_name,
                            'location': location,
                            'notes': f'來自 XLS 分析 (dur={dur:.2f}s)',
                            'rel_start': rel_start,
                            'rel_end': rel_end,
                            '_source': 'xls'
                        }
                        events.append(ev)
                    except Exception:
                        continue
                try:
                    wb.close()
                except Exception:
                    pass
            else:
                # 舊版 .xls (Nox 分析軟體傳統輸出)
                import xlrd
                # 抑制 Nox 分析軟體產生的 event_*.xls 常見 OLE2 格式警告
                _devnull = open(os.devnull, "w")
                try:
                    wb = xlrd.open_workbook(str(xls_path), logfile=_devnull)
                finally:
                    try:
                        _devnull.close()
                    except Exception:
                        pass
                sheet = wb.sheet_by_index(0)
                if sheet.nrows < 3:
                    return []
                for r in range(2, sheet.nrows):
                    try:
                        event_name = str(sheet.cell(r, 0).value).strip()
                        if not event_name or event_name.lower().startswith('analysis'):
                            continue
                        dur = float(sheet.cell(r, 1).value or 0)
                        start_serial = float(sheet.cell(r, 2).value)
                        end_serial = float(sheet.cell(r, 3).value)
                        start_dt = excel_epoch + timedelta(days=start_serial)
                        end_dt = excel_epoch + timedelta(days=end_serial)
                        rel_start = (start_dt - rec_start).total_seconds()
                        rel_end = None
                        if end_serial and end_serial > start_serial:
                            computed = (end_dt - rec_start).total_seconds()
                            if computed > rel_start + 0.0005:
                                rel_end = computed
                        if rel_start < 0:
                            rel_start = 0
                        # 嚴格匹配 for xls：...（同前；非 xls 走新 dot-split exact）
                        location = event_name
                        ev = {
                            'id': f'xls_{r}',
                            'starts_at': 0,
                            'ends_at': 0,
                            'type': event_name,
                            'location': location,
                            'notes': f'來自 XLS 分析 (dur={dur:.2f}s)',
                            'rel_start': rel_start,
                            'rel_end': rel_end,
                            '_source': 'xls'
                        }
                        events.append(ev)
                    except Exception:
                        continue
            return events
        except Exception as e:
            # 檔案常損壞或缺少必要套件，靜默失敗（與原本行為一致）
            return []

    def iter_epochs(
        self,
        channels: Optional[List[str]] = None,
        epoch_sec: float = 30.0,
        step_sec: Optional[float] = None,
    ):
        """
        產生器：逐個 epoch  yield (epoch_idx, t_start, data_dict)
        data_dict: {ch_name: np.ndarray, ...}
        適合睡眠分期或特徵提取等批次處理，不會一次吃掉全部記憶體。
        """
        if step_sec is None:
            step_sec = epoch_sec
        if channels is None:
            channels = [c["name"] for c in self.channels if c["fs"] > 1][:8]  # 預設取較高頻且少量

        total = self.duration_sec
        t = 0.0
        idx = 0
        while t < total:
            data_dict = {}
            for ch in channels:
                try:
                    arr, _ = self.get_data(ch, start_sec=t, duration=epoch_sec)
                    data_dict[ch] = arr
                except Exception:
                    data_dict[ch] = np.array([])
            yield idx, t, data_dict
            t += step_sec
            idx += 1

    def __repr__(self):
        return f"<NoxRecording {self.name} duration={self.duration_sec/3600:.2f}h channels={len(self.ch_index)}>"


class NoxStudy:
    """
    管理多個病患資料夾（例如 input/ 下的兩個資料夾）。
    提供統一介面批次處理。
    """

    def __init__(self, root: PathLike = "input"):
        self.root = Path(root).resolve()
        self.recordings: Dict[str, NoxRecording] = {}
        self._discover()

    def _discover(self):
        # 尋找所有像 D18_... 或 D20_... 的病患資料夾
        for d in sorted(self.root.glob("*")):
            if not d.is_dir():
                continue
            if re.match(r"D\d+_", d.name) or "PSG" in d.name:
                try:
                    rec = NoxRecording(d)
                    key = d.name
                    self.recordings[key] = rec
                except Exception as e:
                    # 允許部分資料夾失敗
                    print(f"[NoxStudy] 跳過 {d.name}: {e}")

    @property
    def patients(self) -> List[str]:
        return sorted(self.recordings.keys())

    def get(self, key: str) -> NoxRecording:
        if key in self.recordings:
            return self.recordings[key]
        # 模糊比對
        for k, rec in self.recordings.items():
            if key in k or key == rec.name:
                return rec
        raise KeyError(f"找不到病患：{key}，可用：{self.patients}")

    def __len__(self):
        return len(self.recordings)

    def __repr__(self):
        return f"<NoxStudy patients={self.patients}>"


# 方便直接使用
def load_study(root: PathLike = "input") -> NoxStudy:
    return NoxStudy(root)


# ===================== 標準 EDF 匯出功能 =====================
def export_to_standard_edf(
    recording: NoxRecording,
    channel_names: List[str],
    out_path: PathLike,
    *,
    max_duration_sec: Optional[float] = None,
    physical_range: Optional[Tuple[float, float]] = None,
) -> Path:
    """
    將指定通道從 raw .ndf 匯出為**完全標準相容**的 EDF(+) 檔案。
    這是解決原廠 .edf 不合規（Physical Maximum 錯誤導致無法被 pyedflib / MNE 開啟）的核心修復方案。

    優點：
    - 產生檔案可直接被 EDFBrowser、MNE-Python、臨床軟體開啟。
    - 只讀取需要的通道 + 支援截斷時間，記憶體安全。
    - 保留原始取樣率與單位描述（來自 SETUP.INI）。

    注意：
    - 因為原始 .ndf 儲存的是儀器內部整數值，匯出時我們以觀測到的 min/max 當作 physical range。
    - 使用 stripped data 計算 pmin/pmax 更正確（避免 junk 污染）；所有 .ndf（含 c3/spo2 等，D18/D20 junk prefix 普遍）均受益（呼叫時預設 strip_header=True）。
    - 如需精確 uV / cmH2O 校正，請提供 physical_range 或後續在分析端 scale。
    """
    import pyedflib

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # 過濾有效通道
    valid = []
    signals_info = []
    for nm in channel_names:
        info = recording.get_channel_info(nm)
        if info and info.get("fs", 0) > 0:
            valid.append(nm)
            signals_info.append(info)

    if not valid:
        raise ValueError("沒有可匯出的有效通道")

    # 決定總時長
    dur = recording.duration_sec
    if max_duration_sec and max_duration_sec > 0:
        dur = min(dur, max_duration_sec)

    # 建立 writer（使用較大 buffer 避免頻繁 flush）
    n_channels = len(valid)
    # 計算 header 需要的 bytes（固定）
    writer = pyedflib.EdfWriter(
        str(out_path), n_channels=n_channels, file_type=pyedflib.FILETYPE_EDFPLUS
    )

    # 設定全域 header
    start_dt = recording.start_datetime or datetime(2026, 5, 11, 22, 2, 55)
    writer.setStartdatetime(start_dt)
    writer.setPatientCode(recording.name[:20])
    writer.setPatientName(recording.name)
    # pyedflib 對 equipment / transducer 有 ASCII + 無空白限制，需清理
    writer.setEquipment("NoxA1_raw_reconstructed")

    # 為每個通道設定 header 與之後才寫入資料
    channel_headers = []
    for i, (nm, info) in enumerate(zip(valid, signals_info)):
        fs = float(info["fs"])
        unit = info.get("unit") or "uV"
        # 先讀一次全通道或前一段來決定 physical min/max（避免一次吃完整個大檔）
        # 為了簡單與正確，我們讀取整個通道（但 memmap 仍很省記憶體）
        sig, _ = recording.get_data(nm, start_sec=0, duration=dur, as_float=True, strip_header=True)
        if len(sig) == 0:
            sig = np.zeros(1, dtype=np.float32)

        pmin = float(np.min(sig)) if physical_range is None else physical_range[0]
        pmax = float(np.max(sig)) if physical_range is None else physical_range[1]
        if pmin == pmax:
            pmax = pmin + 1.0

        ch_header = {
            "label": nm[:16],
            "dimension": unit[:8],
            "sample_frequency": fs,
            "physical_min": pmin,
            "physical_max": pmax,
            "digital_min": -32768,
            "digital_max": 32767,
            "prefilter": "",
            "transducer": (info.get("desc", "") or nm)[:80],
        }
        writer.setSignalHeader(i, ch_header)
        # 確保為 float64，pyedflib write 要求
        channel_headers.append((i, nm, sig.astype("float64"), fs))

    # 正確寫入方式：使用 writeSamples 一次給完整各通道 array list
    # 順序必須與 setSignalHeader 時的順序一致
    data_list = [sig for (_, _, sig, _) in channel_headers]
    writer.writeSamples(data_list)

    writer.close()
    return out_path
