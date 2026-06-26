#!/usr/bin/env python3
"""驗證 EDF/NDF 重疊通道並列顯示（snore(EDF)/snore(NDF)）。
- 重疊通道兩個變體都出現、各自路由到正確來源、都有資料。
- 預設 preset 只勾 EDF 變體。
- 事件比對：Single Snore 同時對應兩個變體。
"""
from __future__ import annotations

import os
import sys
from collections import defaultdict
from pathlib import Path

os.environ.setdefault("PYTHONIOENCODING", "utf-8")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from noxreader.recording import NoxRecording  # noqa: E402
import psg_viewer  # noqa: E402

D18 = ROOT / "input" / "D18_17923057_PSG_20260511"


def main() -> int:
    rec = NoxRecording(D18)
    chans = rec.channels
    names = [c["name"] for c in chans]

    # 找出帶 (EDF)/(NDF) 後綴的重疊通道（base 以小寫分組，因 EDF 標籤可能大小寫不同）
    by_base = defaultdict(dict)  # base_lower -> {variant: actual_name}
    for c in chans:
        base, variant = psg_viewer.split_channel_variant(c["name"])
        if variant:
            by_base[base.lower()][variant] = c["name"]
    dup_bases = {b: v for b, v in by_base.items() if {"EDF", "NDF"} <= set(v)}
    print(f"通道總數={len(chans)}；重疊並列通道(base 同時有 EDF+NDF)={len(dup_bases)}")
    print("重疊 base 範例：", sorted(list(dup_bases))[:12])

    assert dup_bases, "未產生任何 EDF/NDF 並列通道（D18 應有重疊）"
    assert "snore" in dup_bases, f"snore 未並列；現有重疊 base={sorted(dup_bases)}"
    snore_variants = dup_bases["snore"]
    print(f"[OK] snore 並列：{snore_variants['EDF']} + {snore_variants['NDF']}")

    # 兩變體各自路由到正確來源 + 有資料
    for variant in ("EDF", "NDF"):
        nm = snore_variants[variant]
        info = rec.get_channel_info(nm)
        assert info is not None, f"get_channel_info({nm}) 失敗"
        src = info.get("source", "ndf")
        exp = "edf" if variant == "EDF" else "ndf"
        assert src == exp, f"{nm} source={src} 應為 {exp}"
        data, fs = rec.get_data(nm, start_sec=100.0, duration=10.0, as_float=True)
        print(f"[OK] {nm}: source={src} fs={fs:.2f} 取樣10s→{len(data)}點")
        assert len(data) > 0, f"{nm} 無資料"

    # 預設 preset：EDF 變體勾、NDF 變體不勾
    preset = psg_viewer.CLINICAL_DEFAULT_CHANNELS
    e_edf = psg_viewer.channel_matches_preset(snore_variants["EDF"], preset)
    e_ndf = psg_viewer.channel_matches_preset(snore_variants["NDF"], preset)
    print(f"[preset] {snore_variants['EDF']} 預設勾={e_edf}  {snore_variants['NDF']} 預設勾={e_ndf}")
    assert e_edf is True, "snore EDF 變體應預設勾選"
    assert e_ndf is False, "snore NDF 變體不應預設勾選"

    # 事件比對：Single Snore（location=snore）同時對應兩變體
    from PyQt6.QtWidgets import QApplication
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    _app = QApplication.instance() or QApplication(sys.argv)
    v = psg_viewer.PSGViewer()
    v.current_rec = rec
    ev = {"type": "Single Snore", "location": "snore", "_source": "xls",
          "rel_start": 100.0, "rel_end": 101.0}
    m_edf = v._event_matches_channel(ev, snore_variants["EDF"])
    m_ndf = v._event_matches_channel(ev, snore_variants["NDF"])
    print(f"[event] Single Snore 對應 {snore_variants['EDF']}={m_edf}  {snore_variants['NDF']}={m_ndf}")
    assert m_edf and m_ndf, "Single Snore 應同時對應 EDF 與 NDF 變體"

    # 非重疊通道不受影響（名稱無後綴）
    plain = [c["name"] for c in chans
             if psg_viewer.split_channel_variant(c["name"])[1] is None]
    print(f"[OK] 非重疊通道數={len(plain)}（名稱無後綴，事件/preset 行為不變）")

    # 真實事件：Single Snore 應仍透過例外匹配對上 snore（不應因後綴變成「無匹配」）
    v._events_loaded = False
    v.parsed_events = []
    v._parse_and_store_events()
    ss = [e for e in v.parsed_events if str(e.get("type", "")).lower() == "single snore"]
    matched = [e for e in ss if not e.get("is_no_match")]
    print(f"[event] 真實 Single Snore 總數={len(ss)} 已匹配={len(matched)} "
          f"無匹配={len(ss) - len(matched)}")
    assert ss, "找不到 Single Snore 事件"
    assert len(matched) == len(ss), "部分 Single Snore 變成無匹配（例外匹配回歸）"
    print(f"  display_channel 範例: {matched[0].get('display_channel')!r}")

    print("\n全部 PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
