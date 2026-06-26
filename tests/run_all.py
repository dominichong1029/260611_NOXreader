#!/usr/bin/env python3
"""發佈前一次執行所有測試模組。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

TEST_FILES = [
    "test_device_ini.py",
    "test_nox_edf.py",
    "test_channel_preset.py",
    "test_header_strip.py",
    "test_verify_patients.py",
]


def main() -> int:
    print("=" * 72)
    print("PSG Viewer — 全套測試")
    print("=" * 72)

    failed: list[str] = []
    for name in TEST_FILES:
        path = ROOT / "tests" / name
        if not path.is_file():
            print(f"[SKIP] missing {name}")
            continue
        print(f"\n--- {name} ---")
        rc = subprocess.call([sys.executable, str(path)], cwd=str(ROOT))
        if rc != 0:
            failed.append(name)

    print("\n" + "=" * 72)
    if failed:
        print("FAIL:", ", ".join(failed))
        return 1
    print("全部測試 PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())