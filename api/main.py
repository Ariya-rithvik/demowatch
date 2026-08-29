from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, Any, Dict, List
import mimetypes
import sys
import os

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from agents.orchestrator.agent import OrchestratorAgent
from agents.orchestrator.artifacts import resolve_within_root, artifact_root, to_relative


def to_relative_path(p) -> Optional[str]:
    """Relative artifact path for a Path object, for use in URLs."""
    return to_relative(str(p))
from agents.orchestrator.real_explorer import extract_url

app = FastAPI(title="ADIP Orchestrator API", description="Autonomous Product Intelligence Platform Orchestrator")

# The landing page is served from a different origin during development, so the
# browser needs explicit permission to call this API.
_allowed = os.getenv("ADIP_CORS_ORIGINS", "*")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if _allowed == "*" else [o.strip() for o in _allowed.split(",")],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

_static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
_dashboard_path = os.path.join(_static_dir, "dashboard.html")
# The landing page is the product surface judges see; the dashboard is the older
# internal view kept at /dashboard.
_landing_path = os.path.join(_static_dir, "index.html")


def _read_static(path: str, fallback: str) -> str:
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    return fallback


def get_landing_html():
    return _read_static(_landing_path, "<h1>ADIP</h1><p>Landing page not built.</p>")


def get_dashboard_html():
    return _read_static(_dashboard_path, "<h1>ADIP Dashboard Not Found</h1>")


orchestrator = OrchestratorAgent()

# Agents the UI can show progress for, in execution order.
_AGENT_LABELS = {
    "explorer": "Exploring your product",
    "knowledge_graph": "Mapping structure",
    "documentation": "Writing documentation",
    "qa": "Running tests",
    "demo": "Producing the demo",
    "release": "Summarising the release",
}


class WorkflowRequest(BaseModel):
    goal: str
    user_id: str = "anonymous"
    priority: int = 1
    target_url: Optional[str] = None


def _build_progress(status: Dict[str, Any]) -> Dict[str, Any]:
    """Derive UI-facing progress from the workflow's task records."""
    tasks: List[Dict[str, Any]] = status.get("tasks") or []
    # Prefer the declared plan so every step is visible immediately; tasks are
    # only created as they are dispatched, which would make the list grow.
    plan: List[str] = list(status.get("plan") or [])
    for t in tasks:
        name = t.get("agent_name")
        if name and name not in plan:
            plan.append(name)

    by_agent = {t.get("agent_name"): t for t in tasks}
    steps = []
    for name in plan:
        t = by_agent.get(name, {})
        steps.append({
            "agent": name,
            "label": _AGENT_LABELS.get(name, name.replace("_", " ").title()),
            "status": t.get("status", "PENDING"),
            "error": t.get("error_msg") if t.get("error_msg") not in (None, "None") else None,
        })

    done = sum(1 for s in steps if s["status"] == "COMPLETED")
    wf_status = status.get("status", "PENDING")
    percent = 100 if wf_status == "COMPLETED" else (int(done / len(steps) * 100) if steps else 0)

    # Surface the first real failure so the client never sees a silent success.
    error = next((s["error"] for s in steps if s["status"] == "FAILED" and s["error"]), None)
    if not error and wf_status == "FAILED":
        error = "Workflow failed."

    return {"percent": percent, "steps": steps, "completed_steps": done, "total_steps": len(steps), "error": error}


