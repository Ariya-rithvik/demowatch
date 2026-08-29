"""Bridge that lets the orchestrator drive the real Playwright-based Explorer.

The orchestrator historically shipped a simplified in-process Explorer that turned a
*topic string* into a decorative animation and never opened a browser. The genuine
browser-driven explorer lives in `explorer-mcp/agent` but was only reachable from
standalone test scripts.

This module wires the two together:

  * `RealExplorerAgent` runs the real explorer when the goal contains a URL.
  * When no URL is present the caller-supplied topic agent is used instead, so
    topic-style goals ("fortnite launch demo") keep working.

A full exploration is far too large to hand to an LLM or a browser - a modest crawl
produced ~19k elements / 32 MB of JSON. `condense_exploration` reduces that to a
compact, bounded summary that downstream agents and the UI can actually consume.
"""

import json
import os
import re
import sys
import asyncio
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

# `agents` is a PEP 420 namespace package in both the orchestrator tree and the
# explorer tree, so putting the explorer root on sys.path merges `agents.explorer`
# alongside `agents.orchestrator` instead of shadowing it.
_EXPLORER_ROOT = Path(__file__).resolve().parents[2] / "explorer"

# Keep crawls bounded. The unbounded default (50 actions / 20 pages) took ~188s.
DEFAULT_MAX_ACTIONS = int(os.getenv("ADIP_EXPLORE_MAX_ACTIONS", "8"))
DEFAULT_MAX_PAGES = int(os.getenv("ADIP_EXPLORE_MAX_PAGES", "6"))
DEFAULT_MAX_DEPTH = int(os.getenv("ADIP_EXPLORE_MAX_DEPTH", "2"))

# Caps applied when condensing, so the summary stays LLM-safe regardless of site size.
MAX_PAGES_OUT = 12
MAX_ELEMENTS_OUT = 40
MAX_FORMS_OUT = 8
MAX_WORKFLOWS_OUT = 6
MAX_ERRORS_OUT = 10

_INTERACTIVE_TAGS = {"button", "a", "input", "select", "textarea"}


def explorer_available() -> bool:
    """True if the real explorer package is present on disk."""
    return (_EXPLORER_ROOT / "agents" / "explorer" / "agent.py").is_file()


def _ensure_importable() -> None:
    """Put the explorer package on sys.path (idempotent)."""
    root = str(_EXPLORER_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)


def extract_url(text: str) -> Optional[str]:
    """Pull the first usable target URL out of a free-form goal string.

    Accepts explicit URLs ("https://app.acme.com/dashboard") and bare hosts
    ("app.acme.com"). Returns None when the goal is a plain topic.
    """
    if not text:
        return None

    explicit = re.search(r'https?://[^\s"\'<>,)]+', text)
    if explicit:
        return explicit.group(0).rstrip(".,;")

    # Bare host: at least one dot, a plausible TLD, no spaces.
    bare = re.search(
        r'\b((?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,})(/[^\s"\'<>,)]*)?',
        text,
        re.IGNORECASE,
    )
    if bare:
        host = bare.group(0).rstrip(".,;")
        # Avoid matching things like "version 2.0" or file names.
        tld = host.split("/")[0].rsplit(".", 1)[-1]
        if tld.isalpha() and len(tld) >= 2:
            return f"https://{host}"
    return None


def _element_score(el: Dict[str, Any]) -> int:
    """Rank elements so the most demo-worthy survive truncation."""
    tag = (el.get("tag_name") or "").lower()
    text = (el.get("text") or "").strip()
    score = 0
    if tag == "button":
        score += 5
    elif tag in ("input", "select", "textarea"):
        score += 4
    elif tag == "a":
        score += 2
    if text:
        score += 3
    if el.get("is_visible"):
        score += 2
    if el.get("is_enabled"):
        score += 1
    return score


