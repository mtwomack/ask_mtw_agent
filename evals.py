"""
Agent evals for the chat app - the /evals page (admins only).

    /evals                    the page (evals.html)
    /evals/api/...            its JSON API (below)

An eval set is a list of test conversations: what the user says, which tools the agent should
call, and what a good answer looks like (ADK's .evalset.json format). A run sends every case to
the agent and scores the result with ADK's own evaluators - the same code as `adk eval` and the
dev UI's Eval tab. Unlike those, sets and results live in Postgres (DATABASE_URL), so they
survive restarts and Cloud Run deploys, and every run is kept for comparison.

Tables (created on first use, in the same schema as the chats - "cloud" on Cloud Run):
    eval_sets      one row per set, with its scoring criteria (test_config.json "criteria")
    eval_cases     one row per case, the ADK EvalCase as JSON
    eval_runs      one row per run: who started it, model, status, totals
    eval_results   one row per case per run: pass/fail, scores, expected vs actual

Who can use it: admins only (ADMIN_EMAILS). auth.py already blocks everyone else; the routes
check again. With AUTH_MODE=off (local, no sign-in) you're the only user, so you're allowed.

Runs use a separate in-memory session store, so eval conversations never show up in anyone's
chat list or in account activity. One run at a time.

Needs ADK's eval extra:  google-adk[eval]  (pyproject.toml).
"""
import asyncio
import json
import logging
import os
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

import auth
import services

log = logging.getLogger("evals")

HERE = Path(__file__).resolve().parent
APP_NAME = ""                       # set by install()

# What the run dialog offers. Thresholds are the defaults; the set remembers what you pick.
METRICS = [
    {"name": "tool_trajectory_avg_score", "label": "Tool calls match",
     "help": "Same tools, same arguments, same order as expected.", "default": 1.0, "on": True},
    {"name": "final_response_match_v2", "label": "Answer means the same (LLM judge)",
     "help": "Gemini compares the answer with the expected one. Calls Gemini for each case.",
     "default": 0.8, "on": True},
    {"name": "response_match_score", "label": "Answer word match (ROUGE)",
     "help": "Word overlap with the expected answer. Off for live prices - the numbers change.",
     "default": 0.8, "on": False},
    {"name": "rubric_based_final_response_quality_v1", "label": "Rubric checks (LLM judge)",
     "help": "Gemini checks the answer against each case's rubric lines (set in the case editor). "
             "Score = share of lines met.", "default": 0.8, "on": False},
    {"name": "hallucinations_v1", "label": "No hallucinations (LLM judge)",
     "help": "Checks the answer is supported by what the tools returned.", "default": 0.8, "on": False},
]
RUBRIC_TYPE = "FINAL_RESPONSE_QUALITY"   # what rubric_based_final_response_quality_v1 reads
METRIC_NAMES = {m["name"] for m in METRICS}
DEFAULT_CRITERIA = {m["name"]: m["default"] for m in METRICS if m["on"]}
INFO_METRICS = {"tool_call_count_v1", "inference_call_count_v1", "token_usage_v1",
                "invocation_duration_v1"}

ID_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.\-]{0,79}")
MAX_CASES = 200
RUN_PARALLELISM = 2

router = APIRouter(prefix="/evals")

_engine = None
_ready = False
_ready_lock = asyncio.Lock()
_run_lock = asyncio.Lock()
_tasks: set[asyncio.Task] = set()
_chat_sessions = None


# ------------------------------------------------------------------ database
SCHEMA_SQL = [
    """create table if not exists eval_sets (
        app_name text not null, eval_set_id text not null, name text,
        criteria jsonb not null default '{}'::jsonb,
        created_at timestamptz not null default now(), updated_at timestamptz not null default now(),
        primary key (app_name, eval_set_id))""",
    """create table if not exists eval_cases (
        app_name text not null, eval_set_id text not null, eval_id text not null,
        data jsonb not null, position double precision not null default 0,
        created_at timestamptz not null default now(), updated_at timestamptz not null default now(),
        primary key (app_name, eval_set_id, eval_id),
        foreign key (app_name, eval_set_id) references eval_sets on delete cascade)""",
    """create table if not exists eval_runs (
        run_id bigserial primary key, app_name text not null, eval_set_id text not null,
        label text, model text, criteria jsonb not null default '{}'::jsonb,
        status text not null default 'running', phase text, message text,
        total int not null default 0, done int not null default 0,
        passed int not null default 0, failed int not null default 0, errors int not null default 0,
        tokens bigint, started_by text,
        started_at timestamptz not null default now(), finished_at timestamptz)""",
    "create index if not exists eval_runs_set on eval_runs (app_name, eval_set_id, run_id desc)",
    """create table if not exists eval_results (
        run_id bigint not null references eval_runs on delete cascade, eval_id text not null,
        status text not null, data jsonb not null, primary key (run_id, eval_id))""",
]


