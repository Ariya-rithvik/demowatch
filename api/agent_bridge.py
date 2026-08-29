"""Single entry point the MCP servers use to invoke the real agents.

The MCP tool modules used to return hardcoded JSON with a `# TODO: call the real
business logic` comment - qa_run_tests, for instance, always claimed 24 passed and 0
failed regardless of input. That is the same fabrication the pipeline agents were
rewritten to remove, so the tools now delegate here instead of inventing answers.

There is exactly one implementation of each agent (in the orchestrator package) and MCP
is a transport in front of it, not a second copy.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, Optional

# The orchestrator package lives beside this one; MCP servers run from their own
# directories, so make the import explicit rather than relying on cwd.
_ORCHESTRATOR = Path(__file__).resolve().parent
if str(_ORCHESTRATOR) not in sys.path:
    sys.path.insert(0, str(_ORCHESTRATOR))

_AGENT_KEYS = ("explorer", "knowledge_graph", "documentation", "qa", "demo", "release")


def _registry():
    from agents.orchestrator.registry import AgentRegistry  # noqa: WPS433
    return AgentRegistry()


def available() -> bool:
    """True if the real agents can actually be imported from here."""
    try:
        _registry()
        return True
    except Exception:
        return False


def run_agent(agent_name: str, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Invoke one real agent and return its output.

    Errors are returned as structured failures rather than swallowed, so a caller can
    never mistake "the agent could not run" for "the agent found nothing wrong".
    """
    name = (agent_name or "").lower().strip()
    if name not in _AGENT_KEYS:
        return {"ok": False, "error": f"unknown agent '{agent_name}'", "known": list(_AGENT_KEYS)}

    try:
        agent = _registry().get(name)
    except Exception as exc:
        return {"ok": False, "error": f"agent registry unavailable: {exc}"}

    if agent is None:
        return {"ok": False, "error": f"agent '{name}' is not registered"}

    ctx: Dict[str, Any] = dict(context or {})
    ctx.setdefault("workflow_id", "mcp-adhoc")
    ctx.setdefault("goal", "")

    try:
        return {"ok": True, "agent": name, "output": agent.run(ctx)}
    except Exception as exc:
        return {"ok": False, "agent": name, "error": f"{type(exc).__name__}: {exc}"}


def run_pipeline(goal: str, workflow_id: str = "mcp-pipeline") -> Dict[str, Any]:
    """Run the full ordered pipeline, threading each agent's output into the next."""
    from agents.orchestrator.planner import WorkflowPlanner
    from agents.orchestrator.router import RequestRouter

    plan = WorkflowPlanner().create_plan(RequestRouter().route_request(goal))
    ctx: Dict[str, Any] = {"goal": goal, "workflow_id": workflow_id}
    steps = []

    for step in plan:
        result = run_agent(step, ctx)
        steps.append({"agent": step, "ok": result["ok"],
                      "error": result.get("error")})
        if not result["ok"]:
            return {"ok": False, "plan": plan, "steps": steps, "context": ctx}
        ctx.update(result["output"] or {})

    return {"ok": True, "plan": plan, "steps": steps, "context": ctx}
