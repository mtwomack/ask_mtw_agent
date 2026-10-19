r"""
Sign-in and entitlement for the chat (personal project: runs locally and on Google Cloud Run).

People sign in with Google, Microsoft (personal Outlook/Hotmail or work/school) or GitHub.
It's the standard OAuth redirect flow, so it works in every browser - Windows, Mac, iPhone,
Android and the installed (PWA) app - and on http://localhost for local testing.
Each person is identified by their verified email address and only sees their own chats.

.env (locally) / environment variables + Secret Manager (Cloud Run)
-------------------------------------------------------------------
    AUTH_MODE=signin        off | signin
        off     no sign-in. Local dev only; services.py maps everyone to DEV_USER_ID.
        signin  the sign-in screen. Use this locally to test it, and always on Cloud Run.
        Default: off when ENV=local, otherwise signin (so Cloud Run is never left open).

    Turn on the providers you want (any left blank aren't shown):
    GOOGLE_CLIENT_ID=...         GOOGLE_CLIENT_SECRET=...
    MICROSOFT_CLIENT_ID=...      MICROSOFT_CLIENT_SECRET=...
    MICROSOFT_TENANT=common      common = personal + work/school accounts
    GITHUB_CLIENT_ID=...         GITHUB_CLIENT_SECRET=...

    Redirect URL to register with each provider:
        local:      http://localhost:8000/auth/callback/<google|microsoft|github>
        Cloud Run:  https://<your-service-url>/auth/callback/<google|microsoft|github>
    PUBLIC_URL=https://<your-service-url>    set on Cloud Run (locally it's worked out
                                             from the browser's address)

    SESSION_SECRET=<long random string>   signs the sign-in cookie - keep it in Secret Manager
                                          on Cloud Run. Not set = a new one at every start,
                                          so everyone signs in again (and Cloud Run instances
                                          wouldn't recognise each other's sign-ins).
    AUTH_SESSION_HOURS=12

Who may sign in (nobody until you list someone):
    ALLOWED_EMAILS=you@gmail.com,friend@outlook.com
    ALLOWED_DOMAINS=                      every verified account at these domains
    ADMIN_EMAILS=you@gmail.com            also get /docs and the adk dev UI (/dev-ui)
    ALLOWED_USERS_FILE=allowed_users.txt  optional, one email or @domain per line. Handy
                                          locally (re-read on change); on Cloud Run it's baked
                                          into the image, so prefer ALLOWED_EMAILS there.

A signed-in person reaches the chat page and only THEIR OWN sessions, documents and runs of
this app. Swagger, the dev UI, debug/eval endpoints and other people's data return 403
unless they're an admin.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from pathlib import Path
from urllib.parse import quote, unquote, urlencode

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

COOKIE = "chat_auth"
STATE_COOKIE = "chat_oauth"
HERE = Path(__file__).resolve().parent
TIMEOUT = 20


def _env(name: str) -> str:
    return os.getenv(name, "").strip().strip('"').strip("'")


def _csv(name: str) -> set[str]:
    return {x.strip().lower() for x in _env(name).split(",") if x.strip()}


ENV_LOCAL = _env("ENV").lower() == "local"
MODE = (_env("AUTH_MODE") or ("off" if ENV_LOCAL else "signin")).lower()
if MODE == "google":            # earlier name
    MODE = "signin"
if MODE not in ("off", "signin"):
    raise RuntimeError(f"AUTH_MODE must be off or signin - not {MODE!r}")

_secret_env = _env("SESSION_SECRET")
SECRET = (_secret_env or secrets.token_urlsafe(32)).encode()
SESSION_SECONDS = int(float(_env("AUTH_SESSION_HOURS") or "12") * 3600)
PUBLIC_URL = _env("PUBLIC_URL").rstrip("/")

ADMINS = _csv("ADMIN_EMAILS")
_ENV_EMAILS = _csv("ALLOWED_EMAILS")
_ENV_DOMAINS = {d.lstrip("@") for d in _csv("ALLOWED_DOMAINS")}
USERS_FILE = Path(_env("ALLOWED_USERS_FILE") or "allowed_users.txt")
if not USERS_FILE.is_absolute():
    USERS_FILE = HERE / USERS_FILE

APP_NAME = "ask_mtw_agent"      # set by install()


# ------------------------------------------------------------------ providers
def _providers() -> dict[str, dict]:
    found = {}
    if _env("GOOGLE_CLIENT_ID") and _env("GOOGLE_CLIENT_SECRET"):
        found["google"] = {
            "name": "Google", "id": _env("GOOGLE_CLIENT_ID"), "secret": _env("GOOGLE_CLIENT_SECRET"),
            "authorize": "https://accounts.google.com/o/oauth2/v2/auth",
            "token": "https://oauth2.googleapis.com/token",
            "userinfo": "https://openidconnect.googleapis.com/v1/userinfo",
            "scope": "openid email profile", "pkce": True,
            "extra": {"prompt": "select_account"},
        }
    if _env("MICROSOFT_CLIENT_ID") and _env("MICROSOFT_CLIENT_SECRET"):
        tenant = _env("MICROSOFT_TENANT") or "common"
        base = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0"
        found["microsoft"] = {
            "name": "Microsoft", "id": _env("MICROSOFT_CLIENT_ID"), "secret": _env("MICROSOFT_CLIENT_SECRET"),
            "authorize": f"{base}/authorize", "token": f"{base}/token",
            "userinfo": "https://graph.microsoft.com/v1.0/me?$select=displayName,givenName,surname,"
                        "userPrincipalName,jobTitle,officeLocation,preferredLanguage",
            "scope": "openid profile User.Read", "pkce": True,
            "extra": {"prompt": "select_account"},
        }
    if _env("GITHUB_CLIENT_ID") and _env("GITHUB_CLIENT_SECRET"):
        found["github"] = {
            "name": "GitHub", "id": _env("GITHUB_CLIENT_ID"), "secret": _env("GITHUB_CLIENT_SECRET"),
            "authorize": "https://github.com/login/oauth/authorize",
            "token": "https://github.com/login/oauth/access_token",
            "userinfo": "https://api.github.com/user",
            "scope": "read:user user:email", "pkce": False, "extra": {},
        }
    return found


PROVIDERS = _providers()
if MODE == "signin" and not PROVIDERS:
    raise RuntimeError("AUTH_MODE=signin needs at least one sign-in provider "
                       "(e.g. GOOGLE_CLIENT_ID + GOOGLE_CLIENT_SECRET) - or AUTH_MODE=off with ENV=local.")


def _clean(profile: dict) -> dict:
    """Drops empty fields and trims long values (the profile travels in the sign-in cookie)."""
    return {k: str(v)[:300] for k, v in profile.items() if v not in (None, "", [], {})}


async def _fetch_identity(key: str, p: dict, access_token: str) -> dict:
    """Profile of the signed-in account: always a verified "email" and "name", plus whatever
    else the provider shares (first/last name, picture, language, job title...).
    Raises if there's no verified email."""
    auth = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=TIMEOUT, headers=auth) as client:
        r = await client.get(p["userinfo"])
        r.raise_for_status()
        info = r.json()

        if key == "github":
            r = await client.get("https://api.github.com/user/emails")
            r.raise_for_status()
            emails = [e for e in r.json() if e.get("verified")]
            emails.sort(key=lambda e: not e.get("primary"))
            if not emails:
                raise ValueError("no verified email on this GitHub account")
            return _clean({
                "email": emails[0]["email"].lower(),
                "name": info.get("name") or info.get("login"),
                "username": info.get("login"),
                "picture": info.get("avatar_url"),
                "company": info.get("company"),
                "location": info.get("location"),
            })

        if key == "microsoft":
            # userPrincipalName, not "mail": a tenant admin can type any address into "mail",
            # but a UPN must be on a domain that tenant has proven it owns.
            upn = (info.get("userPrincipalName") or "").lower()
            if "@" not in upn or "#ext#" in upn:
                raise ValueError("this Microsoft account has no usable sign-in address")
            return _clean({
                "email": upn,
                "name": info.get("displayName"),
                "given_name": info.get("givenName"),
                "family_name": info.get("surname"),
                "job_title": info.get("jobTitle"),
                "office": info.get("officeLocation"),
                "language": info.get("preferredLanguage"),
            })

        # Google: standard OpenID claims; the email must be verified by the provider.
        if not info.get("email") or info.get("email_verified") not in (True, "true"):
            raise ValueError("the account's email address isn't verified")
        return _clean({
            "email": info["email"].lower(),
            "name": info.get("name"),
            "given_name": info.get("given_name"),
            "family_name": info.get("family_name"),
            "picture": info.get("picture"),
            "language": info.get("locale"),
        })