def _get_engine():
    """Async engine on the chats database (same URL handling as services.py)."""
    global _engine
    if _engine is None:
        uri = os.getenv("SESSION_SERVICE_URI", "devpg://")
        if not (uri.startswith("devpg://") or uri.startswith("postgresql")):
            return None
        from sqlalchemy.ext.asyncio import create_async_engine
        url, kwargs = services._pg(uri if uri.startswith("devpg://") else "devpg://" + uri.split("://", 1)[1])
        _engine = create_async_engine(url, **kwargs)
    return _engine


async def _db():
    """The engine, with the tables created and any run cut off by a restart marked as such."""
    global _ready
    engine = _get_engine()
    if engine is None:
        raise HTTPException(503, "Evals need the Postgres session store (DATABASE_URL). "
                                 "This server uses in-memory sessions (chat mem).")
    if not _ready:
        async with _ready_lock:
            if not _ready:
                from sqlalchemy import text
                async with engine.begin() as conn:
                    for sql in SCHEMA_SQL:
                        await conn.execute(text(sql))
                    await conn.execute(text(
                        "update eval_runs set status='interrupted', phase=null, finished_at=now(), "
                        "message='The server restarted during this run.' "
                        "where status='running' and app_name=:app"), {"app": APP_NAME})
                _ready = True
    return engine


async def _q(sql: str, **params) -> list:
    from sqlalchemy import text
    async with (await _db()).begin() as conn:
        result = await conn.execute(text(sql), params)
        return result.mappings().all() if result.returns_rows else []


def _j(value) -> str:
    return json.dumps(value)


# ------------------------------------------------------------------ who may call
async def _admin(request: Request) -> str:
    """The admin's user ID, or 403. With sign-in off (local) there's only you."""
    if auth.MODE == "off":
        return services.DEV_USER_ID or "local"
    who = await auth.current_user(request)
    if not who or not auth.is_admin(who["email"]):
        raise HTTPException(403, "Evals are only available to admins.")
    return who["email"]


def _check_id(value: str, what: str) -> str:
    value = (value or "").strip()
    if not ID_RE.fullmatch(value):
        raise HTTPException(400, f"{what} can use letters, numbers, _ . - (up to 80, no spaces).")
    return value


# ------------------------------------------------------------------ ADK eval adapter
# Everything that touches google.adk.evaluation is in this section, so an ADK upgrade that
# moves things around only needs changes here.
def _adk():
    try:
        from google.adk.evaluation.base_eval_service import (EvaluateConfig, EvaluateRequest,
                                                             InferenceConfig, InferenceRequest,
                                                             InferenceStatus)
        from google.adk.evaluation.eval_case import EvalCase, get_all_tool_calls
        from google.adk.evaluation.eval_config import EvalConfig, get_eval_metrics_from_config
        from google.adk.evaluation.eval_set import EvalSet
        from google.adk.evaluation.in_memory_eval_sets_manager import InMemoryEvalSetsManager
        from google.adk.evaluation.local_eval_service import LocalEvalService
    except ModuleNotFoundError as e:
        raise HTTPException(500, f"ADK's eval extra isn't installed ({e.name}). "
                                 "Run: uv sync  (pyproject has google-adk[eval]).") from e
    return locals()


def _validate_case(data: dict) -> dict:
    """Checks a case against ADK's EvalCase model; returns it tidied (no nulls)."""
    a = _adk()
    data = dict(data)
    si = dict(data.get("session_input") or data.get("sessionInput") or {})
    si.pop("sessionId", None)
    data.pop("sessionInput", None)
    data["session_input"] = {"app_name": APP_NAME, "user_id": si.get("user_id") or si.get("userId") or "eval@test",
                             "state": si.get("state") or {}}
    try:
        case = a["EvalCase"].model_validate(data)
    except Exception as e:                              # pydantic error -> readable 400
        raise HTTPException(400, f"Case {data.get('eval_id', '?')}: {_short_error(e)}") from e
    if not case.conversation and not case.conversation_scenario:
        raise HTTPException(400, f"Case {case.eval_id}: it has no conversation.")
    return case.model_dump(mode="json", exclude_none=True)


def _short_error(e: Exception) -> str:
    errors = getattr(e, "errors", None)
    if callable(errors):
        try:
            first = errors()[0]
            return f"{'.'.join(str(p) for p in first.get('loc', []))}: {first.get('msg')}"
        except Exception:
            pass
    return str(e).splitlines()[0][:300]


def _text(content) -> str:
    """Text of a genai Content (or its dict), skipping thoughts and files."""
    if content is None:
        return ""
    parts = content.get("parts") if isinstance(content, dict) else content.parts
    out = []
    for p in parts or []:
        text = p.get("text") if isinstance(p, dict) else p.text
        thought = p.get("thought") if isinstance(p, dict) else p.thought
        if text and not thought:
            out.append(text)
    return "\n".join(out).strip()


def _calls(intermediate_data) -> list[dict]:
    if intermediate_data is None:
        return []
    a = _adk()
    return [{"name": c.name, "args": c.args or {}} for c in a["get_all_tool_calls"](intermediate_data)]


