#!/usr/bin/env python3
"""Headless 煙霧測試：通道名稱欄『保留來源後綴』省略正確性（D18 真資料）。

需求：名稱欄狹窄時，過長名稱（如 '1 impedance(NDF)'）**結尾 (EDF)/(NDF) 來源標籤不可被砍**。
- 錯誤：'1 …'（Qt 預設右省略，後綴全失）或 '1 …F)'（通用中間省略，EDF/NDF 變一樣分不出）。
- 正確：'1 imped…(NDF)' —— 只省略前面 base，結尾標籤完整可辨。

重要：本測試**不可用 QT_QPA_PLATFORM=offscreen**。offscreen 平台不附字型，QFontMetrics
會用錯誤的後備字型（過寬）導致省略結果與真實畫面不符。故此測試在原生平台跑、用
WA_DontShowOnScreen 不彈出視窗，取得與使用者螢幕一致的真實字型度量。

驗證方式：取名稱欄 delegate，以該列實際 cell 矩形呼叫 initStyleOption，
檢查最終 option.text（真正畫到畫面的字串）。
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

# 明確避免 offscreen（缺字型會使度量失真）；若外部已設，強制移除。
os.environ.pop("QT_QPA_PLATFORM", None)
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PyQt6.QtWidgets import QApplication, QStyleOptionViewItem  # noqa: E402
from PyQt6.QtGui import QFontMetrics  # noqa: E402
from PyQt6.QtCore import Qt  # noqa: E402
import psg_viewer  # noqa: E402

D18 = ROOT / "input" / "D18_17923057_PSG_20260511"


def pump(app, ms=2000):
    end = time.monotonic() + ms / 1000.0
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


def painted_text(table, row):
    """回傳第 row 列、名稱欄(col=1)真正畫到畫面的字串（走 delegate.initStyleOption）。"""
    deleg = table.itemDelegateForColumn(1)
    index = table.model().index(row, 1)
    opt = QStyleOptionViewItem()
    opt.rect = table.visualRect(index)      # 真實 cell 矩形（含目前欄寬）
    opt.widget = table
    opt.font = table.font()
    opt.fontMetrics = QFontMetrics(table.font())
    deleg.initStyleOption(opt, index)        # 內部自繪省略並寫回 opt.text
    return opt.text


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyle("Fusion")
    v = psg_viewer.PSGViewer()
    v.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    v.show()
    v._load_folder(str(D18), show_error=False)
    pump(app, 2500)

    assert v.current_rec is not None, "錄音未載入"
    t = v.channel_table
    deleg = t.itemDelegateForColumn(1)
    assert type(deleg).__name__ == "_SourceTagElideDelegate", \
        f"名稱欄 delegate 不對（實為 {type(deleg).__name__}）"
    print(f"[setup] delegate={type(deleg).__name__}  欄寬={t.columnWidth(1)}  列數={t.rowCount()}")

    checked = 0
    failures = []
    edf_seen = ndf_seen = None
    for row in range(t.rowCount()):
        it = t.item(row, 1)
        if not it:
            continue
        full = it.text()
        suffix = next((s for s in ("(EDF)", "(NDF)") if full.endswith(s)), None)
        if not suffix:
            continue
        shown = painted_text(t, row)
        checked += 1
        ok = shown.endswith(suffix)                     # 後綴必須完整保留
        if full == shown:                               # 完整顯示（夠寬）也算通過
            ok = True
        if not ok:
            failures.append((row + 1, full, shown))
        if row < 12:
            print(f"  row {row+1:>3} | {full!r:22} → {shown!r}  [{'OK' if ok else 'FAIL'}]")
        # 抓一組省略後的 EDF / NDF，確認兩者可區分
        if shown != full:
            if suffix == "(EDF)" and edf_seen is None:
                edf_seen = shown
            if suffix == "(NDF)" and ndf_seen is None:
                ndf_seen = shown

    print(f"\n[result] 檢查 {checked} 個帶來源後綴通道；失敗 {len(failures)}")
    assert checked >= 4, f"前提失敗：帶後綴通道應≥4，實際 {checked}"
    assert not failures, "後綴被砍的通道：\n" + \
        "\n".join(f"  第{r}列 {f!r} → {s!r}" for r, f, s in failures)

    # 決定性：省略後的 EDF 與 NDF 結尾不同 → 可辨識來源
    if edf_seen and ndf_seen:
        print(f"[result] 省略後可辨識來源：EDF範例={edf_seen!r}  NDF範例={ndf_seen!r}")
        assert edf_seen.endswith("(EDF)") and ndf_seen.endswith("(NDF)"), "省略後 EDF/NDF 無法區分"

    # 短名稱不可誤省略
    for row in range(t.rowCount()):
        it = t.item(row, 1)
        if it and it.text() == "1":
            assert painted_text(t, row) == "1", "短名稱 '1' 不應被省略"
            print("[result] 短名稱 '1' 未被省略 OK")
            break

    print("\n通道名稱保留後綴省略 煙霧測試 PASS")

    # 收尾：停止音訊 worker 執行緒，避免退出時 QThread 解構警告（exit code 9）
    try:
        th = getattr(v, "_audio_thread", None)
        if th is not None:
            th.quit()
            th.wait(2000)
        v.close()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
