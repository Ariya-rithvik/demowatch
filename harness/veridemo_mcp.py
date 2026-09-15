"""MCP server exposing the verified-demo pipeline as tools an agent composes itself.

The pipeline this wraps runs a fixed sequence: crawl, graph, document, demo, verify,
diff. That is a script, not an agent - nothing decides anything at runtime. The tools
below deliberately break that sequence apart so the harness does the reasoning:

  * `crawl_product` observes a page and returns the facts it captured.
  * `list_facts` lets the agent look at what it is allowed to assert.
  * `check_narration` is the guard. The agent proposes a line, and this says whether
    the cited facts support it. An unsupported line comes back rejected with the
    offending numbers named, and the agent has to rewrite it and ask again.
  * `publish_demo` is the irreversible step and is annotated as destructive, so the
    harness stops and asks a person before it runs.

The loop in the middle is the point: nothing here lets an agent assert something the
crawl does not support, so the model cannot talk its way past the verifier.

Run:  python harness/veridemo_mcp.py            (http://127.0.0.1:9077/mcp)
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastmcp import FastMCP
from mcp.types import ToolAnnotations

# The pipeline package lives beside this one.
_ROOT = Path(__file__).resolve().parents[1]
_API = _ROOT / "api"
for p in (str(_API), str(_API / "explorer")):
    if p not in sys.path:
        sys.path.insert(0, p)

from agents.orchestrator.artifacts import to_relative, workflow_dir  # noqa: E402
from agents.orchestrator.real_explorer import condense_exploration, extract_url  # noqa: E402
from agents.orchestrator.truth_set import (  # noqa: E402
    extract_truth_set,
    index_by_id,
    verify_claim,
)
from agents.orchestrator.agent_ingest import ContentIngestAgent  # noqa: E402
from agents.orchestrator.registry import GeminiClient  # noqa: E402

_ingest_agent = ContentIngestAgent(llm=GeminiClient())

mcp = FastMCP("Veridemo — verified product demos")

# Crawl state, keyed by session. The agent works across several tool calls and needs
# the facts from its own crawl, not whichever crawl happened to run last.
_SESSIONS: Dict[str, Dict[str, Any]] = {}

MAX_FACTS_RETURNED = 40


def _session(session_id: str) -> Dict[str, Any]:
    return _SESSIONS.setdefault(session_id or "default", {})


def _facts(session_id: str) -> List[Dict[str, Any]]:
    return _session(session_id).get("facts") or []


def _err(message: str, **extra: Any) -> str:
    """Errors are returned as data so the agent can read and act on them."""
    return json.dumps({"ok": False, "error": message, **extra}, default=str)


# ── crawling ────────────────────────────────────────────────────────────────────

def _enforce_read_only(agent: Any) -> None:
    """Let the crawler observe a page but never act on it.

    Capping `max_actions` at zero does not work: the exploration loop is guarded by
    `has_remaining_work()`, which is false the moment the cap is reached, so the body
    never runs and the page is never read either. Extraction and action live in the
    same loop.

    So the cap stays high enough for the loop to run, and the planner is what refuses.
    `plan_next_action` returning None is the loop's own "nothing left to do" signal, so
    it reads the page, asks for an action, is told there is none, and stops. The result
    is a full extraction with provably zero interaction: not a click, not a keystroke.

    This matters because the default planner types "Test Input" into any text field it
    finds and clicks whatever it can reach. On a page you own that is how multi-step
    flows get discovered. On anyone else's it is submitting forms you do not own.
    """
    def refuse(_page: Any, _elements: Any) -> None:
        return None

    agent.planner.plan_next_action = refuse


async def _run_crawl(target_url: str, out_dir: Path, interact: bool) -> Dict[str, Any]:
    """Drive the real browser.

    This is async and awaits the crawl directly. It must not call `asyncio.run`: the
    orchestrator gets away with that because its agents run on worker threads with no
    event loop, but FastMCP invokes tool functions on the running loop, where
    `asyncio.run` raises and the crawl coroutine is dropped un-awaited. That failure
    is invisible to a direct unit test, which has no loop running - it only appears
    over the actual MCP transport.

    `interact` is off by default and that default matters: interaction means clicking
    and typing on the far end, which is fine on a page you own and not fine anywhere
    else.
    """
    from agents.explorer.agent import ExplorerAgent as _RealExplorer
    from agents.explorer.config import ExplorerConfig

    out_dir.mkdir(parents=True, exist_ok=True)
    config = ExplorerConfig(
        target_url=target_url,
        headless=True,
        # Both caps need headroom for the loop to run its body even once:
        # `has_remaining_work()` is false when the action cap is reached, and also
        # when visited pages reach the page cap - and the entry page is already
        # marked visited before the loop starts. In read-only mode the planner is
        # what stops the crawl, one page in, not these numbers.
        max_actions=8 if interact else 1,
        max_page_visits=6 if interact else 2,
        max_depth=2 if interact else 0,
        output_dir=out_dir,
        save_screenshots=True,
    )

    # ExplorerConfig is a pydantic BaseSettings reading ADIP_EXPLORER_* and .env, so
    # credentials can arrive without any caller passing them - and the explorer logs
    # into whatever page it is pointed at *before* the planner is consulted, typing
    # them into the first text and password fields it finds. On an arbitrary URL that
    # posts your credentials to a stranger. Nothing here needs authentication, so it
    # is cleared explicitly rather than left to whatever the environment holds.
    config.username = None
    config.password = None

    agent = _RealExplorer(config)
    if not interact:
        _enforce_read_only(agent)
    await agent.explore(target_url)

    published = out_dir / "ExplorationResult.json"
    if not published.is_file():
        raise RuntimeError(f"crawler produced no result file at {published}")
    with open(published, "r", encoding="utf-8") as fh:
        raw = json.load(fh)

    condensed = condense_exploration(raw)
    condensed["raw_result_file"] = str(published)
    facts = extract_truth_set(raw)
    condensed["facts"] = facts
    condensed["fact_count"] = len(facts)

    with open(out_dir / "truth_set.json", "w", encoding="utf-8") as fh:
        json.dump(facts, fh, indent=2, ensure_ascii=False)
    return condensed


@mcp.tool(
    annotations=ToolAnnotations(
        title="Crawl a product page",
        # Not read-only unconditionally: `interact` lets the crawler click and type on
        # the target. readOnlyHint is what a host uses to auto-run a tool without
        # asking, so claiming it here would hand the agent a state-changing capability
        # pre-labelled safe. The honest annotation for a tool with that mode is
        # destructive, and the harness can then gate it.
        readOnlyHint=False,
        destructiveHint=True,
        openWorldHint=True,
    )
)
async def crawl_product(url: str, session_id: str = "default", interact: bool = False) -> str:
    """Open a product in a real browser and capture the facts it states.

    Returns a summary of what was captured, including how many checkable facts are now
    available to cite. Nothing else in this server works until this has run.

    Set `interact` only for a site you own: it lets the crawler click buttons and type
    into fields to discover multi-step flows, which is a state-changing act on anyone
    else's site. It is off by default, and off means observation only.
    """
    target = extract_url(url) or url
    if not target.startswith(("http://", "https://")):
        return _err(f"'{url}' is not a usable URL", hint="pass a full http(s) URL")

    sid = session_id or "default"
    try:
        out_dir = workflow_dir(f"mcp-{sid}", "exploration")
        crawl = await _run_crawl(target, out_dir, interact)
    except Exception as exc:
        # workflow_dir is inside the try because a session id of bare dots produces a
        # path the OS rejects, and an uncaught exception here would escape a tool that
        # otherwise always answers with structured errors.
        return _err(f"crawl failed: {type(exc).__name__}: {exc}", url=target)

    # The crawl reports how many actions it executed. In read-only mode that must be
    # zero, and asserting it beats trusting the planner override stayed in place.
    executed = ((crawl.get("summary") or {}).get("total_actions_executed")) or 0
    if not interact and executed:
        return _err(
            f"read-only crawl executed {executed} action(s); refusing to use this crawl",
            url=target,
        )

    _SESSIONS[sid] = {
        "target_url": target,
        "entity": crawl.get("entity"),
        "facts": crawl.get("facts") or [],
        "crawl": crawl,
        "interacted": interact,
        "verified_scenes": [],
    }

    summary = crawl.get("summary") or {}
    kinds: Dict[str, int] = {}
    for f in crawl.get("facts") or []:
        kinds[f["kind"]] = kinds.get(f["kind"], 0) + 1

    return json.dumps({
        "ok": True,
        "session_id": sid,
        "target_url": target,
        "entity": crawl.get("entity"),
        "mode": "interactive" if interact else "read-only",
        "pages_discovered": summary.get("total_pages_discovered"),
        "elements_extracted": summary.get("total_elements_extracted"),
        "workflows_detected": summary.get("total_workflows_detected"),
        "errors_detected": summary.get("total_errors_detected"),
        "facts_captured": len(crawl.get("facts") or []),
        "facts_by_kind": kinds,
        "screenshots": len(crawl.get("screenshots") or []),
        "next": "call list_facts to see what you may cite",
    }, default=str)


# ── reading the truth set ───────────────────────────────────────────────────────

@mcp.tool(annotations=ToolAnnotations(title="List citable facts", readOnlyHint=True))
def list_facts(session_id: str = "default", kind: str = "", limit: int = 25) -> str:
    """List the facts captured from the crawl, which are the only things you may assert.

    `kind` filters to one of: heading, price, cta, feature, number. Each fact carries an
    id; you cite those ids when you propose narration.
    """
    facts = _facts(session_id)
    if not facts:
        return _err("no crawl for this session", hint="call crawl_product first")

    if kind:
        facts = [f for f in facts if f.get("kind") == kind]
        if not facts:
            return _err(f"no facts of kind '{kind}'",
                        available=sorted({f["kind"] for f in _facts(session_id)}))

    limit = max(1, min(int(limit or 25), MAX_FACTS_RETURNED))
    return json.dumps({
        "ok": True,
        "total": len(facts),
        "returned": min(limit, len(facts)),
        "facts": [
            {"id": f["id"], "kind": f["kind"], "text": f["text"][:120], "url": f.get("url")}
            for f in facts[:limit]
        ],
    }, default=str)


# ── the guard ───────────────────────────────────────────────────────────────────

@mcp.tool(annotations=ToolAnnotations(title="Check a narration line", readOnlyHint=True))
def check_narration(text: str, fact_ids: List[str], session_id: str = "default") -> str:
    """Check one line of narration against the facts you cite for it.

    This is the gate every spoken line has to pass. A line is rejected when it cites
    nothing, cites an id that was never captured, or states a number none of its cited
    facts contain. Rejected lines come back with the offending numbers named: rewrite
    the line so it only says what the source says, then check it again.

    Accepted lines are kept, and only kept lines can be published.
    """
    facts = _facts(session_id)
    if not facts:
        return _err("no crawl for this session", hint="call crawl_product first")
    if not (text or "").strip():
        return _err("empty narration")

    by_id = index_by_id(facts)
    ids = list(fact_ids or [])
    known = [by_id[i] for i in ids if i in by_id]
    unknown = [i for i in ids if i not in by_id]

    result = verify_claim(text, known)
    accepted = bool(result["verified"]) and not unknown

    if accepted:
        _session(session_id)["verified_scenes"].append(
            {"narration": text.strip(), "asserts": ids}
        )

    return json.dumps({
        "ok": True,
        "accepted": accepted,
        "narration": text.strip(),
        "cited_ids": ids,
        "unknown_ids": unknown,
        "unsupported_numbers": result["unsupported_numbers"],
        "reason": (
            f"cites fact ids that were never captured: {', '.join(unknown)}" if unknown
            else result["reason"]
        ),
        "verified_so_far": len(_session(session_id)["verified_scenes"]),
        "next": ("this line is accepted" if accepted
                 else "rewrite so the line only states what the cited facts say, then check again"),
    }, default=str)


@mcp.tool(annotations=ToolAnnotations(title="Review the verified script", readOnlyHint=True))
def review_script(session_id: str = "default") -> str:
    """Show the lines accepted so far, which is exactly what publishing would ship."""
    sess = _session(session_id)
    scenes = sess.get("verified_scenes") or []
    return json.dumps({
        "ok": True,
        "entity": sess.get("entity"),
        "target_url": sess.get("target_url"),
        "verified_scenes": len(scenes),
        "script": [{"index": i + 1, **s} for i, s in enumerate(scenes)],
    }, default=str)


# ── Veridemo Watch: audit content someone already wrote ─────────────────────────

@mcp.tool(annotations=ToolAnnotations(title="Audit existing content against a crawl", readOnlyHint=True))
def audit_content(content: str, session_id: str = "default", auto_accept: bool = True) -> str:
    """Check a creator's existing script/post/transcript against the crawl's facts.

    Unlike check_narration (one proposed line at a time), this takes a whole piece of
    already-written content and does the segmentation and fact-matching itself in one
    pass -- Gemini when VERIDEMO's GEMINI_API_KEY is set, a deterministic keyword/number
    overlap matcher otherwise. Either way every extracted claim still goes through the
    same numeric-support guard check_narration uses, so a bad match fails verification
    instead of shipping.

    With auto_accept (default), verified claims are added to this session's script, same
    as an accepted check_narration line -- so review_script/publish_demo see them too.
    """
    facts = _facts(session_id)
    if not facts:
        return _err("no crawl for this session", hint="call crawl_product first")
    if not (content or "").strip():
        return _err("empty content")

    result = _ingest_agent.ingest(content, facts)

    if auto_accept:
        sess = _session(session_id)
        for c in result["claims"]:
            if c["verified"]:
                sess["verified_scenes"].append({"narration": c["text"], "asserts": c["cited_ids"]})

    return json.dumps({
        "ok": True,
        "generated_by": result["generated_by"],
        "total_claims": result["total_claims"],
        "verified": result["verified"],
        "failed": result["failed"],
        "claims": result["claims"],
        "added_to_script": result["verified"] if auto_accept else 0,
    }, default=str)


# ── the irreversible step ───────────────────────────────────────────────────────

@mcp.tool(
    annotations=ToolAnnotations(
        title="Publish the demo",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
    )
)
def publish_demo(session_id: str = "default", title: str = "") -> str:
    """Publish the verified script as a demo package. This is the irreversible step.

    Marked destructive so the harness holds it for human approval: publishing puts a
    claim about someone's product in front of an audience, and that cannot be recalled.
    Only lines already accepted by check_narration are included.
    """
    sess = _session(session_id)
    scenes = sess.get("verified_scenes") or []
    if not sess.get("facts"):
        return _err("no crawl for this session", hint="call crawl_product first")
    if not scenes:
        return _err("nothing has passed verification, so there is nothing to publish",
                    hint="propose narration through check_narration first")

    # The title ships in the package and is usually its most prominent line, so it is
    # a claim like any other. Free text here was a hole straight through the guard:
    # the narration was checked and the title was not. Only the crawled entity name
    # is allowed, and an agent-supplied title has to match it.
    entity = (sess.get("entity") or "Product demo").strip()
    requested = (title or "").strip()
    if requested and requested.lower() not in entity.lower():
        return _err(
            "title must come from the crawled page, not from the agent",
            requested=requested, allowed=entity,
        )

    out_dir = workflow_dir(f"mcp-{session_id or 'default'}", "demo")
    package = {
        "title": requested or entity,
        "target_url": sess.get("target_url"),
        "crawl_mode": "interactive" if sess.get("interacted") else "read-only",
        "facts_available": len(sess.get("facts") or []),
        "scenes": [{"index": i + 1, **s} for i, s in enumerate(scenes)],
    }
    path = out_dir / "verified_demo.json"
    path.write_text(json.dumps(package, indent=2, ensure_ascii=False), encoding="utf-8")

    return json.dumps({
        "ok": True,
        "published": True,
        "scenes_published": len(scenes),
        "artifact_path": to_relative(str(path)),
        "every_line_cited": True,
    }, default=str)


if __name__ == "__main__":
    port = int(os.getenv("VERIDEMO_MCP_PORT", "9077"))
    host = os.getenv("VERIDEMO_MCP_HOST", "127.0.0.1")
    print(f"Veridemo MCP server on http://{host}:{port}/mcp", file=sys.stderr)
    mcp.run(transport="http", host=host, port=port)