def _case_summary(row) -> dict:
    data = row["data"]
    turns = data.get("conversation") or []
    first = turns[0] if turns else {}
    rubrics = len(data.get("rubrics") or []) + sum(len(t.get("rubrics") or []) for t in turns)
    return {"eval_id": row["eval_id"], "turns": len(turns), "rubrics": rubrics,
            "user": _text(first.get("user_content")),
            "tools": [c["name"] for t in turns
                      for c in ((t.get("intermediate_data") or {}).get("tool_uses") or [])],
            "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None}


def _session_to_case(session, eval_id: str, user_id: str) -> dict:
    """A saved chat -> an eval case: each user message, the tools called and the final answer.
    Files are left out (they were uploads for that chat) and thoughts are dropped."""
    from google.adk.cli.utils.evals import convert_session_to_eval_invocations
    turns = []
    for inv in convert_session_to_eval_invocations(session):
        user = _text(inv.user_content)
        if not user:
            continue
        turn = {"user_content": {"role": "user", "parts": [{"text": user}]},
                "intermediate_data": {"tool_uses": _calls(inv.intermediate_data)}}
        answer = _text(inv.final_response)
        if answer:
            turn["final_response"] = {"role": "model", "parts": [{"text": answer}]}
        turns.append(turn)
    if not turns:
        raise HTTPException(400, "That chat has no messages to turn into a case.")
    return _validate_case({"eval_id": eval_id, "conversation": turns,
                           "session_input": {"user_id": user_id, "state": {}}})


def _metrics_from(criteria: dict):
    a = _adk()
    return a["get_eval_metrics_from_config"](a["EvalConfig"].model_validate({"criteria": criteria}))


def _model_name(agent) -> str:
    model = getattr(agent, "model", "")
    return str(getattr(model, "model", model) or "")


def _schema_params(schema) -> list[dict]:
    """A tool's parameters (genai Schema) -> [{name, type, required, description}]."""
    if schema is None:
        return []
    props = getattr(schema, "properties", None) or {}
    required = set(getattr(schema, "required", None) or [])
    out = []
    for name, prop in props.items():
        kind = getattr(prop, "type", None)
        kind = (getattr(kind, "value", kind) or "string")
        out.append({"name": name, "type": str(kind).lower(), "required": name in required,
                    "description": (getattr(prop, "description", "") or "")[:200]})
    return out


async def _agent_tools() -> list[dict]:
    """The root agent's tools as the model sees them: name, description, parameters.
    Used by the case editor so expected tool calls use real names and arguments."""
    from google.adk.tools.base_tool import BaseTool
    from google.adk.tools.base_toolset import BaseToolset
    from google.adk.tools.function_tool import FunctionTool
    root, _ = _load_agent()
    tools = []
    for t in getattr(root, "tools", []) or []:
        try:
            if isinstance(t, BaseToolset):
                tools.extend(await t.get_tools())
            elif isinstance(t, BaseTool):
                tools.append(t)
            elif callable(t):
                tools.append(FunctionTool(t))
        except Exception as e:                       # noqa: BLE001 - one bad toolset shouldn't hide the rest
            log.warning("evals: couldn't list tools from %s: %s", t, e)
    out, seen = [], set()
    for tool in tools:
        try:
            decl = tool._get_declaration()
        except Exception:
            decl = None
        if not decl or decl.name in seen:
            continue
        seen.add(decl.name)
        params = _schema_params(decl.parameters)
        if not params and getattr(decl, "parameters_json_schema", None):
            js = decl.parameters_json_schema or {}
            params = [{"name": k, "type": str((v or {}).get("type", "string")), "required": k in (js.get("required") or []),
                       "description": str((v or {}).get("description", ""))[:200]}
                      for k, v in (js.get("properties") or {}).items()]
        out.append({"name": decl.name, "description": (decl.description or "").strip().split("\n")[0][:200],
                    "params": params})
    if getattr(root, "sub_agents", None):
        out.append({"name": "transfer_to_agent", "description": "Hands the conversation to a sub-agent: "
                    + ", ".join(a.name for a in root.sub_agents),
                    "params": [{"name": "agent_name", "type": "string", "required": True, "description": ""}]})
    return out


def _load_agent():
    from google.adk.cli.utils.agent_loader import AgentLoader
    loaded = AgentLoader(str(HERE)).load_agent(APP_NAME)
    root = getattr(loaded, "root_agent", loaded)
    return root, (loaded if hasattr(loaded, "root_agent") else None)


def _status_name(status) -> str:
    name = getattr(status, "name", str(status)).lower()
    return {"passed": "passed", "failed": "failed"}.get(name, name)


def _result_payload(case: dict, case_result) -> tuple[str, dict]:
    """ADK's EvalCaseResult -> what the page shows: overall and per-turn scores, expected vs
    actual tool calls and answers, rubric reasons."""
    overall, info = [], {}
    for m in case_result.overall_eval_metric_results or []:
        if m.metric_name in INFO_METRICS:
            info[m.metric_name] = m.score
            continue
        overall.append({"name": m.metric_name, "score": m.score, "threshold": m.threshold,
                        "status": _status_name(m.eval_status)})
    turns = []
    expected_turns = case.get("conversation") or []
    for i, per in enumerate(case_result.eval_metric_result_per_invocation or []):
        expected = per.expected_invocation
        exp_dict = expected_turns[i] if i < len(expected_turns) else {}
        reasons = []
        scores = []
        for m in per.eval_metric_results or []:
            if m.metric_name in INFO_METRICS:
                continue
            scores.append({"name": m.metric_name, "score": m.score, "status": _status_name(m.eval_status)})
            for r in ((m.details.rubric_scores if m.details else None) or []):
                if getattr(r, "rationale", None):
                    reasons.append({"metric": m.metric_name, "score": r.score, "text": r.rationale})
        turns.append({
            "user": _text(per.actual_invocation.user_content),
            "expected_tools": _calls(expected.intermediate_data) if expected
            else (exp_dict.get("intermediate_data") or {}).get("tool_uses", []),
            "actual_tools": _calls(per.actual_invocation.intermediate_data),
            "expected_answer": _text(expected.final_response) if expected else "",
            "actual_answer": _text(per.actual_invocation.final_response),
            "scores": scores, "reasons": reasons,
        })
    status = _status_name(case_result.final_eval_status)
    return status, {"metrics": overall, "turns": turns,
                    "tokens": info.get("token_usage_v1"), "seconds": info.get("invocation_duration_v1"),
                    "tool_calls": info.get("tool_call_count_v1")}


async def _execute_run(run_id: int, eval_set_id: str, cases: list[dict], criteria: dict):
    """Runs every case through the agent, then scores them. Saves each result as it lands."""
    a = _adk()
    by_id = {c["eval_id"]: c for c in cases}
    try:
        root, app = _load_agent()
        manager = a["InMemoryEvalSetsManager"]()
        manager.create_eval_set(APP_NAME, eval_set_id)
        for c in cases:
            manager.add_eval_case(APP_NAME, eval_set_id, a["EvalCase"].model_validate(c))
        kwargs = {"app": app} if app is not None else {}
        service = a["LocalEvalService"](root_agent=root, eval_sets_manager=manager, **kwargs)
        metrics = _metrics_from(criteria)

        inferences, done = [], 0
        request = a["InferenceRequest"](app_name=APP_NAME, eval_set_id=eval_set_id,
                                        inference_config=a["InferenceConfig"](parallelism=RUN_PARALLELISM))
        async for inf in service.perform_inference(inference_request=request):
            done += 1
            if inf.status == a["InferenceStatus"].SUCCESS:
                inferences.append(inf)
            else:
                await _save_result(run_id, inf.eval_case_id, "error",
                                   {"error": inf.error_message or "The agent failed on this case."})
            await _q("update eval_runs set done=:d where run_id=:r", d=done, r=run_id)

        await _q("update eval_runs set phase='scoring' where run_id=:r", r=run_id)
        if inferences:
            evaluate = a["EvaluateRequest"](inference_results=inferences,
                                            evaluate_config=a["EvaluateConfig"](
                                                eval_metrics=metrics, parallelism=RUN_PARALLELISM))
            async for case_result in service.evaluate(evaluate_request=evaluate):
                status, payload = _result_payload(by_id.get(case_result.eval_id, {}), case_result)
                await _save_result(run_id, case_result.eval_id, status, payload)

        await _q("""update eval_runs r set status='done', phase=null, finished_at=now(),
                    passed=(select count(*) from eval_results where run_id=r.run_id and status='passed'),
                    failed=(select count(*) from eval_results where run_id=r.run_id and status='failed'),
                    errors=(select count(*) from eval_results where run_id=r.run_id
                            and status not in ('passed','failed')),
                    tokens=(select sum((data->>'tokens')::numeric) from eval_results
                            where run_id=r.run_id)
                    where run_id=:r""", r=run_id)
    except Exception as e:                       # noqa: BLE001 - shown on the page
        log.exception("eval run %s failed", run_id)
        message = e.detail if isinstance(e, HTTPException) else f"{type(e).__name__}: {e}"
        await _q("update eval_runs set status='error', phase=null, finished_at=now(), message=:m "
                 "where run_id=:r", m=str(message)[:1000], r=run_id)


async def _save_result(run_id: int, eval_id: str, status: str, payload: dict):
    await _q("""insert into eval_results (run_id, eval_id, status, data)
                values (:r, :e, :s, cast(:d as jsonb))
                on conflict (run_id, eval_id) do update set status=excluded.status, data=excluded.data""",
             r=run_id, e=eval_id, s=status, d=_j(payload))


# ------------------------------------------------------------------ API: sets and cases
def _iso(value):
    return value.isoformat() if value else None


def _run_row(r) -> dict:
    return {"run_id": r["run_id"], "eval_set_id": r["eval_set_id"], "label": r["label"],
            "model": r["model"], "criteria": r["criteria"], "status": r["status"], "phase": r["phase"],
            "message": r["message"], "total": r["total"], "done": r["done"], "passed": r["passed"],
            "failed": r["failed"], "errors": r["errors"], "tokens": r["tokens"],
            "started_by": r["started_by"], "started_at": _iso(r["started_at"]),
            "finished_at": _iso(r["finished_at"])}


@router.get("/api/info")
async def info(request: Request):
    user = await _admin(request)
    running = await _q("select run_id from eval_runs where app_name=:a and status='running' "
                       "order by run_id desc limit 1", a=APP_NAME)
    try:
        import google.adk
        adk_version = google.adk.__version__
    except Exception:
        adk_version = "?"
    return {"app_name": APP_NAME, "user": user, "metrics": METRICS, "default_criteria": DEFAULT_CRITERIA,
            "running_run_id": running[0]["run_id"] if running else None, "adk_version": adk_version,
            "sign_in": auth.MODE != "off"}


@router.get("/api/sets")
async def list_sets(request: Request):
    await _admin(request)
    rows = await _q("""
        select s.eval_set_id, s.name, s.criteria, s.updated_at,
               (select count(*) from eval_cases c where c.app_name=s.app_name and c.eval_set_id=s.eval_set_id) as cases,
               r.run_id, r.status, r.passed, r.failed, r.errors, r.total, r.started_at
        from eval_sets s
        left join lateral (select * from eval_runs r where r.app_name=s.app_name and r.eval_set_id=s.eval_set_id
                           order by run_id desc limit 1) r on true
        where s.app_name=:a order by s.eval_set_id""", a=APP_NAME)
    return [{"eval_set_id": r["eval_set_id"], "name": r["name"] or r["eval_set_id"], "cases": r["cases"],
             "last_run": None if r["run_id"] is None else
             {"run_id": r["run_id"], "status": r["status"], "passed": r["passed"], "failed": r["failed"],
              "errors": r["errors"], "total": r["total"], "started_at": _iso(r["started_at"])}}
            for r in rows]


async def _set_or_404(eval_set_id: str):
    rows = await _q("select * from eval_sets where app_name=:a and eval_set_id=:s", a=APP_NAME, s=eval_set_id)
    if not rows:
        raise HTTPException(404, f"No eval set called {eval_set_id}.")
    return rows[0]


@router.post("/api/sets")
async def create_set(request: Request):
    await _admin(request)
    body = await request.json()
    set_id = _check_id(body.get("eval_set_id"), "The set ID")
    name = (body.get("name") or "").strip()[:120] or set_id
    rows = await _q("""insert into eval_sets (app_name, eval_set_id, name, criteria)
                       values (:a, :s, :n, cast(:c as jsonb)) on conflict do nothing returning eval_set_id""",
                    a=APP_NAME, s=set_id, n=name, c=_j(DEFAULT_CRITERIA))
    if not rows:
        raise HTTPException(409, f"There's already a set called {set_id}.")
    return {"eval_set_id": set_id, "name": name}


@router.get("/api/sets/{eval_set_id}")
async def get_set(eval_set_id: str, request: Request):
    await _admin(request)
    s = await _set_or_404(eval_set_id)
    cases = await _q("select eval_id, data, updated_at from eval_cases where app_name=:a and eval_set_id=:s "
                     "order by position, created_at", a=APP_NAME, s=eval_set_id)
    runs = await _q("select * from eval_runs where app_name=:a and eval_set_id=:s order by run_id desc limit 30",
                    a=APP_NAME, s=eval_set_id)
    return {"eval_set_id": s["eval_set_id"], "name": s["name"] or s["eval_set_id"], "criteria": s["criteria"],
            "cases": [_case_summary(c) for c in cases], "runs": [_run_row(r) for r in runs]}


@router.patch("/api/sets/{eval_set_id}")
async def update_set(eval_set_id: str, request: Request):
    await _admin(request)
    await _set_or_404(eval_set_id)
    body = await request.json()
    if "name" in body:
        await _q("update eval_sets set name=:n, updated_at=now() where app_name=:a and eval_set_id=:s",
                 n=(body["name"] or "").strip()[:120] or eval_set_id, a=APP_NAME, s=eval_set_id)
    if "criteria" in body:
        criteria = _clean_criteria(body["criteria"])
        await _q("update eval_sets set criteria=cast(:c as jsonb), updated_at=now() "
                 "where app_name=:a and eval_set_id=:s", c=_j(criteria), a=APP_NAME, s=eval_set_id)
    return {"ok": True}


@router.delete("/api/sets/{eval_set_id}")
async def delete_set(eval_set_id: str, request: Request):
    await _admin(request)
    await _q("delete from eval_runs where app_name=:a and eval_set_id=:s", a=APP_NAME, s=eval_set_id)
    await _q("delete from eval_sets where app_name=:a and eval_set_id=:s", a=APP_NAME, s=eval_set_id)
    return {"ok": True}


def _clean_criteria(criteria) -> dict:
    if not isinstance(criteria, dict) or not criteria:
        raise HTTPException(400, "Pick at least one way to score.")
    out = {}
    for name, value in criteria.items():
        if isinstance(value, (int, float)):
            if not 0 <= float(value) <= 1:
                raise HTTPException(400, f"{name}: the threshold must be between 0 and 1.")
            out[name] = float(value)
        elif isinstance(value, dict):                    # a full criterion (e.g. rubrics) from a file
            out[name] = value
        else:
            raise HTTPException(400, f"{name}: the threshold must be a number.")
    try:
        _metrics_from(out)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, f"Scoring settings aren't valid: {_short_error(e)}") from e
    return out


