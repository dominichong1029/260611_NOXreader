@echo off
chcp 65001 >nul 2>&1
setlocal EnableExtensions
cd /d "%~dp0"

echo ================================================
echo   NoxPSG Viewer - One Click Launcher (start.bat)
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
    echo [WARN] requirements.txt had issues. Trying core packages explicitly...
    python -m pip install PyQt6 pyqtgraph numpy pyedflib python-docx openpyxl --disable-pip-version-check -q
)

:: ============================================
:: 3. 優先啟動已打包的獨立版本（最穩定）
:: ============================================
if exist "dist\NoxPSGViewer\NoxPSGViewer.exe" (
    echo [NOTE] Old packaged .exe found.
    echo For the latest fixes (scrollbar, fixed overview separation, no crash on open, etc.),
    echo we are FORCING run from source code instead of the stale exe.
    echo If you want to test the old exe, temporarily rename the dist folder.
    echo.
    timeout /t 1 >nul
)

:: ============================================
:: 4. 沒有打包檔 → 直接從原始碼啟動（已自動裝好套件）
:: ============================================
echo [INFO] No pre-built exe. Starting from source...
echo.

python -c "import noxpsg_viewer; print('[CHECK] Viewer module imports cleanly')" 2>&1
if errorlevel 1 (
    echo [WARN] Basic import check failed. Will still try full launch...
)

python noxpsg_viewer.py

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
    echo   python noxpsg_viewer.py
    echo.
    pause
)

endlocal
exit /b 0