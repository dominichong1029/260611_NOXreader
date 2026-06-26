#!/usr/bin/env python3
"""
build_exe.py
使用 PyInstaller 將 PSG Viewer 打包成獨立的 Windows 可執行檔 (.exe)

使用方式 (在 Windows PowerShell 或 CMD 執行，建議用乾淨 venv)：
  python build_exe.py

推薦輸出：
  - dist/PSGviewer/ 目錄 (one-folder，啟動較快、相依完整，強烈建議)

注意：
- 第一次完整打包可能需要 5~15 分鐘（視電腦而定）。
- 打包後的資料夾可直接複製到沒有 Python 的 Windows 電腦使用。
- PyQt6 + pyqtgraph + numpy 打包常見問題已盡量處理（collect-all + hidden imports）。
"""

import os
import sys
import shutil
from pathlib import Path

try:
    import PyInstaller.__main__ as pyi
except ImportError:
    print("錯誤：請先安裝 PyInstaller →  pip install pyinstaller")
    sys.exit(1)

ROOT = Path(__file__).parent.resolve()
DIST = ROOT / "dist"
BUILD = ROOT / "build"

def clean():
    for p in (DIST, BUILD):
        if p.exists():
            print(f"清理舊的建置目錄 {p} ...")
            shutil.rmtree(p, ignore_errors=True)

def build():
    print("=== 開始打包 PSG Viewer (PyQt6 + pyqtgraph + noxreader) ===")
    print("使用 one-folder 模式（推薦）。如需單檔請自行修改或用 --onefile。")

    # 強健的打包參數（針對 PyQt6 + pyqtgraph 常見問題）
    args = [
        "psg_viewer.py",
        "--name", "PSGviewer",
        "--windowed",           # 無 console 視窗（GUI 應用）
        "--clean",
        "--noconfirm",
        # 強烈建議 one-folder（比 onefile 穩定且啟動快）
        # "--onefile",

        # 收集 Qt6 平台插件與資源（解決 "could not find or load the Qt platform plugin"）
        "--collect-all", "PyQt6",
        "--collect-all", "PyQt6-Qt6",
        "--collect-all", "pyqtgraph",

        # 隱藏 imports（我們的後端 + 常見科學計算）
        "--hidden-import", "numpy",
        "--hidden-import", "numpy.core._multiarray_umath",
        "--hidden-import", "pyqtgraph",
        "--hidden-import", "PyQt6",
        "--hidden-import", "noxreader",
        "--hidden-import", "noxreader.recording",
        "--hidden-import", "noxreader.nox_edf",
        "--hidden-import", "viz.position_renderer",
        "--hidden-import", "docx",
        "--hidden-import", "openpyxl",
        "--hidden-import", "xlrd",
        "--hidden-import", "pyedflib",

        # 讓 PyInstaller 能找到本機模組
        "--paths", str(ROOT),

        # 加入整個 noxreader / viz 套件（以防萬一）
        "--add-data", f"{ROOT / 'noxreader'}{os.pathsep}noxreader",
        "--add-data", f"{ROOT / 'viz'}{os.pathsep}viz",

        # 關鍵：例外匹配規則檔（viewer 啟動時會從 __file__ 旁邊讀取）
        "--add-data", f"{ROOT / 'channel_matching.md'}{os.pathsep}.",
    ]

    # 自訂 icon（若根目錄有 icon.ico 會自動使用）
    icon = ROOT / "icon.ico"
    if icon.exists():
        args += ["--icon", str(icon)]
        print(f"使用圖示: {icon}")

    # 執行
    pyi.run(args)

    print("\n" + "="*60)
    print("【打包完成】")
    exe_dir = DIST / "PSGviewer"
    if exe_dir.exists():
        exe = exe_dir / "PSGviewer.exe"
        print(f"可執行檔目錄：{exe_dir}")
        print(f"主執行檔：{exe}")
        print("")
        print("★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★")
        print("重要提醒：")
        print("  • 請「只」執行 dist 資料夾裡的 PSGviewer.exe")
        print("  • build 資料夾是 PyInstaller 建置過程的暫存檔，絕對不要執行裡面的 .exe")
        print("    （執行 build 裡的 exe 會出現 Failed to load Python DLL 的錯誤，路徑會指向 build\\...\\_internal\\python312.dll）")
        print("  • 建議建置完成後直接刪除整個 build 資料夾")
        print("★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★")
        print("")
        print("部署方式：")
        print(f"  1. 將整個資料夾 '{exe_dir.name}' 複製到目標 Windows 電腦")
        print(f"  2. 直接雙擊 {exe.name}")
        print("  3. 點擊界面上的「開啟資料夾」選擇您的 PSG 資料夾（input/ 或單一病患資料夾）即可開始瀏覽")
        print("")
        print("提示：第一次在目標電腦執行可能較慢（Qt 資源初始化），之後會正常。")
    else:
        print("⚠️  請檢查 dist/ 目錄下是否有 PSGviewer 資料夾。")
        print("   如有錯誤訊息，請把完整 log 貼給我。")

    # 強制清理 build 資料夾，避免使用者誤執行中間產物導致 python dll 路徑錯誤
    if BUILD.exists():
        print(f"\n正在自動清理 build 暫存資料夾：{BUILD}")
        shutil.rmtree(BUILD, ignore_errors=True)
        print("build 資料夾已刪除。")

    # 在最終資料夾產生中文使用注意事項，避免使用者誤跑 build 裡的 exe
    if exe_dir.exists():
        note_path = exe_dir / "使用前必讀.txt"
        note_content = """PSG Viewer - 使用注意事項

【最重要】
• 請只執行這個資料夾裡的 「PSGviewer.exe」
• 不要執行專案根目錄下 build\\ 資料夾裡的任何 .exe
  （build 資料夾是 PyInstaller 建置時的暫存檔，執行它會出現：
   "Failed to load Python DLL '...\\build\\PSGviewer\\_internal\\python312.dll'" 的錯誤）

• 建議建置完後可以直接刪除整個 build 資料夾。

【正確啟動方式】
1. 直接雙擊 PSGviewer.exe
2. 程式啟動後點擊「開啟資料夾」，選擇您的 PSG 資料夾
   （例如整個 input\\ 資料夾，或單一 D18_... / D20_... 病患資料夾）

【第一次執行】
可能會稍慢（Qt 框架初始化），之後就會正常。

【分發給別人】
把整個 PSGviewer 資料夾壓縮成 zip 即可。
目標電腦不需要安裝 Python 或任何東西。

有任何問題請聯絡開發者。
"""
        note_path.write_text(note_content, encoding="utf-8")
        print(f"已在最終資料夾產生使用說明：{note_path}")

if __name__ == "__main__":
    clean()
    build()