@router.get("/api/sets/{eval_set_id}/cases/{eval_id}")
async def get_case(eval_set_id: str, eval_id: str, request: Request):
    await _admin(request)
    rows = await _q("select data from eval_cases where app_name=:a and eval_set_id=:s and eval_id=:e",
                    a=APP_NAME, s=eval_set_id, e=eval_id)
    if not rows:
        raise HTTPException(404, "Case not found.")
    return rows[0]["data"]


async def _upsert_case(eval_set_id: str, case: dict) -> bool:
    """Saves a case; True if it was new."""
    rows = await _q("""insert into eval_cases (app_name, eval_set_id, eval_id, data, position)
                       values (:a, :s, :e, cast(:d as jsonb), extract(epoch from now()))
                       on conflict (app_name, eval_set_id, eval_id)
                       do update set data=excluded.data, updated_at=now()
                       returning (xmax = 0) as inserted""",
                    a=APP_NAME, s=eval_set_id, e=case["eval_id"], d=_j(case))
    return bool(rows and rows[0]["inserted"])


@router.get("/api/tools")
async def agent_tools(request: Request):
    await _admin(request)
    try:
        return await _agent_tools()
    except Exception as e:                           # noqa: BLE001 - the editor still works without the list
        log.exception("evals: listing tools failed")
        raise HTTPException(500, f"Couldn't list the agent's tools ({type(e).__name__}: {e}).") from e