def _build_result(status: Dict[str, Any]) -> Dict[str, Any]:
    """Extract the client-facing artifacts from the accumulated workflow context."""
    ctx = status.get("context") or {}
    explorer = ctx.get("explorer_output") or {}
    demo = ctx.get("demo_output") or {}
    qa = ctx.get("qa_output") or {}
    docs = ctx.get("documentation_output") or {}
    graph = ctx.get("knowledge_graph_output") or {}
    release = ctx.get("release_output") or {}

    shots = [
        {"artifact_path": s.get("artifact_path"), "url": s.get("url")}
        for s in (explorer.get("screenshots") or [])
        if s.get("artifact_path")
    ]

    return {
        "target_url": explorer.get("target_url"),
        "entity": explorer.get("entity"),
        "explored_live": explorer.get("source") == "real-explorer",
        "summary": explorer.get("summary") or {},
        "fact_count": explorer.get("fact_count") or len(explorer.get("facts") or []),
        "key_elements": (explorer.get("key_elements") or [])[:12],
        "workflows": explorer.get("workflows") or [],
        "screenshots": shots,
        "demo": {
            "artifact_path": demo.get("artifact_path"),
            "generated_by": demo.get("generated_by"),
            "line_count": demo.get("line_count"),
            "scenes": demo.get("scenes") or [],
            "scenes_dropped": demo.get("scenes_dropped", 0),
            "media_mode": demo.get("media_mode"),
            "manifest": demo.get("manifest"),
            "storage": demo.get("storage"),
        } if demo else None,
        # Verification is the headline result, so it is surfaced even when it fails.
        "verification": {
            "status": qa.get("status"),
            "total_claims": qa.get("total_claims"),
            "verified": qa.get("verified"),
            "failed": qa.get("failed"),
            "pass_rate": qa.get("pass_rate"),
            "artifact_path": qa.get("artifact_path"),
            "checks": (qa.get("checks") or [])[:20],
        } if qa else None,
        "documentation": {
            "artifact_path": docs.get("artifact_path"),
            "word_count": docs.get("word_count"),
            "facts_cited": docs.get("facts_cited"),
        } if docs else None,
        "graph": {
            "artifact_path": graph.get("artifact_path"),
            "metrics": graph.get("metrics"),
            "summary": graph.get("summary"),
        } if graph else None,
        "release": {
            "artifact_path": release.get("artifact_path"),
            "is_baseline": release.get("is_baseline"),
            "has_drift": release.get("has_drift"),
            "summary": release.get("summary"),
            "agents_to_rerun": release.get("agents_to_rerun") or [],
            "stale_artifacts": release.get("stale_artifacts") or [],
        } if release else None,
    }


@app.get("/")
def read_root():
    """Service descriptor, or the UI when running as a single process.

    In the deployed topology the UI is its own service, so this returns what the API
    is rather than a placeholder page pretending to be the product.
    """
    if os.path.exists(_landing_path):
        return HTMLResponse(content=get_landing_html(), status_code=200)
    return {
        "service": "veridemo-api",
        "description": "Verified demo pipeline. The UI is served by the web service.",
        "endpoints": ["/health", "/showcase", "/workflows", "/artifacts/{path}"],
    }


@app.get("/dashboard", response_class=HTMLResponse)
def read_dashboard():
    """Internal operations view."""
    return HTMLResponse(content=get_dashboard_html(), status_code=200)


@app.get("/showcase")
def showcase():
    """Completed packages available for immediate inspection.

    A live crawl takes a couple of minutes, which is a poor first impression for
    someone evaluating the app. This reads finished packages off the artifact
    directory rather than in-memory workflow state, so pre-generated examples survive
    restarts and redeploys.
    """
    import json as _json

    root = artifact_root()
    items = []
    if root.is_dir():
        for wf_dir in root.iterdir():
            if not wf_dir.is_dir():
                continue
            demo_dir = wf_dir / "demo"
            video = demo_dir / "demo.mp4"
            storyboard = demo_dir / "storyboard.html"
            if not (video.is_file() or storyboard.is_file()):
                continue

            def _load(p):
                try:
                    return _json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None
                except (OSError, ValueError):
                    return None

            summary = _load(wf_dir / "exploration" / "exploration_summary.json") or {}
            qa = _load(wf_dir / "qa" / "verification.json") or {}
            scenes = _load(demo_dir / "scenes.json") or []

            items.append({
                "workflow_id": wf_dir.name,
                "target_url": summary.get("target_url"),
                "entity": summary.get("entity"),
                "fact_count": summary.get("fact_count"),
                "video_artifact_path": to_relative_path(video) if video.is_file() else None,
                "storyboard_artifact_path": to_relative_path(storyboard) if storyboard.is_file() else None,
                "scenes": len(scenes),
                "verification_status": qa.get("status"),
                "pass_rate": qa.get("pass_rate"),
                "mtime": video.stat().st_mtime if video.is_file() else storyboard.stat().st_mtime,
            })

    items.sort(key=lambda i: i["mtime"], reverse=True)
    for i in items:
        i.pop("mtime", None)
    return {"count": len(items), "packages": items[:12]}


