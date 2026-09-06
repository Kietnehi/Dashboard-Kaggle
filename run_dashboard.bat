@echo off
title Kaggle Multi-Account Monitor Dashboard
cd /d "%~dp0"

echo =======================================================
echo          KAGGLE MULTI-ACCOUNT MONITOR HUB
echo =======================================================
echo.
echo [1/2] Dang khoi dong Web Server tren http://localhost:8000 ...
echo [2/2] Trinh duyet se tu dong mo trong giay lat...
echo.
echo Nhan Ctrl+C de dung server khi khong dung nua.
echo =======================================================
echo.

start "" "http://localhost:8000"
python -m uvicorn main:app --host 127.0.0.1 --port 8000
pause
