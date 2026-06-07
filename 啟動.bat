@echo off
chcp 65001 >nul
title 影片搜尋下載工具
echo 正在啟動影片搜尋下載工具...
echo 啟動後瀏覽器會自動開啟，請勿關閉此視窗

cd /d D:\影片搜尋下載工具

REM 安裝依賴（首次執行）
C:\Users\USER\AppData\Local\Programs\Python\Python312\python.exe -m pip install fastapi uvicorn yt-dlp httpx --quiet

REM 啟動伺服器
C:\Users\USER\AppData\Local\Programs\Python\Python312\python.exe server.py
pause