@router.post("/api/sets/{eval_set_id}/cases")
async def create_case(eval_set_id: str, request: Request):
    """A new case from the case editor. 409 if the ID is taken (the editor asks before replacing)."""
    await _admin(request)
    await _set_or_404(eval_set_id)
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "A case is a JSON object.")
    _check_id(body.get("eval_id"), "The case ID")
    case = _validate_case(body)
    taken = await _q("select 1 from eval_cases where app_name=:a and eval_set_id=:s and eval_id=:e",
                     a=APP_NAME, s=eval_set_id, e=case["eval_id"])
    if taken:
        raise HTTPException(409, f"There's already a case called {case['eval_id']}.")
    await _upsert_case(eval_set_id, case)
    return case


@router.put("/api/sets/{eval_set_id}/cases/{eval_id}")
async def put_case(eval_set_id: str, eval_id: str, request: Request):
    """Save a case (edited as JSON on the page). A changed eval_id renames it."""
    await _admin(request)
    await _set_or_404(eval_set_id)
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "A case is a JSON object.")
    body.setdefault("eval_id", eval_id)
    _check_id(body["eval_id"], "The case ID")
    case = _validate_case(body)
    if case["eval_id"] != eval_id:
        taken = await _q("select 1 from eval_cases where app_name=:a and eval_set_id=:s and eval_id=:e",
                         a=APP_NAME, s=eval_set_id, e=case["eval_id"])
        if taken:
            raise HTTPException(409, f"There's already a case called {case['eval_id']}.")
        await _q("update eval_cases set eval_id=:n, data=cast(:d as jsonb), updated_at=now() "
                 "where app_name=:a and eval_set_id=:s and eval_id=:e",
                 n=case["eval_id"], d=_j(case), a=APP_NAME, s=eval_set_id, e=eval_id)
    else:
        await _upsert_case(eval_set_id, case)
    return case


