@echo off
cd /d "%~dp0"
if not exist .env (
  copy .env.example .env >nul
  echo Created .env from .env.example. Add your BOT_TOKEN and a private DASHBOARD_SECRET, then run this file again.
  pause
  exit /b 1
)
findstr /C:"put_your_bot_token_here" .env >nul && (
  echo Edit .env first and replace the template BOT_TOKEN value.
  pause
  exit /b 1
)
findstr /C:"choose_a_long_unique_password" .env >nul && (
  echo Edit .env first and replace the template DASHBOARD_SECRET value.
  pause
  exit /b 1
)
python -m pip install -r requirements.txt
if errorlevel 1 pause & exit /b 1
python run.py
pause