# ------------------------------------------------------------------ entitlement
_file_cache: tuple[float, set[str], set[str]] = (-1.0, set(), set())


def _file_lists() -> tuple[set[str], set[str]]:
    """(emails, domains) from ALLOWED_USERS_FILE, re-read only when the file changes."""
    global _file_cache
    try:
        mtime = USERS_FILE.stat().st_mtime
    except OSError:
        return set(), set()
    if mtime != _file_cache[0]:
        emails, domains = set(), set()
        for line in USERS_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
            entry = line.split("#", 1)[0].strip().lower()
            if entry.startswith("@"):
                domains.add(entry[1:])
            elif "@" in entry:
                emails.add(entry)
        _file_cache = (mtime, emails, domains)
    return _file_cache[1], _file_cache[2]


def is_admin(email: str) -> bool:
    return email.lower() in ADMINS


def is_entitled(email: str) -> bool:
    email = (email or "").lower()
    if "@" not in email:
        return False
    file_emails, file_domains = _file_lists()
    domain = email.rsplit("@", 1)[1]
    return (email in ADMINS or email in _ENV_EMAILS or email in file_emails
            or domain in _ENV_DOMAINS or domain in file_domains)


# ------------------------------------------------------------------ signed cookies
def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(payload: dict) -> str:
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    mac = _b64(hmac.new(SECRET, body.encode(), hashlib.sha256).digest())
    return f"{body}.{mac}"