@router.delete("/api/sets/{eval_set_id}/cases/{eval_id}")
async def delete_case(eval_set_id: str, eval_id: str, request: Request):
    await _admin(request)
    await _q("delete from eval_cases where app_name=:a and eval_set_id=:s and eval_id=:e",
             a=APP_NAME, s=eval_set_id, e=eval_id)
    return {"ok": True}


@router.post("/api/sets/{eval_set_id}/import")
async def import_cases(eval_set_id: str, request: Request):
    """An ADK .evalset.json (and optionally a test_config.json's criteria). Existing cases
    with the same ID are replaced; everything is checked before anything is saved."""
    await _admin(request)
    await _set_or_404(eval_set_id)
    body = await request.json()
    evalset = body.get("evalset") if isinstance(body, dict) and "evalset" in body else body
    if isinstance(evalset, dict):
        raw_cases = evalset.get("eval_cases", evalset.get("evalCases"))
    else:
        raw_cases = None
    if not isinstance(raw_cases, list):
        raise HTTPException(400, "That isn't an ADK eval set - expected {\"eval_set_id\": ..., \"eval_cases\": [...]}.")
    if not raw_cases:
        raise HTTPException(400, "The file has no eval cases.")
    if len(raw_cases) > MAX_CASES:
        raise HTTPException(400, f"Up to {MAX_CASES} cases per import.")
    cases = []
    for c in raw_cases:
        if not isinstance(c, dict):
            raise HTTPException(400, "Each eval case must be a JSON object.")
        c = dict(c)
        c["eval_id"] = _check_id(c.get("eval_id") or c.get("evalId"), "Each case ID")
        c.pop("evalId", None)
        cases.append(_validate_case(c))
    ids = [c["eval_id"] for c in cases]
    if len(set(ids)) != len(ids):
        raise HTTPException(400, "The file has two cases with the same eval_id.")
    criteria = body.get("criteria") if isinstance(body, dict) else None
    if isinstance(criteria, dict) and "criteria" in criteria:       # a whole test_config.json
        criteria = criteria["criteria"]
    if criteria:
        criteria = _clean_criteria(criteria)
    added = 0
    for c in cases:
        added += await _upsert_case(eval_set_id, c)
    if criteria:
        await _q("update eval_sets set criteria=cast(:c as jsonb), updated_at=now() "
                 "where app_name=:a and eval_set_id=:s", c=_j(criteria), a=APP_NAME, s=eval_set_id)
    return {"added": added, "updated": len(cases) - added, "criteria_updated": bool(criteria)}


