@echo off
REM Starts adk web with Postgres sessions + long-term memory (DATABASE_URL from .env)
REM Usage: adkweb            -> Postgres (Neon)
REM        adkweb mem        -> in-memory sessions, nothing saved
REM        adkweb --log_level DEBUG
REM
REM Artifacts (snapshot charts) use devmem:// - in memory, with the same dev-user override as
REM sessions (services.py). Memory: adk web's file storage puts the user ID in a folder name and
REM Windows can't create folders containing ':'. Same override: the dev UI asks for user "user".

cd /d %~dp0

REM Local calls (localhost A2A agents) skip the proxy; internet APIs still use it.
set NO_PROXY=localhost,127.0.0.1
set no_proxy=localhost,127.0.0.1

if /i "%1"=="mem" (
    uv run adk web --session_service_uri "devmem://" --artifact_service_uri "devmem://"
) else (
    uv run adk web --session_service_uri "devpg://" --memory_service_uri "dbmem://" --artifact_service_uri "devmem://" %*
)