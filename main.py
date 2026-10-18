"""
Serves the ADK API plus a chat page for ask_mtw_agent.

    http://localhost:8000/chat     the chat UI (chat.html)
    http://localhost:8000/docs     Swagger for the ADK API
    /dev-ui                        adk web's dev UI, only if WITH_UI=1

Local:      chat.cmd
Cloud Run:  same file (Dockerfile runs `python main.py`); behind IAP the chat uses the
            signed-in Google account as the user ID.

Project layout:
    main.py
    chat.html
    services.py          <- registers devpg:// and dbmem:// (imported below)
    <agent package>/     <- e.g. agents_mtw_agent/ (__init__.py, agent.py)
"""
import os
import re
from pathlib import Path

import uvicorn
from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse

import services  # noqa: F401  registers the devpg / dbmem URI schemes, loads .env
from google.adk.cli.fast_api import get_fast_api_app

HERE = Path(__file__).resolve().parent

# The agent package the chat talks to: AGENT_APP in .env, else the first folder here with agent.py.
APP_NAME = os.getenv("AGENT_APP") or next(
    (p.name for p in sorted(HERE.iterdir()) if (p / "agent.py").is_file()), "ask_mtw_agent")
print(f"[main.py] chat app -> {APP_NAME}")

app = get_fast_api_app(
    agents_dir=str(HERE),
    session_service_uri=os.getenv("SESSION_SERVICE_URI", "devpg://"),   # DATABASE_URL in .env / secret
    memory_service_uri=None if os.getenv("MEMORY_SERVICE_URI") == "none"
    else os.getenv("MEMORY_SERVICE_URI", "dbmem://"),
    # Uploaded documents (and any other artifacts). devmem:// = in memory, kept until restart;
    # on Cloud Run use gs://<bucket> so uploads survive restarts and multiple instances.
    artifact_service_uri=os.getenv("ARTIFACT_SERVICE_URI", "devmem://"),
    allow_origins=[o for o in os.getenv("ALLOW_ORIGINS", "").split(",") if o],
    web=os.getenv("WITH_UI", "0") == "1",
)


@app.get("/chat", include_in_schema=False)
async def chat_page():
    return FileResponse(HERE / "chat.html", headers={"Cache-Control": "no-cache"})


@app.get("/chat/config", include_in_schema=False)
async def chat_config(request: Request):
    """Which app and user the chat page should use.

    Behind IAP, Google adds the signed-in account to every request; use it so each person
    gets their own chats. Locally there's no header: the page uses "user", which
    services.py maps to DEV_USER_ID when ENV=local.
    """
    iap = request.headers.get("x-goog-authenticated-user-email", "")
    if iap:
        email = iap.split(":", 1)[-1]
        return {"app_name": APP_NAME, "user_id": email, "display_user": email}
    return {"app_name": APP_NAME, "user_id": "user",
            "display_user": services.DEV_USER_ID if services.OVERRIDE_ON else ""}


@app.get("/chat/{session_id}", include_in_schema=False)
async def chat_session_page(session_id: str):
    """/chat/<session id> opens the chat page on that conversation (the page loads it).
    Registered after /chat/config, so that route still wins."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", session_id):
        raise HTTPException(status_code=404)
    return FileResponse(HERE / "chat.html", headers={"Cache-Control": "no-cache"})


if os.getenv("WITH_UI", "0") != "1":
    @app.get("/", include_in_schema=False)
    async def root():
        return RedirectResponse("/chat")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