@router.get("/api/sets/{eval_set_id}/export")
async def export_set(eval_set_id: str, request: Request):
    await _admin(request)
    s = await _set_or_404(eval_set_id)
    cases = await _q("select data from eval_cases where app_name=:a and eval_set_id=:s "
                     "order by position, created_at", a=APP_NAME, s=eval_set_id)
    return JSONResponse(
        {"eval_set_id": eval_set_id, "name": s["name"] or eval_set_id,
         "eval_cases": [c["data"] for c in cases], "test_config": {"criteria": s["criteria"]}},
        headers={"Content-Disposition": f'attachment; filename="{eval_set_id}.evalset.json"'})


# ------------------------------------------------------------------ API: cases from chats
def _chat_session_service():
    """The chats' own session store (with the same local user override as the chat)."""
    global _chat_sessions
    if _chat_sessions is None:
        uri = os.getenv("SESSION_SERVICE_URI", "devpg://")
        if uri.startswith("devmem://"):
            raise HTTPException(503, "Chats are kept in memory on this server (chat mem); use Postgres.")
        _chat_sessions = services._devpg_factory(uri if uri.startswith("devpg://") else "devpg://" + uri.split("://", 1)[1])
    return _chat_sessions


def _chat_user(user: str) -> str:
    return "user" if auth.MODE == "off" else user        # the override maps "user" to DEV_USER_ID locally


@router.get("/api/chats")
async def my_chats(request: Request):
    """Your own chats, newest first - to pick one to turn into a case."""
    user = await _admin(request)
    resp = await _chat_session_service().list_sessions(app_name=APP_NAME, user_id=_chat_user(user))
    sessions = sorted(resp.sessions, key=lambda s: s.last_update_time or 0, reverse=True)[:100]
    out = []
    for s in sessions:
        title = (s.state or {}).get("chat_title") or ""          # set by the chat page
        out.append({"id": s.id, "title": title, "updated": s.last_update_time})
    return out


@router.post("/api/chats/{session_id}/case")
async def chat_to_case(session_id: str, request: Request):
    """Preview (save=false) or save an eval case made from one of your chats."""
    user = await _admin(request)
    body = await request.json()
    session = await _chat_session_service().get_session(app_name=APP_NAME, user_id=_chat_user(user),
                                                        session_id=session_id)
    if not session:
        raise HTTPException(404, "That chat wasn't found (it may have been deleted).")
    eval_id = _check_id(body.get("eval_id") or "case_" + session_id[:8], "The case ID")
    run_as = (body.get("user_id") or "").strip() or (services.DEV_USER_ID if auth.MODE == "off" else user)
    case = _session_to_case(session, eval_id, run_as)
    if not body.get("save"):
        first = _text(case["conversation"][0]["user_content"])
        return {"case": case, "title": (session.state or {}).get("chat_title") or first[:60]}
    eval_set_id = body.get("eval_set_id") or ""
    await _set_or_404(eval_set_id)
    exists = await _q("select 1 from eval_cases where app_name=:a and eval_set_id=:s and eval_id=:e",
                      a=APP_NAME, s=eval_set_id, e=eval_id)
    if exists and not body.get("replace"):
        raise HTTPException(409, f"There's already a case called {eval_id} in this set.")
    if isinstance(body.get("case"), dict):              # edited in the dialog before saving
        edited = dict(body["case"])
        edited["eval_id"] = eval_id
        case = _validate_case(edited)
    await _upsert_case(eval_set_id, case)
    return {"saved": True, "case": case}


