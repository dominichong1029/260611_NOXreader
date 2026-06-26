#!/usr/bin/env python3
"""驗證 D01–D20 前後端相容：錄音載入、時長、get_events（前端事件列表來源）。"""

from __future__ import annotations

import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from noxreader import NoxStudy
from noxreader.recording import _edf_duration_sec, find_event_xls_paths

ROOT = Path(__file__).parent.parent / "input"


def _fmt_hms(sec: float) -> str:
    s = int(sec)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _discover_patient_dirs() -> list[Path]:
    if not ROOT.is_dir():
        return []
    dirs = [d for d in ROOT.iterdir() if d.is_dir() and re.match(r"D\d+_", d.name)]
    return sorted(dirs, key=lambda p: int(re.search(r"D(\d+)", p.name).group(1)))


def _simulate_frontend_event_channels(rec, events: list) -> dict[str, int]:
    """模擬 viewer _enrich + _compute_event_counts：每筆 event 必有 display_channel。"""
    raw_chans = [c["name"] for c in rec.channels]
    counts: dict[str, int] = defaultdict(int)
    for ev in events:
        loc = str(ev.get("location", "") or ev.get("type", "") or "").strip()
        matched = None
        loc_l = loc.lower()
        for ch in raw_chans:
            ch_l = ch.lower()
            if loc_l and (loc_l == ch_l or loc_l in ch_l or ch_l in loc_l):
                matched = ch
                break
        key = matched if matched else f"{loc or 'unknown'} (無匹配)"
        counts[key] += 1
    return dict(counts)


def verify_one(patient_dir: Path) -> dict:
    r = {
        "id": re.search(r"(D\d+)", patient_dir.name).group(1),
        "folder": patient_dir.name,
        "ok": True,
        "errors": [],
        "warnings": [],
        "details": {},
    }
    edfs = sorted(patient_dir.glob("*.edf"), key=lambda p: -p.stat().st_size)
    events_files = find_event_xls_paths(patient_dir, None)
    r["details"]["has_edf"] = bool(edfs)
    r["details"]["has_event_file"] = bool(events_files)

    if not edfs:
        r["ok"] = False
        r["errors"].append("缺少 EDF")
        return r

    try:
        study = NoxStudy(patient_dir)
        if len(study) != 1:
            r["warnings"].append(f"NoxStudy 病患數={len(study)}")
        rec = study.get(study.patients[0])
        r["details"]["rec_type"] = type(rec).__name__
        r["details"]["duration_sec"] = rec.duration_sec
        r["details"]["channels"] = len(rec.list_channels())

        if rec.duration_sec < 60:
            r["ok"] = False
            r["errors"].append(f"時長異常: {rec.duration_sec:.1f}s")

        evs = rec.get_events(include_xls=True) or []
        r["details"]["get_events"] = len(evs)
        if events_files and len(evs) < 1:
            r["ok"] = False
            r["errors"].append("有 event 檔但 get_events=0（前端會顯示本錄音無事件）")

        ch_counts = _simulate_frontend_event_channels(rec, evs)
        r["details"]["ui_channel_groups"] = len(ch_counts)
        r["details"]["ui_top_events"] = sorted(
            ch_counts.items(), key=lambda kv: -kv[1]
        )[:5]
        if evs and not ch_counts:
            r["ok"] = False
            r["errors"].append("事件有資料但 UI 通道群組為 0")

        ch = rec.list_channels()
        sample = next(
            (c for c in ("C3-M2", "C3", "flow", "Nasal Pressure") if c in ch),
            ch[0] if ch else None,
        )
        if sample:
            data, fs = rec.get_data(sample, start_sec=30, duration=0.5)
            r["details"]["waveform"] = f"{sample} len={len(data)} fs={fs}"
            if len(data) < 1:
                r["ok"] = False
                r["errors"].append(f"波形讀取失敗: {sample}")
        else:
            r["ok"] = False
            r["errors"].append("無通道可讀")

        edf_dur = _edf_duration_sec(edfs[0])
        r["details"]["edf_duration_sec"] = edf_dur
        if edf_dur > 3600 and rec.duration_sec < edf_dur * 0.5:
            r["warnings"].append(
                f"recording 時長 {rec.duration_sec:.0f}s 明顯短於 EDF {edf_dur:.0f}s"
            )
    except Exception as exc:
        r["ok"] = False
        r["errors"].append(str(exc))

    return r


def main() -> int:
    patients = _discover_patient_dirs()
    if not patients:
        print("[SKIP] input/ 無病患資料夾，略過 D01–D20 相容性驗證")
        return 0

    present_ids = {int(re.search(r"D(\d+)", p.name).group(1)) for p in patients}
    missing = [f"D{i:02d}" for i in range(1, 21) if i not in present_ids]

    print("=" * 72)
    print("D01–D20 前後端相容性驗證（input/ 內實際存在的病患）")
    print("=" * 72)
    if missing:
        print(f"缺少資料夾（略過）: {', '.join(missing)}")
        print()

    all_ok = True
    for p in patients:
        r = verify_one(p)
        d = r["details"]
        tag = "PASS" if r["ok"] else "FAIL"
        print(f"[{tag}] {r['id']}  {r['folder']}")
        print(
            f"       類型={d.get('rec_type')}  時長={d.get('duration_sec', 0):.0f}s "
            f"({_fmt_hms(d.get('duration_sec', 0))})"
        )
        print(
            f"       通道={d.get('channels')}  get_events={d.get('get_events')}  "
            f"UI群組={d.get('ui_channel_groups')}"
        )
        if d.get("ui_top_events"):
            tops = ", ".join(f"{k}:{v}" for k, v in d["ui_top_events"][:3])
            print(f"       事件Top3: {tops}")
        for w in r["warnings"]:
            print(f"       WARN: {w}")
        for e in r["errors"]:
            print(f"       ERR:  {e}")
        if not r["ok"]:
            all_ok = False
        print()

    print("=" * 72)
    print("總結果:", "全部 PASS" if all_ok else "有 FAIL")
    print("=" * 72)
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())