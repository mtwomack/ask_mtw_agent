@echo off
REM Starts adk web with Postgres sessions + long-term memory (DATABASE_URL from .env)
REM Usage: adkweb            -> Postgres (Neon)
REM        adkweb mem        -> in-memory only, nothing saved
REM        adkweb --log_level DEBUG

cd /d %~dp0

REM Keep local Ollama calls off the corporate proxy
set HTTP_PROXY=
set HTTPS_PROXY=
set http_proxy=
set https_proxy=
set NO_PROXY=localhost,127.0.0.1
set no_proxy=localhost,127.0.0.1

if /i "%1"=="mem" (
    uv run adk web --session_service_uri "devmem://"
) else (
    uv run adk web --session_service_uri "devpg://" --memory_service_uri "dbmem://" %*
)