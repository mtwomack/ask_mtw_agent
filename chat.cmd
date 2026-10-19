@echo off
REM Starts the chat UI for ask_mtw_agent: http://localhost:8000/chat
REM Sessions + memory in Postgres (DATABASE_URL from .env), same as adkweb.
REM Usage: chat          -> chat UI, Postgres (Neon)
REM        chat dev      -> chat UI + adk web dev UI on the same server:
REM                         http://localhost:8000/chat and http://localhost:8000/dev-ui/
REM        chat mem      -> chat UI, in-memory sessions, nothing saved

cd /d %~dp0

set NO_PROXY=localhost,127.0.0.1
set no_proxy=localhost,127.0.0.1
set PORT=8000

if /i "%1"=="mem" (
    set SESSION_SERVICE_URI=devmem://
    set MEMORY_SERVICE_URI=none
)
if /i "%1"=="dev" set WITH_UI=1

REM open the browser once the server has had a few seconds to start
start "" /b cmd /c "timeout /t 5 /nobreak >nul & start http://localhost:8000/chat"
if /i "%1"=="dev" start "" /b cmd /c "timeout /t 6 /nobreak >nul & start http://localhost:8000/dev-ui/"
uv run python main.py