# ------------------------------------------------------------------ API: runs
@router.post("/api/sets/{eval_set_id}/runs")
async def start_run(eval_set_id: str, request: Request):
    user = await _admin(request)
    s = await _set_or_404(eval_set_id)
    body = await request.json()
    criteria = _clean_criteria(body.get("criteria") or s["criteria"] or DEFAULT_CRITERIA)
    wanted = body.get("eval_ids") or None
    rows = await _q("select eval_id, data from eval_cases where app_name=:a and eval_set_id=:s "
                    "order by position, created_at", a=APP_NAME, s=eval_set_id)
    cases = [r["data"] for r in rows if not wanted or r["eval_id"] in wanted]
    if not cases:
        raise HTTPException(400, "There are no cases to run in this set yet.")
    _adk()                                            # clear message if the eval extra is missing
    if _run_lock.locked():
        raise HTTPException(409, "Another run is still going - wait for it to finish.")
    await _run_lock.acquire()
    try:
        root, _ = _load_agent()
        await _q("update eval_sets set criteria=cast(:c as jsonb), updated_at=now() "
                 "where app_name=:a and eval_set_id=:s", c=_j(criteria), a=APP_NAME, s=eval_set_id)
        run = (await _q("""insert into eval_runs (app_name, eval_set_id, label, model, criteria, total, started_by, phase)
                           values (:a, :s, :l, :m, cast(:c as jsonb), :t, :u, 'running') returning run_id""",
                        a=APP_NAME, s=eval_set_id, l=(body.get("label") or "").strip()[:80] or None,
                        m=_model_name(root), c=_j(criteria), t=len(cases), u=user))[0]
    except BaseException:
        _run_lock.release()
        raise
    run_id = run["run_id"]

    async def go():
        try:
            await _execute_run(run_id, eval_set_id, cases, criteria)
        finally:
            _run_lock.release()

    task = asyncio.create_task(go())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return {"run_id": run_id}


@router.get("/api/runs/{run_id}")
async def get_run(run_id: int, request: Request):
    """A run with its results. A case that passed in the set's previous finished run and
    doesn't now is marked regressed."""
    await _admin(request)
    rows = await _q("select * from eval_runs where run_id=:r and app_name=:a", r=run_id, a=APP_NAME)
    if not rows:
        raise HTTPException(404, "Run not found.")
    run = _run_row(rows[0])
    results = await _q("select eval_id, status, data from eval_results where run_id=:r", r=run_id)
    prev = await _q("""select run_id, label, started_at from eval_runs where app_name=:a and eval_set_id=:s
                       and run_id < :r and status='done' order by run_id desc limit 1""",
                    a=APP_NAME, s=run["eval_set_id"], r=run_id)
    before = {}
    if prev:
        for p in await _q("select eval_id, status, data from eval_results where run_id=:r", r=prev[0]["run_id"]):
            before[p["eval_id"]] = {"status": p["status"],
                                    "metrics": {m["name"]: m.get("score") for m in p["data"].get("metrics", [])}}
    out = []
    for r in results:
        was = before.get(r["eval_id"])
        out.append({"eval_id": r["eval_id"], "status": r["status"], **r["data"],
                    "previous": was, "regressed": bool(was and was["status"] == "passed"
                                                       and r["status"] != "passed")})
    case_ids = await _q("select eval_id from eval_cases where app_name=:a and eval_set_id=:s "
                        "order by position, created_at", a=APP_NAME, s=run["eval_set_id"])
    order = {c["eval_id"]: i for i, c in enumerate(case_ids)}
    out.sort(key=lambda x: (order.get(x["eval_id"], 1e9), x["eval_id"]))
    run["results"] = out
    run["previous"] = ({"run_id": prev[0]["run_id"], "label": prev[0]["label"],
                        "started_at": _iso(prev[0]["started_at"])} if prev else None)
    if prev:
        p = (await _q("select passed, total, tokens from eval_runs where run_id=:r", r=prev[0]["run_id"]))[0]
        run["previous"].update(passed=p["passed"], total=p["total"], tokens=p["tokens"],
                               metrics=_avg_metrics(before))
    run["averages"] = _avg_metrics({r["eval_id"]: {"metrics": {m["name"]: m.get("score") for m in r.get("metrics", [])}}
                                    for r in out})
    return run


def _avg_metrics(results: dict) -> dict:
    sums: dict[str, list[float]] = {}
    for r in results.values():
        for name, score in (r.get("metrics") or {}).items():
            if isinstance(score, (int, float)):
                sums.setdefault(name, []).append(float(score))
    return {k: sum(v) / len(v) for k, v in sums.items()}


@router.delete("/api/runs/{run_id}")
async def delete_run(run_id: int, request: Request):
    await _admin(request)
    await _q("delete from eval_runs where run_id=:r and app_name=:a and status <> 'running'", r=run_id, a=APP_NAME)
    return {"ok": True}


# ------------------------------------------------------------------ page + install
def install(app, app_name: str):
    global APP_NAME
    APP_NAME = app_name

    @app.get("/evals", include_in_schema=False)
    async def evals_page(request: Request):
        await _admin(request)
        return FileResponse(HERE / "evals.html", headers={"Cache-Control": "no-cache"})

    app.include_router(router, include_in_schema=False)

