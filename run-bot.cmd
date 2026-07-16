@echo off
rem Launcher for the music event bot. Runs from this script's directory so
rem .env and data/ resolve, and appends all output to data\bot.log.
cd /d "%~dp0"
music-event-bot bot >> "data\bot.log" 2>&1
