"""Documentation agent: writes a user guide for the product that was actually crawled.

The agent it replaces produced market-research boilerplate - SWOT sections, competitive
landscape, "quarterly executive intelligence briefings" - which is the wrong genre
entirely for a product demo pipeline, and none of it was grounded in the crawl.

Every section here is built from crawl data and records which truth-set facts it used,
so QA can verify it and the drift watcher can tell which sections went stale.
"""

from __future__ import annotations

from typing import Any, Dict, List

from .artifacts import workflow_dir, to_relative
from .media_store import DependencyRegistry
from .truth_set import load_truth_set


class DocumentationAgent:
    """Generates a grounded, citation-tracked user guide."""

    def __init__(self, llm: Any = None):
        self.name = "Documentation"
        self.llm = llm

    def _facts_of_kind(self, facts: List[Dict[str, Any]], *kinds: str) -> List[Dict[str, Any]]:
        return [f for f in facts if f.get("kind") in kinds]

    def _overview(self, entity: str, facts: List[Dict[str, Any]]) -> tuple[str, List[str], str]:
        """Lead section: what the product says about itself, in its own words."""
        headings = self._facts_of_kind(facts, "heading")[:6]
        prices = self._facts_of_kind(facts, "price")[:4]

        lines = [f"## What {entity} offers", ""]
        if headings:
            lines.append("The product presents these capabilities:")
            lines.append("")
            lines += [f"- {h['text']}" for h in headings]
            lines.append("")
        if prices:
            lines.append("Pricing shown on the site:")
            lines.append("")
            lines += [f"- {p['text']}" for p in prices]
            lines.append("")

        cited = [f["id"] for f in headings + prices]
        claim = "; ".join(f["text"] for f in (headings + prices)[:6])
        if not cited:
            lines.append("_No headline or pricing content was captured during the crawl._")
            lines.append("")
        return "\n".join(lines), cited, claim

    def _pages_section(self, explorer: Dict[str, Any], facts: List[Dict[str, Any]]) -> tuple[str, List[str]]:
        pages = explorer.get("pages") or []
        shots = {s.get("url"): s.get("artifact_path") for s in (explorer.get("screenshots") or [])}
        by_url: Dict[str, List[Dict[str, Any]]] = {}
        for f in facts:
            by_url.setdefault(f.get("url"), []).append(f)

        lines = ["## Pages", ""]
        cited: List[str] = []
        if not pages:
            lines += ["_No pages were recorded._", ""]
            return "\n".join(lines), cited

        for p in pages[:12]:
            url = p.get("url") or ""
            lines.append(f"### {p.get('title') or url}")
            lines.append("")
            lines.append(f"`{url}`")
            lines.append("")
            shot = shots.get(url)
            if shot:
                lines.append(f"![Screenshot of {p.get('title') or url}](/artifacts/{shot})")
                lines.append("")
            page_facts = by_url.get(url, [])[:5]
            if page_facts:
                lines.append("Visible on this page:")
                lines.append("")
                lines += [f"- {f['text']}" for f in page_facts]
                lines.append("")
                cited += [f["id"] for f in page_facts]
        return "\n".join(lines), cited

    def _actions_section(self, explorer: Dict[str, Any], facts: List[Dict[str, Any]]) -> tuple[str, List[str]]:
        ctas = self._facts_of_kind(facts, "cta")[:12]
        lines = ["## Key actions", ""]
        if not ctas:
            lines += ["_No interactive controls were captured._", ""]
            return "\n".join(lines), []
        lines.append("| Action | Selector |")
        lines.append("| --- | --- |")
        for c in ctas:
            label = c["text"].replace("|", "\\|")
            lines.append(f"| {label} | `{c['selector']}` |")
        lines.append("")
        return "\n".join(lines), [c["id"] for c in ctas]

    def _forms_section(self, explorer: Dict[str, Any]) -> str:
        forms = explorer.get("forms") or []
        lines = ["## Forms", ""]
        if not forms:
            lines += ["_No forms were detected._", ""]
            return "\n".join(lines)
        for i, f in enumerate(forms[:8], 1):
            lines.append(f"### Form {i} — `{f.get('method') or 'GET'}`")
            lines.append("")
            if f.get("action_url"):
                lines.append(f"Submits to `{f['action_url']}`")
                lines.append("")
            fields = f.get("fields") or []
            if fields:
                lines.append("| Field | Type |")
                lines.append("| --- | --- |")
                for fl in fields[:10]:
                    lines.append(f"| {fl.get('name') or '(unnamed)'} | {fl.get('type') or 'text'} |")
                lines.append("")
        return "\n".join(lines)

    def _workflows_section(self, explorer: Dict[str, Any]) -> str:
        flows = explorer.get("workflows") or []
        lines = ["## User workflows", ""]
        if not flows:
            lines += ["_No multi-step workflows were detected._", ""]
            return "\n".join(lines)
        for w in flows[:8]:
            steps = w.get("step_count") or 0
            lines.append(f"- **{w.get('name') or 'Workflow'}** — {steps} step(s): "
                         f"`{w.get('start_url')}` → `{w.get('end_url')}`")
        lines.append("")
        return "\n".join(lines)

    def run(self, context: Dict[str, Any]) -> Dict[str, Any]:
        workflow_id = context.get("workflow_id", "adhoc")
        explorer = context.get("explorer_output") or {}
        facts = load_truth_set(explorer)
        entity = explorer.get("entity") or "This product"
        target_url = explorer.get("target_url") or ""
        summary = explorer.get("summary") or {}

        print(f"  [{self.name}] Writing guide from {len(facts)} facts...")

        overview_md, overview_cites, overview_claim = self._overview(entity, facts)
        pages_md, page_cites = self._pages_section(explorer, facts)
        actions_md, action_cites = self._actions_section(explorer, facts)

        parts = [
            f"# {entity} — User Guide",
            "",
            f"Generated from a live crawl of `{target_url}`. "
            f"{summary.get('total_pages_discovered', 0)} page(s), "
            f"{summary.get('total_elements_extracted', 0)} element(s) examined.",
            "",
            overview_md,
            pages_md,
            actions_md,
            self._forms_section(explorer),
            self._workflows_section(explorer),
            "## Facts referenced",
            "",
            "Each statement above traces to an element captured during the crawl.",
            "",
        ]
        cited_all = sorted(set(overview_cites + page_cites + action_cites))
        by_id = {f["id"]: f for f in facts}
        for fid in cited_all[:60]:
            f = by_id.get(fid)
            if f:
                parts.append(f"- `{fid}` — {f['kind']}: \"{f['text'][:90]}\" (`{f['selector']}`)")
        parts.append("")

        markdown = "\n".join(parts)

        # section_deps is what QA verifies and what the drift watcher resolves against.
        # Sections with no facts are omitted entirely: they render as "none captured"
        # and assert nothing, so declaring them would make QA report an uncited claim
        # for a section that never made one.
        section_deps = [
            dep for dep in (
                {"section": f"What {entity} offers", "fact_ids": overview_cites, "claim": overview_claim},
                {"section": "Pages", "fact_ids": page_cites},
                {"section": "Key actions", "fact_ids": action_cites},
            ) if dep["fact_ids"]
        ]

        registry = DependencyRegistry.load(workflow_id)
        for dep in section_deps:
            if dep["fact_ids"]:
                registry.declare(f"guide.md#{dep['section']}", "documentation", dep["fact_ids"])
        registry.save()

        out_dir = workflow_dir(workflow_id, "docs")
        path = out_dir / "guide.md"
        path.write_text(markdown, encoding="utf-8")

        print(f"  [{self.name}] Guide written: {len(markdown.split())} words, "
              f"{len(cited_all)} facts cited.")

        return {"documentation_output": {
            "markdown": markdown,
            "artifact_path": to_relative(str(path)),
            "section_deps": section_deps,
            "facts_cited": len(cited_all),
            "word_count": len(markdown.split()),
            "generated_by": "deterministic",
        }}


