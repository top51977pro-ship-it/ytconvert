@echo off
cd /d "%~dp0"
python -c "import yt_dlp" 2>nul || python -m pip install -q "yt-dlp[default]"
start "" pythonw "%~dp0app.py"
