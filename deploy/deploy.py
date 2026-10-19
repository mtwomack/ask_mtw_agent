r"""
Deploys the chat to Google Cloud Run (personal project).

    uv run python deploy/deploy.py

Run it from the project root (where main.py, Dockerfile and .env are). Before the first run:
  - run deploy/cloud_schema.sql in Neon and put the new login's connection string in .env as
    DATABASE_URL_CLOUD (Cloud Run then uses the "cloud" schema; local keeps "public")
  - set PROJECT / REGION / SERVICE below

It:
  1. enables the Cloud APIs it needs (first time only)
  2. copies the secrets from your .env into Secret Manager (cloud database URL, Gemini key,
     sign-in secrets, Tavily / Finnhub / Twelve Data keys) - they never go into the image
  3. deploys with sign-in ON (AUTH_MODE=signin) and DB_SCHEMA=cloud: the URL is public, but
     only people in ALLOWED_EMAILS / ADMIN_EMAILS get past the sign-in screen, and the app
     refuses to start if the database login isn't the cloud-schema one
  4. sets PUBLIC_URL to the service's address and prints the redirect URLs to register
     with Google / Microsoft / GitHub

Re-run it after code changes. Changed a key in .env?  uv run python deploy/deploy.py --update-secrets
"""
import json
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

from dotenv import dotenv_values

# ------------------------------------------------------------------ edit these
PROJECT = "your-gcp-project-id"
REGION = "us-east1"
SERVICE = "ask-mtw-agent"
# ------------------------------------------------------------------

ENV = {k: (v or "").strip() for k, v in dotenv_values(".env").items()}
UPDATE_SECRETS = "--update-secrets" in sys.argv

# .env key -> Secret Manager name (DATABASE_URL_CLOUD becomes DATABASE_URL on Cloud Run).
# Only the ones that have a value in .env are used.
SECRETS = {
    "DATABASE_URL_CLOUD": "database-url-cloud",
    "GOOGLE_API_KEY": "google-api-key",
    "SESSION_SECRET": "session-secret",
    "GOOGLE_CLIENT_SECRET": "google-client-secret",
    "MICROSOFT_CLIENT_SECRET": "microsoft-client-secret",
    "GITHUB_CLIENT_SECRET": "github-client-secret",
    "TAVILY_API_KEY": "tavily-api-key",
    "FINNHUB_API_KEY": "finnhub-api-key",
    "TWELVEDATA_API_KEY": "twelvedata-api-key",
}
# Plain settings copied from .env when set (not secret).
PASS_THROUGH = ["GOOGLE_CLIENT_ID", "MICROSOFT_CLIENT_ID", "MICROSOFT_TENANT", "GITHUB_CLIENT_ID",
                "ALLOWED_EMAILS", "ALLOWED_DOMAINS", "ADMIN_EMAILS", "AUTH_SESSION_HOURS",
                "AGENT_MODEL", "SEARCH_MODEL", "OCR_MODEL"]
# On Cloud Run the env var is called DATABASE_URL (services.py reads that).
RENAME = {"DATABASE_URL_CLOUD": "DATABASE_URL"}


GCLOUD = shutil.which("gcloud") or "gcloud"     # gcloud.cmd on Windows


def run(*args, stdin=None) -> subprocess.CompletedProcess:
    return subprocess.run([GCLOUD, *args, f"--project={PROJECT}"], text=True, input=stdin,
                          capture_output=True)


def gcloud(*args, stdin=None, quiet=False) -> str:
    """Runs gcloud; stops with its error message if it fails. Returns its output."""
    if not quiet:
        r = subprocess.run([GCLOUD, *args, f"--project={PROJECT}"], text=True, input=stdin,
                           stdout=subprocess.PIPE if stdin is not None else None)
    else:
        r = run(*args, stdin=stdin)
    if r.returncode:
        sys.exit(f"\ngcloud {' '.join(args[:3])} ... failed{': ' + r.stderr if quiet and r.stderr else ''}")
    return (r.stdout or "").strip()


def value_of(*args) -> str:
    """Output of a gcloud query, or "" if it fails (e.g. the service doesn't exist yet)."""
    r = run(*args)
    return r.stdout.strip() if r.returncode == 0 else ""