@app.get("/health")
def health():
    """Liveness probe, capability flags, and the state of each backing service.

    Doubles as the readiness check: a container that cannot reach PostgreSQL should
    not take traffic, so `status` degrades when the database is unreachable.
    """
    from agents.orchestrator.real_explorer import explorer_available
    from agents.orchestrator.media_store import MediaStore
    from agents.orchestrator.video_render import ffmpeg_available, tts_available

    db_ok = orchestrator.db.healthy()
    store = MediaStore("health")

    return JSONResponse(
        status_code=200 if db_ok else 503,
        content={
            "status": "ok" if db_ok else "degraded",
            "services": {
                "database": {"backend": orchestrator.db.backend, "reachable": db_ok},
                "storage": {"backend": store.backend, "bucket": store.describe().get("bucket")},
            },
            "capabilities": {
                "live_exploration": explorer_available(),
                "video_render": ffmpeg_available() and tts_available(),
                "llm_configured": bool(os.environ.get("GEMINI_API_KEY")),
            },
            "artifact_root": str(artifact_root()),
        },
    )


@app.post("/workflows")
def submit_workflow(req: WorkflowRequest):
    goal = req.goal.strip()
    if not goal:
        raise HTTPException(status_code=400, detail="goal must not be empty")

    # Fold an explicit target_url into the goal rather than mutating workflow
    # context after submission: the scheduler may dispatch the first task within
    # milliseconds, so a post-enqueue write would race the Explorer reading it.
    resolved = req.target_url or extract_url(goal)
    if req.target_url and req.target_url not in goal:
        goal = f"{goal} {req.target_url}".strip()

    wf_id = orchestrator.submit_workflow({"goal": goal}, user_id=req.user_id, priority=req.priority)
    return {"workflow_id": wf_id, "status": "SUBMITTED", "target_url": resolved}


@app.get("/workflows/{workflow_id}")
def get_workflow(workflow_id: str):
    status = orchestrator.get_workflow_status(workflow_id)
    if not status:
        raise HTTPException(status_code=404, detail="Workflow not found")

    status["progress"] = _build_progress(status)
    status["result"] = _build_result(status)
    return status


@app.get("/artifacts/{artifact_path:path}")
def get_artifact(artifact_path: str):
    """Serve a generated artifact, refusing anything outside the artifact root."""
    resolved = resolve_within_root(artifact_path)
    if not resolved:
        raise HTTPException(status_code=404, detail="Artifact not found")

    media_type, _ = mimetypes.guess_type(resolved.name)
    return FileResponse(resolved, media_type=media_type or "application/octet-stream")


@app.get("/workflows/{workflow_id}/demo_html", response_class=HTMLResponse)
def get_demo_html(workflow_id: str):
    """Serve the Demo Agent's generated showcase for embedding in an iframe."""
    status = orchestrator.get_workflow_status(workflow_id)
    if not status or "context" not in status:
        raise HTTPException(status_code=404, detail="Workflow context not found")

    demo_out = (status.get("context") or {}).get("demo_output", {}) or {}
    resolved = resolve_within_root(demo_out.get("artifact_path") or "")
    if resolved:
        return HTMLResponse(content=resolved.read_text(encoding="utf-8"), status_code=200)

    raise HTTPException(status_code=404, detail="Demo artifact not available yet")


@app.get("/system/status")
def system_status():
    return orchestrator.get_system_status()


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "3000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