if __name__ == "__main__":  # pragma: no cover
    ok = fail = 0

    def check(name, cond, detail=""):
        global ok, fail
        if cond:
            ok += 1; print(f"  PASS  {name}")
        else:
            fail += 1; print(f"  FAIL  {name}  {detail}")

    facts = [
        {"id": "f1", "url": "https://x.test/", "selector": "h1", "tag": "h1", "kind": "heading",
         "text": "Simple pricing", "sha256": "a"},
        {"id": "f2", "url": "https://x.test/", "selector": ".p", "tag": "span", "kind": "price",
         "text": "$99/month", "sha256": "b"},
        {"id": "f3", "url": "https://x.test/", "selector": "button", "tag": "button", "kind": "cta",
         "text": "Get Started", "sha256": "c"},
    ]
    ctx = {"workflow_id": "wf-doc-selftest", "explorer_output": {
        "entity": "X Test", "target_url": "https://x.test", "facts": facts,
        "summary": {"total_pages_discovered": 1, "total_elements_extracted": 3},
        "pages": [{"url": "https://x.test/", "title": "Home"}],
        "screenshots": [], "forms": [{"method": "POST", "action_url": "/signup",
                                      "fields": [{"name": "email", "type": "email"}]}],
        "workflows": [{"name": "Signup", "start_url": "/", "end_url": "/done", "step_count": 2}],
    }}

    out = DocumentationAgent().run(ctx)["documentation_output"]
    md = out["markdown"]
    check("guide produced", len(md) > 200)
    check("real heading used", "Simple pricing" in md)
    check("real price used", "$99/month" in md)
    check("form field documented", "email" in md)
    check("workflow documented", "Signup" in md)
    check("no market-research boilerplate",
          not any(w in md for w in ("SWOT", "competitive landscape", "executive intelligence")))
    check("section_deps emitted", len(out["section_deps"]) == 3)
    check("deps cite real ids", all(set(d["fact_ids"]) <= {"f1", "f2", "f3"} for d in out["section_deps"]))
    check("artifact written", bool(out["artifact_path"]))

    reg = DependencyRegistry.load("wf-doc-selftest")
    check("registry updated", "documentation" in reg.agents_to_rerun(["f1"]))

    empty = DocumentationAgent().run({"workflow_id": "wf-doc-selftest2"})["documentation_output"]
    check("degrades with no data", empty["facts_cited"] == 0 and len(empty["markdown"]) > 0)

    print(f"\n{ok} passed, {fail} failed")
    raise SystemExit(1 if fail else 0)
