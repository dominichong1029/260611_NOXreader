@echo off
chcp 65001 >nul 2>&1
setlocal EnableExtensions
cd /d "%~dp0"

echo ================================================
echo   PSG Viewer - One Click Launcher (start.bat)
echo ================================================
echo Current folder: %CD%
echo.

:: ============================================
:: 1. 檢查 Python 是否可用
:: ============================================
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found in PATH.
    echo.
    echo Please install Python 3.10 or newer (64-bit) from:
    echo   https://www.python.org/downloads/
    echo.
    echo During installation, make sure to CHECK the box:
    echo   "Add python.exe to PATH"
    echo.
    echo Then re-run this start.bat.
    echo.
    pause
    endlocal
    exit /b 1
)

echo [INFO] Python found.

:: ============================================
:: 2. 自動安裝相依套件（第一次會花時間，之後很快）
:: ============================================
echo [INFO] Ensuring required packages are installed...
python -m pip install -r requirements.txt --disable-pip-version-check -q
if errorlevel 1 (
    echo [WARN] requirements.txt had issues. Trying core packages explicitly (showing output)...
    python -m pip install PyQt6 pyqtgraph numpy pyedflib python-docx openpyxl xlrd==1.2.0 --disable-pip-version-check
)

:: ============================================
:: 3. 優先啟動已打包的獨立版本（發佈版）
:: ============================================
if exist "dist\PSGviewer\PSGviewer.exe" (
    echo [INFO] Launching packaged PSGviewer.exe ...
    start "" "dist\PSGviewer\PSGviewer.exe"
    endlocal
    exit /b 0
)

:: ============================================
:: 4. 沒有打包檔 → 直接從原始碼啟動（已自動裝好套件）
:: ============================================
echo [INFO] No pre-built exe. Starting from source...
echo.

echo [INFO] Python version:
python --version
echo [INFO] Core packages installed:
python -m pip list 2>&1 | findstr /I "PyQt6 pyqtgraph numpy pyedflib python-docx openpyxl"
echo [INFO] Current working dir: %CD%
echo.

set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1

python -c "import psg_viewer; print('[CHECK] Viewer module imports cleanly')" 2>&1
if errorlevel 1 (
    echo [WARN] Basic import check failed. Will still try full launch...
)

python -u psg_viewer.py

echo.
echo The viewer has exited (or crashed). Press any key to close this console...
pause >nul

set LAUNCH_ERR=%ERRORLEVEL%
if %LAUNCH_ERR% neq 0 (
    echo.
    echo [ERROR] The viewer failed to start (error code %LAUNCH_ERR%).
    echo.
    echo Possible causes:
    echo   - Some packages are still missing
    echo   - A runtime error in the current UI code (see Python traceback above for exact line)
    echo.
    echo You can try these manual commands for more details:
    echo   python -m pip install -r requirements.txt
    echo   python psg_viewer.py
    echo.
    pause
)

endlocal
exit /b 0
