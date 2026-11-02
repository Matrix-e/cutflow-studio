@echo off
chcp 65001 >nul
cd /d "%~dp0"
python scripts\stop_gpu.py
pause
