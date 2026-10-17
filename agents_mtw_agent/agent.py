import os


def _bypass_proxy(hosts=("localhost", "127.0.0.1")):
    """Append local hosts to NO_PROXY without wiping any corporate entries."""
    for key in ("NO_PROXY", "no_proxy"):
        current = [h.strip() for h in os.environ.get(key, "").split(",") if h.strip()]
        os.environ[key] = ",".join(dict.fromkeys(current + list(hosts)))


#def get_user_id(request_user: str | None = None) -> str:
    #"""Production: the authenticated user. Local: DEV_USER_ID, default test:local."""
    #if os.getenv("ENV", "").lower() == "local":
    #    return os.getenv("DEV_USER_ID", "test:local")
    #if not request_user:
    #    raise PermissionError("No authenticated user")  # fail closed outside local
    #return request_user

def get_user_id() -> str:
    return  "test:mark"
    #"""Production: the authenticated user. Local: DEV_USER_ID, default test:local."""
    #if os.getenv("ENV", "").lower() == "local":
    #    return os.getenv("DEV_USER_ID", "test:local")
    #if not request_user:
    #    raise PermissionError("No authenticated user")  # fail closed outside local
    #return request_user


# Must run before LiteLLM is imported
_bypass_proxy()
os.environ.setdefault("OLLAMA_API_BASE", "http://127.0.0.1:11434")

from google.adk.agents import LlmAgent
from google.adk.models.lite_llm import LiteLlm

from google.adk.tools import ToolContext
from google.adk.agents.remote_a2a_agent import RemoteA2aAgent

# Model comes from .env so you can switch without editing code
AGENT_MODEL = os.getenv("AGENT_MODEL", "gemini-3.5-flash-lite")

# Mock tool implementations
def get_current_time(city: str) -> dict:
    """Returns the current time in a specified city."""
    return {"status": "success", "city": city, "time": "10:30 AM"}


def greet_user(name: str) -> dict:
    """Greets a user with a friendly message."""
    return {"status": "success", "message": f"Hello {name}! Welcome!"}


def whoami(tool_context: ToolContext) -> dict:
    """Returns the user ID of the current session."""
    return {"user_id": tool_context.user_id}

echo_agent = RemoteA2aAgent(
    name="echo_agent",
    description="Public remote test agent that echoes back whatever text it is sent.",
    agent_card="https://echo-agent.mattiapapaccioli.com/.well-known/agent-card.json",
)

root_agent = LlmAgent(
    model=AGENT_MODEL,
    name="root_agent",
    description="Tells the current time in a specified city and greets users.",
    instruction=(
        "You are a helpful assistant that tells the current time in cities and greets users. "
        "Use the 'get_current_time' and 'greet_user' tools as appropriate. "
        "If the user asks to echo something or to test the echo agent, transfer to echo_agent."
    ),
    tools=[get_current_time, greet_user, whoami],
    sub_agents=[echo_agent],
)

# No Runner here: `adk web` / `adk api_server` build their own from root_agent.
# Sessions go to Postgres via --session_service_uri (see run.ps1).