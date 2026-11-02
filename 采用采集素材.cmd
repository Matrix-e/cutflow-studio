@echo off
chcp 65001 >nul
cd /d "%~dp0"
python scripts\promote_collected.py
pause
