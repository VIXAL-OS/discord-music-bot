@echo off
rem Manual launcher for the music event bot. Runs from this script's
rem directory so .env and data/ resolve. The bot writes its own UTF-8 log.
rem (The Task Scheduler job invokes music-event-bot directly instead of
rem this wrapper, so stopping the task cannot orphan the python process.)
cd /d "%~dp0"
music-event-bot --log-file "data\bot.log" bot
