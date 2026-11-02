@echo off
chcp 65001 >nul
cd /d "%~dp0"
python scripts\start_local.py --configure-wecom
pause
