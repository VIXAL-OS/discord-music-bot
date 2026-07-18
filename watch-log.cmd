@echo off
rem Live view of the bot's log — the console window, but on demand.
powershell -NoProfile -Command "Get-Content -Path '%~dp0data\bot.log' -Tail 50 -Wait"
