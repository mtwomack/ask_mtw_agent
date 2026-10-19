r"""
Auto-loaded by `adk web` from the folder you run it in.

URI schemes:
    devmem://   in-memory sessions               (dev user override if ENV=local)
                in-memory artifacts (charts)     (dev user override if ENV=local)
    devpg://    Postgres sessions + state        (dev user override if ENV=local)
    dbmem://    Postgres long-term memory        (adk-database-memory)

Sessions and artifacts must use the SAME user override, otherwise the dev UI (which asks
for user "user") can't find artifacts the agent saved for DEV_USER_ID - a 404.

devpg:// and dbmem:// with nothing after them use DATABASE_URL from .env.
"""
import os
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from dotenv import load_dotenv

# ---------------------------------------------------------------
DEFAULT_USER_ID = "todd"   # used only if DEV_USER_ID is not set
# ---------------------------------------------------------------

# Find .env: next to this file, the folder adk web runs from, or any agent subfolder
_here = Path(__file__).resolve().parent
_candidates = [_here / ".env", Path.cwd() / ".env"]
_candidates += sorted(Path.cwd().glob("*/.env")) + sorted(_here.glob("*/.env"))

ENV_FILE = None
for _p in dict.fromkeys(p.resolve() for p in _candidates):
    if _p.is_file() and "DEV_USER_ID" in _p.read_text(encoding="utf-8", errors="ignore"):
        load_dotenv(_p, override=True)
        ENV_FILE = _p
        break

DEV_USER_ID = os.getenv("DEV_USER_ID", DEFAULT_USER_ID).strip().strip('"').strip("'")
# The override maps EVERY user to DEV_USER_ID - only for local dev without sign-in.
# With sign-in (AUTH_MODE=signin, see auth.py) each person keeps their own user ID (their email).
_ENV_LOCAL = os.getenv("ENV", "").strip().lower() == "local"
_AUTH_MODE = (os.getenv("AUTH_MODE") or ("off" if _ENV_LOCAL else "signin")).strip().lower()
OVERRIDE_ON = _ENV_LOCAL and _AUTH_MODE == "off"

from google.adk.artifacts import InMemoryArtifactService
from google.adk.cli.service_registry import get_service_registry
from google.adk.sessions import DatabaseSessionService, InMemorySessionService


# ------------------------------------------------------------------ user override
def _dev_user(user_id):
    return DEV_USER_ID if OVERRIDE_ON else user_id


class _DevUserMixin:
    """Rewrites user_id on every session call the UI / API makes."""

    @staticmethod
    def _swap(kwargs):
        if kwargs.get("user_id") is not None:
            kwargs["user_id"] = _dev_user(kwargs["user_id"])
        return kwargs

    async def create_session(self, **kwargs):
        return await super().create_session(**self._swap(kwargs))

    async def get_session(self, **kwargs):
        return await super().get_session(**self._swap(kwargs))

    async def list_sessions(self, **kwargs):
        return await super().list_sessions(**self._swap(kwargs))

    async def delete_session(self, **kwargs):
        return await super().delete_session(**self._swap(kwargs))


class DevUserDatabaseSessionService(_DevUserMixin, DatabaseSessionService):
    pass


class DevUserInMemorySessionService(_DevUserMixin, InMemorySessionService):
    pass


class DevUserInMemoryArtifactService(InMemoryArtifactService):
    """Artifacts (e.g. snapshot charts) with the same user override as sessions."""

    _swap = staticmethod(_DevUserMixin._swap)

    async def save_artifact(self, **kwargs):
        return await super().save_artifact(**self._swap(kwargs))

    async def load_artifact(self, **kwargs):
        return await super().load_artifact(**self._swap(kwargs))

    async def list_artifact_keys(self, **kwargs):
        return await super().list_artifact_keys(**self._swap(kwargs))

    async def delete_artifact(self, **kwargs):
        return await super().delete_artifact(**self._swap(kwargs))

    async def list_versions(self, **kwargs):
        return await super().list_versions(**self._swap(kwargs))

    async def list_artifact_versions(self, **kwargs):
        return await super().list_artifact_versions(**self._swap(kwargs))

    async def get_artifact_version(self, **kwargs):
        return await super().get_artifact_version(**self._swap(kwargs))


# ------------------------------------------------------------------ Postgres URL handling
def _pg(uri: str):
    """
    Turns devpg://, dbmem://, or a plain postgresql:// URL into an asyncpg URL.
    - empty URI (devpg://)      -> uses DATABASE_URL from .env
    - sslmode=require           -> ssl=require   (asyncpg doesn't accept sslmode)
    - channel_binding=...       -> dropped       (not an asyncpg option)
    - Neon/PgBouncer '-pooler'  -> disables prepared-statement caches
    Returns (url, engine_kwargs).
    """
    rest = uri.split("://", 1)[1] if "://" in uri else ""
    if not rest.strip("/"):
        rest = os.environ["DATABASE_URL"].split("://", 1)[1]

    parts = urlsplit("postgresql+asyncpg://" + rest)
    query = dict(parse_qsl(parts.query))
    if "sslmode" in query:
        query["ssl"] = query.pop("sslmode")
    query.pop("channel_binding", None)

    engine_kwargs = {"pool_pre_ping": True}
    if "pooler" in (parts.hostname or ""):
        query["prepared_statement_cache_size"] = "0"
        engine_kwargs["connect_args"] = {"statement_cache_size": 0}

    url = urlunsplit(parts._replace(query=urlencode(query)))
    return url, engine_kwargs


def _safe(url: str) -> str:
    p = urlsplit(url)
    return f"{p.scheme}://{p.username}:***@{p.hostname}{p.path}"


# ------------------------------------------------------------------ factories
def _devpg_factory(uri, **kwargs):
    url, engine_kwargs = _pg(uri)
    print(f"[services.py] sessions -> {_safe(url)}")
    return DevUserDatabaseSessionService(db_url=url, **engine_kwargs)


def _devmem_factory(uri, **kwargs):
    return DevUserInMemorySessionService()


def _devmem_artifacts_factory(uri, **kwargs):
    return DevUserInMemoryArtifactService()


def _dbmem_factory(uri, **kwargs):
    from adk_database_memory import DatabaseMemoryService
    url, engine_kwargs = _pg(uri)
    print(f"[services.py] memory   -> {_safe(url)}")
    return DatabaseMemoryService(url, **engine_kwargs)


registry = get_service_registry()
registry.register_session_service("devpg", _devpg_factory)
registry.register_session_service("devmem", _devmem_factory)
registry.register_artifact_service("devmem", _devmem_artifacts_factory)
registry.register_memory_service("dbmem", _dbmem_factory)

print(f"[services.py] .env used: {ENV_FILE or 'NONE FOUND'}")
print(f"[services.py] override {'ON' if OVERRIDE_ON else 'OFF'} -> user_id = {_dev_user('user')}")
