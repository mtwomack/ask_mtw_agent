# Ask MTW Agent

A small Google ADK web agent that runs a local Qwen model through Ollama. It can answer requests using a few example tools, and the web UI can use either in-memory sessions or PostgreSQL-backed sessions and long-term memory.

## What it does

- Runs `qwen2.5:7b` locally through LiteLLM and Ollama.
- Provides example tools for greeting a user, returning a sample city time, and identifying the current session user.
- Starts the Google ADK web interface with `adk web`.
- Supports in-memory sessions for quick local testing, or PostgreSQL sessions and database-backed memory for persistent use.

The time tool currently returns a fixed example time; it does not look up live time-zone data.

## Requirements

- Windows (the included launcher is `adkweb.cmd`).
- Python 3.11 or newer.
- [uv](https://docs.astral.sh/uv/) for creating the environment and running the project.
- [Ollama](https://ollama.com/) installed and running locally.
- A PostgreSQL database only if you want persistent sessions and long-term memory.

## Set up

Open PowerShell or Command Prompt in the project directory and install the Python dependencies:

```powershell
uv sync
ollama pull qwen2.5:7b
```

The same commands work in Command Prompt:

```cmd
uv sync
ollama pull qwen2.5:7b
```

Make sure Ollama is running before starting the agent. The project defaults to `http://127.0.0.1:11434` for Ollama and configures local addresses to bypass HTTP proxies.

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

Edit `.env` and set at least `DATABASE_URL` to your PostgreSQL connection string. For example:

```dotenv
DATABASE_URL=postgresql://USER:PASSWORD@HOST:5432/DATABASE?sslmode=require
ENV=local
DEV_USER_ID=your-local-user
```

Replace the example values with your own. Do not commit `.env` or share its credentials; `.env` is ignored by Git. The database service translates `sslmode` for its async PostgreSQL driver and has handling for pooled database hosts.

The `.env.example` also contains `GOOGLE_GENAI_USE_VERTEXAI` and `GOOGLE_API_KEY` placeholders. They are not needed for the current Ollama model configuration. `ENV=local` enables the development user override, and `DEV_USER_ID` selects that local session user.

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
|-- pyproject.toml        Project metadata and dependencies
|-- readme.txt            Original quick-start notes
|-- services.py           ADK session and memory service registration
`-- uv.lock               Locked dependency versions
```

The `.venv/`, `__pycache__/`, and `*.egg-info/` directories are generated locally and are not shown above.

## Troubleshooting

- **The model cannot be reached:** Confirm Ollama is running, then check that `qwen2.5:7b` is installed with `ollama list`.
- **Database mode cannot connect:** Check that `DATABASE_URL` in `.env` is valid and that the database accepts connections from your network. Use `adkweb mem` to run without PostgreSQL.
- **The browser does not open automatically:** Use the local URL printed by `adk web` in the terminal.