def main():
    if PROJECT.startswith("your-"):
        sys.exit("Edit PROJECT / REGION / SERVICE at the top of deploy.py first.")
    missing = [k for k in ("DATABASE_URL_CLOUD", "GOOGLE_API_KEY", "GOOGLE_CLIENT_ID",
                           "GOOGLE_CLIENT_SECRET") if not ENV.get(k)]
    if ENV.get("DATABASE_URL_CLOUD") and ENV.get("DATABASE_URL_CLOUD") == ENV.get("DATABASE_URL"):
        sys.exit("DATABASE_URL_CLOUD must be the ponder_cloud login from deploy/cloud_schema.sql, "
                 "not your local DATABASE_URL.")
    if not (ENV.get("ALLOWED_EMAILS") or ENV.get("ADMIN_EMAILS") or ENV.get("ALLOWED_DOMAINS")):
        missing.append("ALLOWED_EMAILS or ADMIN_EMAILS")
    if missing:
        sys.exit("Add these to .env first: " + ", ".join(missing))

    print("1/4 Enabling APIs ...")
    gcloud("services", "enable", "run.googleapis.com", "cloudbuild.googleapis.com",
           "artifactregistry.googleapis.com", "secretmanager.googleapis.com")
    number = gcloud("projects", "describe", PROJECT, "--format=value(projectNumber)", quiet=True)
    sa = f"{number}-compute@developer.gserviceaccount.com"

    print("2/4 Secrets ...")
    if not ENV.get("SESSION_SECRET"):
        ENV["SESSION_SECRET"] = secrets.token_urlsafe(32)   # only used if the secret doesn't exist yet
    mounted = []
    for key, name in SECRETS.items():
        value = ENV.get(key)
        if not value:
            continue
        if run("secrets", "describe", name).returncode != 0:
            gcloud("secrets", "create", name, "--data-file=-", stdin=value, quiet=True)
            print(f"   created {name}")
        elif UPDATE_SECRETS and key != "SESSION_SECRET":
            gcloud("secrets", "versions", "add", name, "--data-file=-", stdin=value, quiet=True)
            print(f"   updated {name}")
        gcloud("secrets", "add-iam-policy-binding", name, f"--member=serviceAccount:{sa}",
               "--role=roles/secretmanager.secretAccessor", quiet=True)
        mounted.append(f"{RENAME.get(key, key)}={name}:latest")

    print("3/4 Deploying (a few minutes) ...")
    url = value_of("run", "services", "describe", SERVICE, f"--region={REGION}",
                   "--format=value(status.url)")
    env = {"ENV": "prod", "AUTH_MODE": "signin", "WITH_UI": "0", "DB_SCHEMA": "cloud",
           "GOOGLE_GENAI_USE_VERTEXAI": "0",            # Gemini API key, same as local
           **{k: ENV[k] for k in PASS_THROUGH if ENV.get(k)}}
    if url:
        env["PUBLIC_URL"] = url
    env_file = Path(".cloudrun-env.yaml")
    # JSON strings are valid YAML: handles commas and quotes in values like ALLOWED_EMAILS
    env_file.write_text("".join(f"{k}: {json.dumps(v)}\n" for k, v in env.items()), encoding="utf-8")
    try:
        gcloud("run", "deploy", SERVICE, "--source", ".", f"--region={REGION}",
               f"--service-account={sa}", "--allow-unauthenticated", "--memory=1Gi",
               # one instance: uploaded documents are kept in memory (lost on restart)
               "--max-instances=1",
               f"--env-vars-file={env_file}", "--set-secrets=" + ",".join(mounted))
    finally:
        env_file.unlink(missing_ok=True)

    print("4/4 Address ...")
    new_url = gcloud("run", "services", "describe", SERVICE, f"--region={REGION}",
                     "--format=value(status.url)", quiet=True)
    if new_url != url:
        gcloud("run", "services", "update", SERVICE, f"--region={REGION}",
               f"--update-env-vars=PUBLIC_URL={new_url}")
    print(f"\nDone: {new_url}/chat\n")
    print("Register these redirect URLs (once):")
    for key, label in (("GOOGLE_CLIENT_ID", "google"), ("MICROSOFT_CLIENT_ID", "microsoft"),
                       ("GITHUB_CLIENT_ID", "github")):
        if ENV.get(key):
            print(f"   {label:10} {new_url}/auth/callback/{label}")


if __name__ == "__main__":
    main()
