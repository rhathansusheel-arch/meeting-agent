@echo off
REM Double-click this file (or run it from Task Scheduler) to start the bot.
REM It activates the virtual environment and launches bot.py.
cd /d "%~dp0"
call ".venv\Scripts\activate.bat"
python bot.py
REM If the bot stops, keep the window open so you can read any message.
echo.
echo The bot has stopped. Press any key to close this window.
pause >nul
