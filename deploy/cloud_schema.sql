-- One-time setup: a separate schema (and login) for the Cloud Run version.
-- Run it in the Neon console > SQL Editor, on the same database the local version uses.
-- Local keeps using the "public" schema; Cloud Run uses "cloud". They never see each other's chats.
--
-- 1. Replace <STRONG_PASSWORD> below (long and random; Neon rejects weak passwords).
-- 2. Run the whole script.
-- 3. Make the Cloud Run connection string: copy your normal one from the Neon dashboard and
--    replace the user and password with ponder_cloud / <STRONG_PASSWORD>.
--    Put it in .env as DATABASE_URL_CLOUD=... (deploy.py stores it in Secret Manager).

CREATE ROLE ponder_cloud WITH LOGIN PASSWORD '<STRONG_PASSWORD>';

-- let the owner role hand the new schema to ponder_cloud
GRANT ponder_cloud TO CURRENT_USER;

CREATE SCHEMA IF NOT EXISTS cloud AUTHORIZATION ponder_cloud;

-- Every connection made as ponder_cloud uses the cloud schema. This works through Neon's
-- connection pooler (a plain SET search_path does not).
ALTER ROLE ponder_cloud SET search_path TO cloud;

-- ponder_cloud can only create objects in its own schema
REVOKE CREATE ON SCHEMA public FROM ponder_cloud;

-- Check: should list ponder_cloud with search_path=cloud
SELECT rolname, rolconfig FROM pg_roles WHERE rolname = 'ponder_cloud';
