# Ask MTW Agent

A small Google ADK web agent with a configurable language model. It defaults to Gemini 3.5 Flash Lite and includes example tools plus a remote A2A echo sub-agent. The web UI can use in-memory sessions or PostgreSQL-backed sessions and long-term memory.

## What it does

- Uses the model configured by `AGENT_MODEL`, defaulting to `gemini-3.5-flash-lite`.
- Provides example tools for greeting a user, returning a sample city time, and identifying the current session user.
- Can transfer echo requests to a public remote A2A echo agent.
- Starts the Google ADK web interface with `adk web`.
- Supports in-memory sessions for quick local testing, or PostgreSQL sessions and database-backed memory for persistent use.

The time tool currently returns a fixed example time; it does not look up live time-zone data. The echo sub-agent is hosted remotely, so using it requires network access.

## Requirements

- Windows (the included launcher is `adkweb.cmd`).
- Python 3.11 or newer.
- [uv](https://docs.astral.sh/uv/) for creating the environment and running the project.
- A Google AI Studio API key for the default Gemini configuration, or credentials for whichever model provider you configure.
- A PostgreSQL database only if you want persistent sessions and long-term memory.

## Set up

Open PowerShell or Command Prompt in the project directory and install the Python dependencies:

```powershell
uv sync
```

The same commands work in Command Prompt:

```cmd
uv sync
```

Configure the model and provider credentials in `.env` before starting the agent. The current model path uses a Gemini model ID; for the default Gemini model with a Google AI Studio API key, set `AGENT_MODEL=gemini-3.5-flash-lite`, `GOOGLE_GENAI_USE_VERTEXAI=0`, and `GOOGLE_API_KEY` to your key.

## Configure environment

For the in-memory mode, no `.env` file is required. To configure the database-backed mode, create a local environment file from the example.

PowerShell:

```powershell
Copy-Item .env.example .env
```

Command Prompt:

```cmd
copy .env.example .env
```

Edit `.env` and replace the placeholders with your settings. For the default Gemini setup, configure the model and Google credentials. Add `DATABASE_URL` only if you intend to use PostgreSQL mode. For example:

```dotenv
AGENT_MODEL=gemini-3.5-flash-lite
GOOGLE_GENAI_USE_VERTEXAI=0
GOOGLE_API_KEY=your-google-ai-studio-api-key
ENV=local
DEV_USER_ID=your-local-user
DATABASE_URL=postgresql://USER:PASSWORD@HOST:5432/DATABASE?sslmode=require
```

Use only one `GOOGLE_GENAI_USE_VERTEXAI` assignment in `.env`; the example template may show both a default and a placeholder for this setting. Use `0` for a Google AI Studio API key, or configure the appropriate Vertex AI credentials and set it to `1`. Replace all example values with your own. Do not commit `.env` or share its credentials; `.env` is ignored by Git. The database service translates `sslmode` for its async PostgreSQL driver and has handling for pooled database hosts.

`ENV=local` enables the development user override, and `DEV_USER_ID` selects that local session user.

## Run the web app

From the project directory, use the included Windows launcher. In PowerShell, prefix the command with `.` and `\`; in Command Prompt, run the `.cmd` file by name.

### In-memory mode

This mode does not require PostgreSQL and does not persist sessions or memory between runs:

```powershell
.\adkweb.cmd mem
```

```cmd
adkweb.cmd mem
```

### PostgreSQL mode

This mode uses `DATABASE_URL` from `.env` for PostgreSQL-backed sessions and long-term memory:

```powershell
.\adkweb.cmd
```

```cmd
adkweb.cmd
```

The launcher starts the ADK web interface. Open the local URL printed in the terminal; ADK commonly uses `http://localhost:8000` by default. Press `Ctrl+C` in the terminal to stop it.

Additional ADK CLI options can be passed through the launcher, for example:

```powershell
.\adkweb.cmd --log_level DEBUG
```

```cmd
adkweb.cmd --log_level DEBUG
```

## Project layout

```text
ask_mtw_agent/
|-- .adk/                 ADK-generated artifacts and session data
|-- .env                  Local settings and secrets (create from .env.example)
|-- .env.example          Environment variable template
|-- .gitignore            Excludes secrets and generated files
|-- README.md             Project overview and setup instructions
|-- __init__.py           Python package initialization
|-- adkweb.cmd            Windows launcher for ADK web
|-- agent.py              Agent, Ollama model, and example tools
|-- dbtest.py             PostgreSQL connectivity check
|-- eval_set_1.evalset.json ADK evaluation set (currently empty)
|-- pyproject.toml        Project metadata and dependencies
|-- readme.txt            Original quick-start notes
|-- services.py           ADK session and memory service registration
`-- uv.lock               Locked dependency versions
```

The `.venv/`, `__pycache__/`, and `*.egg-info/` directories are generated locally and are not shown above.

## Troubleshooting

- **Gemini authentication fails:** Check that `GOOGLE_API_KEY` is set and that `.env` has one correct `GOOGLE_GENAI_USE_VERTEXAI` value.
- **The echo agent does not respond:** Check that the machine can reach the configured remote agent over the network.
- **Database mode cannot connect:** Check that `DATABASE_URL` in `.env` is valid and that the database accepts connections from your network. Use `adkweb mem` to run without PostgreSQL.
- **The browser does not open automatically:** Use the local URL printed by `adk web` in the terminal.