def condense_exploration(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Reduce a full ExplorationResult to a bounded, LLM-safe summary.

    A real crawl yields tens of thousands of elements and megabytes of network
    headers; none of that can be forwarded to a model or rendered in a UI.
    """
    summary = raw.get("summary", {}) or {}

    pages_out: List[Dict[str, Any]] = []
    for p in (raw.get("pages") or [])[:MAX_PAGES_OUT]:
        pages_out.append({
            "url": p.get("url"),
            "title": p.get("title"),
            "depth": p.get("depth"),
            "elements_count": len(p.get("elements") or []),
        })

    # Rank, dedupe, then cap the interactive elements.
    seen = set()
    ranked = sorted(
        (e for e in (raw.get("elements") or [])
         if (e.get("tag_name") or "").lower() in _INTERACTIVE_TAGS),
        key=_element_score,
        reverse=True,
    )
    elements_out: List[Dict[str, Any]] = []
    for el in ranked:
        text = (el.get("text") or "").strip()
        key = ((el.get("tag_name") or "").lower(), text, el.get("css_selector"))
        if key in seen:
            continue
        seen.add(key)
        elements_out.append({
            "tag": el.get("tag_name"),
            "type": el.get("element_type"),
            "text": text[:80],
            "selector": el.get("css_selector"),
        })
        if len(elements_out) >= MAX_ELEMENTS_OUT:
            break

    forms_out: List[Dict[str, Any]] = []
    for f in (raw.get("forms") or [])[:MAX_FORMS_OUT]:
        forms_out.append({
            "selector": f.get("css_selector"),
            "method": f.get("method"),
            "action_url": f.get("action_url"),
            "page_url": f.get("page_url"),
            "fields": [
                {"name": fl.get("name"), "type": fl.get("field_type")}
                for fl in (f.get("fields") or [])[:10]
            ],
        })

    workflows_out: List[Dict[str, Any]] = []
    for w in (raw.get("workflows") or [])[:MAX_WORKFLOWS_OUT]:
        workflows_out.append({
            "name": w.get("name"),
            "start_url": w.get("start_url"),
            "end_url": w.get("end_url"),
            "step_count": len(w.get("steps") or []),
            "is_completed": w.get("is_completed"),
        })

    from .artifacts import to_relative
    screenshots_out = [
        {
            "file_path": s.get("file_path"),
            # Relative form is what the API serves; None if written outside the root.
            "artifact_path": to_relative(s.get("file_path") or ""),
            "url": s.get("url"),
            "type": s.get("screenshot_type"),
        }
        for s in (raw.get("screenshots") or [])
    ]

    errors_out = [
        {"type": e.get("error_type"), "message": (e.get("message") or "")[:200], "url": e.get("url")}
        for e in (raw.get("errors") or [])[:MAX_ERRORS_OUT]
    ]

    # Network: keep aggregate shape only - full request/response headers are huge.
    by_type: Dict[str, int] = {}
    failed = 0
    for r in (raw.get("network_requests") or []):
        rt = r.get("resource_type") or "other"
        by_type[rt] = by_type.get(rt, 0) + 1
        if r.get("failed"):
            failed += 1

    nav = raw.get("navigation_graph") or {}

    return {
        "source": "real-explorer",
        "exploration_id": raw.get("exploration_id"),
        "target_url": summary.get("target_url"),
        "entity": (pages_out[0]["title"] if pages_out and pages_out[0].get("title")
                   else summary.get("target_url")),
        "summary": summary,
        "pages": pages_out,
        "key_elements": elements_out,
        "forms": forms_out,
        "workflows": workflows_out,
        "screenshots": screenshots_out,
        "errors": errors_out,
        "network": {
            "total": len(raw.get("network_requests") or []),
            "failed": failed,
            "by_resource_type": by_type,
        },
        "navigation_graph": {
            "total_nodes": nav.get("total_nodes"),
            "total_edges": nav.get("total_edges"),
            "nodes": [
                {"url": n.get("url"), "title": n.get("title"), "depth": n.get("depth")}
                for n in (nav.get("nodes") or [])[:MAX_PAGES_OUT]
            ],
        },
        # Shapes below keep downstream agents (Knowledge Graph, Demo) working, since
        # they were written against the topic-explorer's contract.
        "key_components": [e["text"] for e in elements_out if e["text"]][:8],
        "key_insights": [
            f"Crawled {summary.get('total_pages_discovered', 0)} pages from {summary.get('target_url')}",
            f"Extracted {summary.get('total_elements_extracted', 0)} UI elements "
            f"and {summary.get('total_forms_found', 0)} forms",
            f"Detected {summary.get('total_workflows_detected', 0)} user workflows",
            f"Captured {summary.get('total_screenshots_taken', 0)} screenshots "
            f"across {len(pages_out)} recorded pages",
        ],
        "metrics": {
            "data_sources_scanned": summary.get("total_network_requests", 0),
            "entities_discovered": summary.get("total_elements_extracted", 0),
            "relationships_mapped": nav.get("total_edges", 0),
            "confidence_score": "live-capture",
        },
    }


def run_exploration(target_url: str, output_dir: Path) -> Dict[str, Any]:
    """Run the real explorer synchronously and return the condensed result.

    Safe to call from a worker thread: each task thread has no running event loop,
    so `asyncio.run` can own one for the duration of the crawl.
    """
    _ensure_importable()
    from agents.explorer.config import ExplorerConfig
    from agents.explorer.agent import ExplorerAgent as _RealExplorer

    output_dir.mkdir(parents=True, exist_ok=True)
    config = ExplorerConfig(
        target_url=target_url,
        headless=True,
        max_actions=DEFAULT_MAX_ACTIONS,
        max_page_visits=DEFAULT_MAX_PAGES,
        max_depth=DEFAULT_MAX_DEPTH,
        output_dir=output_dir,
        save_screenshots=True,
    )

    agent = _RealExplorer(config)
    asyncio.run(agent.explore(target_url))

    # The publisher writes the authoritative artifact; read it back rather than
    # re-serialising the in-memory model.
    published = output_dir / "ExplorationResult.json"
    if not published.is_file():
        raise RuntimeError(f"Explorer produced no result file at {published}")

    with open(published, "r", encoding="utf-8") as fh:
        raw = json.load(fh)

    condensed = condense_exploration(raw)
    condensed["raw_result_file"] = str(published)

    # The truth set is the contract every downstream agent cites against, so it is
    # produced here once rather than re-derived (and possibly diverging) per agent.
    from .truth_set import extract_truth_set
    facts = extract_truth_set(raw)
    condensed["facts"] = facts
    condensed["fact_count"] = len(facts)

    truth_path = output_dir / "truth_set.json"
    with open(truth_path, "w", encoding="utf-8") as fh:
        json.dump(facts, fh, indent=2, ensure_ascii=False)
    condensed["truth_set_file"] = str(truth_path)

    with open(output_dir / "exploration_summary.json", "w", encoding="utf-8") as fh:
        json.dump(condensed, fh, indent=2, ensure_ascii=False)

    return condensed


class RealExplorerAgent:
    """Explorer that crawls a real URL, falling back to a topic agent otherwise."""

    def __init__(self, topic_agent: Any, artifact_root: Optional[Path] = None):
        self.name = "Explorer"
        self.topic_agent = topic_agent
        self.artifact_root = Path(
            artifact_root or os.getenv("ADIP_ARTIFACT_DIR") or (Path(tempfile.gettempdir()) / "adip_artifacts")
        )

    def run(self, context: Dict[str, Any]) -> Dict[str, Any]:
        goal = context.get("goal", "") or ""
        target_url = context.get("target_url") or extract_url(goal)

        if not target_url:
            print(f"  [{self.name}] No URL in goal - using topic exploration.")
            return self.topic_agent.run(context)

        if not explorer_available():
            print(f"  [{self.name}] Real explorer package missing - using topic exploration.")
            return self.topic_agent.run(context)

        workflow_id = context.get("workflow_id") or "adhoc"
        out_dir = self.artifact_root / str(workflow_id) / "exploration"

        print(f"  [{self.name}] Crawling real target: {target_url}")
        result = run_exploration(target_url, out_dir)
        print(
            f"  [{self.name}] Crawl complete: "
            f"{result['summary'].get('total_pages_discovered', 0)} pages, "
            f"{result['summary'].get('total_elements_extracted', 0)} elements."
        )
        return {"explorer_output": result}
