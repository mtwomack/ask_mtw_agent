"""
Admin-only tools.

account_activity: a table of every account that has used this app - number of sessions
(conversations), number of chats (messages the person sent), and when they last chatted -
newest first.

Who counts as an admin: the emails in ADMIN_EMAILS (.env / Cloud Run). The check uses the
signed-in user ID that ADK gives the tool, never anything the model or browser says, so a
non-admin can't get the table by asking cleverly. With AUTH_MODE=off (local, no sign-in)
there is only you, so the tool is allowed.

Reads the same Postgres database as the chats (DATABASE_URL): the "public" schema locally,
"cloud" on Cloud Run. With in-memory sessions (chat mem) there is no database to read.
"""
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from google.adk.tools import ToolContext

MAX_ROWS = 200
DISPLAY_TZ = os.getenv("DISPLAY_TZ", "America/New_York")

_engine = None


def _auth_mode() -> str:
    local = os.getenv("ENV", "").strip().lower() == "local"
    return (os.getenv("AUTH_MODE") or ("off" if local else "signin")).strip().lower()


def _is_admin(user_id: str) -> bool:
    if _auth_mode() == "off":
        return True                      # local dev without sign-in: you're the only user
    admins = {e.strip().lower() for e in os.getenv("ADMIN_EMAILS", "").split(",") if e.strip()}
    return (user_id or "").strip().lower() in admins


def _get_engine():
    """One shared async engine on the chats database (same URL handling as services.py)."""
    global _engine
    if _engine is None:
        uri = os.getenv("SESSION_SERVICE_URI", "devpg://")
        if not (uri.startswith("devpg://") or uri.startswith("postgresql")):
            return None                   # in-memory sessions: nothing stored to report on
        import services                   # registers the URI schemes; has the Postgres helpers
        from sqlalchemy.ext.asyncio import create_async_engine
        url, engine_kwargs = services._pg(uri if uri.startswith("devpg://") else "devpg://" + uri.split("://", 1)[1])
        _engine = create_async_engine(url, **engine_kwargs)
    return _engine


QUERY = """
with user_msgs as (
    select user_id, count(*) as chats, max("timestamp") as last_chat
    from events
    where app_name = :app and event_data ->> 'author' = 'user'
    group by user_id
), convos as (
    select user_id, count(*) as sessions, max(update_time) as last_update
    from sessions
    where app_name = :app
    group by user_id
)
select c.user_id, c.sessions, coalesce(m.chats, 0) as chats, coalesce(m.last_chat, c.last_update) as last_chat
from convos c left join user_msgs m on m.user_id = c.user_id
order by coalesce(m.last_chat, c.last_update) desc nulls last
limit :limit
"""


def _fmt_time(value, tz: ZoneInfo) -> str:
    """Short enough for a phone: "Today 9:46 AM", "Yesterday 6:02 PM", "Oct 21, 9:46 AM",
    and the year only when it isn't this year ("Dec 30, 2025")."""
    if value is None:
        return "-"
    if value.tzinfo is None:              # stored as UTC without a zone
        value = value.replace(tzinfo=timezone.utc)
    local = value.astimezone(tz)
    today = datetime.now(tz).date()
    clock = local.strftime("%I:%M %p").lstrip("0")
    if local.date() == today:
        return f"Today {clock}"
    if local.date() == today - timedelta(days=1):
        return f"Yesterday {clock}"
    if local.year == today.year:
        return f"{local.strftime('%b')} {local.day}, {clock}"
    return f"{local.strftime('%b')} {local.day}, {local.year}"


async def account_activity(tool_context: ToolContext) -> dict:
    """ADMIN ONLY. Summarizes account activity for this app: for each account, the number of
    sessions (conversations), the number of chats (messages the person sent) and the date/time
    of their last chat, newest first. Call it only when an admin asks for account activity,
    usage or who has been using the app.
    """
    if not _is_admin(tool_context.user_id):
        return {"status": "error",
                "message": "Account activity is only available to admins."}

    engine = _get_engine()
    if engine is None:
        return {"status": "error",
                "message": "Account activity needs the Postgres session store - this server "
                           "is using in-memory sessions (chat mem), so there's nothing stored."}

    from sqlalchemy import text
    app = tool_context.session.app_name
    try:
        async with engine.connect() as conn:
            rows = (await conn.execute(text(QUERY), {"app": app, "limit": MAX_ROWS})).all()
    except Exception as e:                # noqa: BLE001 - reported to the admin
        return {"status": "error", "message": f"Couldn't read account activity ({type(e).__name__})."}

    tz = ZoneInfo(DISPLAY_TZ)
    zone = DISPLAY_TZ.split("/")[-1].replace("_", " ")     # e.g. "New York"
    lines = ["| Account | Sessions | Chats | Last chat |", "|---|---:|---:|---|"]
    for r in rows:
        # <wbr> lets a long email wrap after the "@" on narrow phone screens
        account = (r.user_id or "").replace("|", "\\|").replace("@", "@<wbr>")
        lines.append(f"| {account} | {r.sessions} | {r.chats} | {_fmt_time(r.last_chat, tz)} |")
    return {
        "status": "success",
        "accounts": len(rows),
        "time_zone": DISPLAY_TZ,
        "table_markdown": "\n".join(lines) if rows else "No accounts have used the app yet.",
        "note": (f"Sessions = conversations; Chats = messages the person sent; times in "
                 f"{zone} time." + (f" Showing the {MAX_ROWS} most recent." if len(rows) == MAX_ROWS else "")),
    }
