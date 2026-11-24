@echo off
cd /d "%~dp0"
if not exist .venv (
    echo Creating virtual environment...
    py -m venv .venv || python -m venv .venv
    call .venv\Scripts\activate.bat
    pip install -r requirements.txt
) else (
    call .venv\Scripts\activate.bat
)
if not exist .env (
    copy .env.example .env >nul
    echo Created .env - fill in your Discord details (see README), save, then run this again.
    notepad .env
    pause
    exit /b
)
echo Dashboard: http://localhost:8080
python bot.py
pause
