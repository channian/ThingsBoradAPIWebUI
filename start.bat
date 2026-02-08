@echo off
chcp 65001 >nul
title Kepware 點位管理系統
echo ========================================
echo   Kepware ThingsBoard 點位管理系統
echo   啟動中...
echo ========================================
echo.

REM 檢查 Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [錯誤] 找不到 Python，請先安裝 Python 3.8 以上版本
    pause
    exit /b 1
)

REM 安裝相依套件 (首次執行)
if not exist ".venv" (
    echo [INFO] 首次啟動，建立虛擬環境...
    python -m venv .venv
    call .venv\Scripts\activate.bat
    pip install -r requirements.txt
) else (
    call .venv\Scripts\activate.bat
)

echo.
echo [INFO] 伺服器啟動於 http://localhost:9000
echo [INFO] 按 Ctrl+C 停止伺服器
echo.

python run.py

pause
