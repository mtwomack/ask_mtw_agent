"""Checks the Neon login in .env without starting adk web.
Run from the folder with your .env:  uv run python dbtest.py
"""
import asyncio
import os
from urllib.parse import unquote, urlsplit

import asyncpg
from dotenv import load_dotenv

load_dotenv(".env", override=True)
raw = os.environ.get("DATABASE_URL", "")
if not raw:
    raise SystemExit("DATABASE_URL not found in .env")

p = urlsplit(raw)
pw = unquote(p.password or "")
print("user:     ", p.username)
print("host:     ", p.hostname)
print("database: ", p.path.lstrip("/"))
print("password: ", (pw[:4] + "..." + pw[-2:]) if pw else "(empty)", "| length:", len(pw))
if "<" in pw or "NEW_PASSWORD" in pw:
    raise SystemExit("Password is still the placeholder from .env.example")


async def main():
    conn = await asyncpg.connect(
        user=p.username, password=pw, host=p.hostname, port=p.port or 5432,
        database=p.path.lstrip("/"), ssl="require", statement_cache_size=0,
    )
    print("CONNECTED as:", await conn.fetchval("select current_user"))
    await conn.close()


asyncio.run(main())