def _unsign(token: str) -> dict | None:
    try:
        body, mac = token.split(".", 1)
        good = _b64(hmac.new(SECRET, body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(mac, good):
            return None
        payload = json.loads(_unb64(body))
        return payload if payload.get("exp", 0) > time.time() else None
    except Exception:
        return None


def _cookie(headers: dict[str, str], name: str) -> str:
    for part in headers.get("cookie", "").split(";"):
        key, _, value = part.strip().partition("=")
        if key == name:
            return value
    return ""


# ------------------------------------------------------------------ who is calling
async def _identity(headers: dict[str, str]) -> dict | None:
    """{"email", "name", "via", "profile"} of the signed-in person, or None."""
    if MODE == "signin":
        p = _unsign(_cookie(headers, COOKIE))
        if not p:
            return None
        profile = {**(p.get("profile") or {}), "email": p["email"], "signed_in_with": p.get("via", "")}
        return {"email": p["email"], "name": p.get("name") or "", "via": p.get("via", ""),
                "profile": _clean(profile)}
    return None


async def current_user(request: Request) -> dict | None:
    return await _identity({k.lower(): v for k, v in request.headers.items()})


# ------------------------------------------------------------------ request guard
PUBLIC = {"/chat", "/chat/config", "/auth/logout", "/favicon.ico", "/"}
_CHAT_PAGE = re.compile(r"/chat/[A-Za-z0-9_-]{8,64}/?")
_AUTH_FLOW = re.compile(r"/auth/(login|callback)/[a-z]+")


def _deny(status: int, detail: str):
    return JSONResponse({"detail": detail}, status_code=status)


class AuthMiddleware:
    """Every request except the chat page and sign-in must come from an entitled person,
    and may only touch that person's own data in this app."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if MODE == "off" or scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)

        path = scope.get("path", "")
        method = scope.get("method", "GET")
        if scope["type"] == "http" and (method == "OPTIONS" or path in PUBLIC
                                        or _CHAT_PAGE.fullmatch(path) or _AUTH_FLOW.fullmatch(path)):
            return await self.app(scope, receive, send)

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        who = await _identity(headers)

        async def reject(status, detail):
            if scope["type"] == "websocket":
                return await send({"type": "websocket.close", "code": 4401 if status == 401 else 4403})
            return await _deny(status, detail)(scope, receive, send)

        if not who:
            return await reject(401, "sign_in_required")
        email = who["email"]
        if not is_entitled(email):
            return await reject(403, "not_entitled")
        admin = is_admin(email)

        # Runs: the JSON body names the user and app - check them before the agent sees it,
        # and hand the agent the person's profile (session state "user:profile", read by whoami).
        if scope["type"] == "http" and path in ("/run", "/run_sse") and method == "POST":
            body, more = b"", True
            while more:
                message = await receive()
                if message["type"] != "http.request":
                    return
                body += message.get("body", b"")
                more = message.get("more_body", False)
            try:
                data = json.loads(body or b"{}")
                user = data.get("userId", data.get("user_id"))
                app_name = data.get("appName", data.get("app_name"))
            except Exception:
                return await reject(400, "bad_request")
            own = isinstance(user, str) and user.lower() == email and app_name in (None, APP_NAME)
            if not own and not admin:
                return await reject(403, "forbidden")
            if own:
                delta = data.pop("state_delta", None) or data.get("stateDelta") or {}
                data["stateDelta"] = {**delta, "user:profile": who["profile"]}
                body = json.dumps(data).encode()
                scope = {**scope, "headers": [(k, v) for k, v in scope.get("headers", [])
                                              if k.lower() != b"content-length"]
                         + [(b"content-length", str(len(body)).encode())]}

            sent = False

            async def replay():
                nonlocal sent
                if not sent:
                    sent = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return await receive()

            return await self.app(scope, replay, send)

        if admin:
            return await self.app(scope, receive, send)          # admins: everything else
        if scope["type"] == "websocket":
            return await reject(403, "forbidden")

        # Own sessions / documents: /apps/<app>/users/<me>/...
        parts = path.split("/")
        if len(parts) >= 5 and parts[1] == "apps" and parts[3] == "users":
            if unquote(parts[2]) == APP_NAME and unquote(parts[4]).lower() == email:
                return await self.app(scope, receive, send)
            return await reject(403, "forbidden")

        if path == "/list-apps" and method == "GET":
            return await self.app(scope, receive, send)

        return await reject(403, "forbidden")    # Swagger, dev UI, debug/eval endpoints, ...


# ------------------------------------------------------------------ sign-in routes
def _safe_next(target: str | None) -> str:
    """Only send people back to the chat itself after signing in (no open redirects)."""
    if target and (target == "/chat" or _CHAT_PAGE.fullmatch(target)):
        return target
    return "/chat"


def _callback_url(request: Request, key: str) -> str:
    base = PUBLIC_URL or str(request.base_url).rstrip("/")
    return f"{base}/auth/callback/{key}"


def _back_to_chat(error: str, email: str = "") -> RedirectResponse:
    query = {"auth_error": error, **({"email": email} if email else {})}
    res = RedirectResponse("/chat?" + urlencode(query), status_code=303)
    res.delete_cookie(STATE_COOKIE, path="/auth")
    return res


def install(app, app_name: str):
    """Adds the guard and the /auth routes to the FastAPI app made by get_fast_api_app."""
    global APP_NAME
    APP_NAME = app_name
    app.add_middleware(AuthMiddleware)

    @app.get("/auth/login/{key}", include_in_schema=False)
    async def login(key: str, request: Request, next: str = "/chat"):
        p = PROVIDERS.get(key)
        if MODE != "signin" or not p:
            raise HTTPException(status_code=404)
        state = secrets.token_urlsafe(24)
        verifier = secrets.token_urlsafe(48)
        params = {"client_id": p["id"], "redirect_uri": _callback_url(request, key),
                  "response_type": "code", "scope": p["scope"], "state": state, **p["extra"]}
        if p["pkce"]:
            challenge = _b64(hashlib.sha256(verifier.encode()).digest())
            params.update(code_challenge=challenge, code_challenge_method="S256")
        res = RedirectResponse(f"{p['authorize']}?{urlencode(params, quote_via=quote)}", status_code=303)
        flow = _sign({"state": state, "verifier": verifier, "key": key,
                      "next": _safe_next(next), "exp": int(time.time()) + 600})
        res.set_cookie(STATE_COOKIE, flow, max_age=600, httponly=True, samesite="lax",
                       secure=request.url.scheme == "https", path="/auth")
        return res

    @app.get("/auth/callback/{key}", include_in_schema=False)
    async def callback(key: str, request: Request, code: str = "", state: str = "", error: str = ""):
        p = PROVIDERS.get(key)
        if MODE != "signin" or not p:
            raise HTTPException(status_code=404)
        flow = _unsign(request.cookies.get(STATE_COOKIE, ""))
        if error:
            return _back_to_chat("cancelled" if error == "access_denied" else "failed")
        if not flow or flow.get("key") != key or not hmac.compare_digest(flow.get("state", ""), state) or not code:
            return _back_to_chat("expired")
        try:
            form = {"grant_type": "authorization_code", "code": code,
                    "redirect_uri": _callback_url(request, key),
                    "client_id": p["id"], "client_secret": p["secret"]}
            if p["pkce"]:
                form["code_verifier"] = flow["verifier"]
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                r = await client.post(p["token"], data=form, headers={"Accept": "application/json"})
            tokens = r.json()
            if r.status_code != 200 or "access_token" not in tokens:
                raise ValueError(tokens.get("error_description") or tokens.get("error") or f"HTTP {r.status_code}")
            profile = await _fetch_identity(key, p, tokens["access_token"])
            email, name = profile["email"], profile.get("name", "")
        except Exception as e:
            print(f"[auth] {key} sign-in failed: {type(e).__name__}: {e}")
            return _back_to_chat("failed")

        if not is_entitled(email):
            print(f"[auth] not entitled: {email} (via {key})")
            return _back_to_chat("not_entitled", email)
        token = _sign({"email": email, "name": name, "via": key, "profile": profile,
                       "exp": int(time.time()) + SESSION_SECONDS})
        res = RedirectResponse(flow["next"], status_code=303)
        res.set_cookie(COOKIE, token, max_age=SESSION_SECONDS, httponly=True, samesite="lax",
                       secure=request.url.scheme == "https", path="/")
        res.delete_cookie(STATE_COOKIE, path="/auth")
        print(f"[auth] signed in: {email} (via {key})")
        return res

    @app.post("/auth/logout", include_in_schema=False)
    async def sign_out():
        res = JSONResponse({"ok": True})
        res.delete_cookie(COOKIE, path="/")
        return res

    if MODE != "off":
        lists = len(ADMINS | _ENV_EMAILS) + len(_ENV_DOMAINS) + sum(map(len, _file_lists()))
        names = ", ".join(p["name"] for p in PROVIDERS.values()) or "-"
        print(f"[auth] mode={MODE}  providers: {names}  entitled entries={lists}  admins={len(ADMINS)}")
        if not lists:
            print("[auth] WARNING: nobody is entitled yet - set ALLOWED_EMAILS / ALLOWED_DOMAINS "
                  f"or add people to {USERS_FILE.name}")
        if MODE == "signin" and not _secret_env:
            print("[auth] note: SESSION_SECRET not set - everyone signs in again after a restart")
    else:
        print("[auth] mode=off - no sign-in (local dev)")


async def chat_config(request: Request, app_name: str, dev_user: str) -> dict:
    """What the chat page needs: app, user, and how to sign in."""
    base = {"app_name": app_name, "auth": {"mode": MODE}}
    if MODE == "off":
        return {**base, "signed_in": True, "user_id": "user", "display_user": dev_user}
    base["auth"].update(signout="/auth/logout",
                        providers=[{"id": k, "name": p["name"]} for k, p in PROVIDERS.items()])
    who = await current_user(request)
    if not who:
        return {**base, "signed_in": False}
    if not is_entitled(who["email"]):
        return {**base, "signed_in": False, "reason": "not_entitled", "email": who["email"]}
    return {**base, "signed_in": True, "user_id": who["email"], "email": who["email"],
            "display_user": who["name"] or who["email"], "via": who["via"],
            "picture": who["profile"].get("picture", ""),
            "admin": is_admin(who["email"])}
