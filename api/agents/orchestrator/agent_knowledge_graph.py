"""Knowledge graph agent: builds the graph from the crawl, not from the model.

The agent it replaces sent a text summary to an LLM and asked it to invent nodes and
edges. That produced a plausible-looking graph with no relationship to the site. The
crawler already emits a real navigation graph, real forms and real workflows - this
assembles those into a structure, and uses the LLM only to phrase a summary sentence.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from .artifacts import workflow_dir, to_relative
from .truth_set import load_truth_set

MAX_FACT_NODES = 30


class KnowledgeGraphAgent:
    """Assembles pages, forms, workflows and salient facts into one graph."""

    def __init__(self, llm: Any = None):
        self.name = "Knowledge_Graph"
        self.llm = llm

    def _raw_nav_graph(self, explorer: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Prefer the full crawl on disk: it has real edges, the condensed form does not."""
        path = explorer.get("raw_result_file")
        if not path or not os.path.isfile(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return (json.load(fh) or {}).get("navigation_graph")
        except (OSError, ValueError):
            return None

    def run(self, context: Dict[str, Any]) -> Dict[str, Any]:
        workflow_id = context.get("workflow_id", "adhoc")
        explorer = context.get("explorer_output") or {}
        facts = load_truth_set(explorer)

        nodes: List[Dict[str, Any]] = []
        edges: List[Dict[str, Any]] = []
        seen = set()

        def add_node(nid: str, label: str, ntype: str, **extra: Any) -> Optional[str]:
            if not nid or nid in seen:
                return nid if nid else None
            seen.add(nid)
            nodes.append({"id": nid, "label": (label or nid)[:120], "type": ntype, **extra})
            return nid

        raw_nav = self._raw_nav_graph(explorer)
        condensed_nav = explorer.get("navigation_graph") or {}

        # Pages
        page_ids: Dict[str, str] = {}
        nav_nodes = (raw_nav or {}).get("nodes") or condensed_nav.get("nodes") or explorer.get("pages") or []
        for n in nav_nodes:
            url = n.get("url") or ""
            if not url:
                continue
            nid = n.get("id") or f"page::{url}"
            add_node(nid, n.get("title") or url, "Page", url=url, depth=n.get("depth"))
            page_ids[url] = nid

        # Real navigation edges when the full crawl is present
        for e in ((raw_nav or {}).get("edges") or []):
            src, tgt = e.get("source") or e.get("source_page_id"), e.get("target") or e.get("target_page_id")
            if src in seen and tgt in seen:
                edges.append({"source": src, "target": tgt, "relation": "NAVIGATES_TO"})

        # Forms
        for i, f in enumerate((explorer.get("forms") or [])[:12]):
            fid = add_node(f"form::{i}", f"Form ({f.get('method') or 'GET'})", "Form",
                           selector=f.get("selector"), fields=len(f.get("fields") or []))
            parent = page_ids.get(f.get("page_url") or "")
            if parent and fid:
                edges.append({"source": parent, "target": fid, "relation": "CONTAINS"})

        # Workflows
        for i, w in enumerate((explorer.get("workflows") or [])[:10]):
            wid = add_node(f"workflow::{i}", w.get("name") or f"Workflow {i+1}", "Workflow",
                           steps=w.get("step_count"))
            s, t = page_ids.get(w.get("start_url") or ""), page_ids.get(w.get("end_url") or "")
            if wid and s:
                edges.append({"source": s, "target": wid, "relation": "STARTS"})
            if wid and t:
                edges.append({"source": wid, "target": t, "relation": "ENDS_AT"})

        # Salient facts, so the graph can answer "which page asserts this price?"
        salient = [f for f in facts if f.get("kind") in ("price", "heading")][:MAX_FACT_NODES]
        for f in salient:
            fid = add_node(f"fact::{f['id']}", f["text"], "Fact",
                           fact_id=f["id"], kind=f["kind"], selector=f.get("selector"))
            parent = page_ids.get(f.get("url") or "")
            if parent and fid:
                edges.append({"source": parent, "target": fid, "relation": "ASSERTS"})

        referenced = {e["source"] for e in edges} | {e["target"] for e in edges}
        orphans = [n["id"] for n in nodes if n["id"] not in referenced]
        depths = [n.get("depth") for n in nodes if isinstance(n.get("depth"), int)]

        metrics = {
            "total_nodes": len(nodes),
            "total_edges": len(edges),
            "pages": sum(1 for n in nodes if n["type"] == "Page"),
            "forms": sum(1 for n in nodes if n["type"] == "Form"),
            "workflows": sum(1 for n in nodes if n["type"] == "Workflow"),
            "facts": sum(1 for n in nodes if n["type"] == "Fact"),
            "orphan_nodes": len(orphans),
            "max_depth": max(depths) if depths else 0,
            "graph_density": round(len(edges) / max(len(nodes), 1), 3),
            "edges_from_full_crawl": bool(raw_nav),
        }

        summary = (
            f"{metrics['pages']} page(s) linked by {metrics['total_edges']} relationship(s), "
            f"covering {metrics['forms']} form(s), {metrics['workflows']} workflow(s) and "
            f"{metrics['facts']} asserted fact(s)."
        )

        out_dir = workflow_dir(workflow_id, "graph")
        path = out_dir / "graph.json"
        payload = {"nodes": nodes, "edges": edges, "metrics": metrics,
                   "summary": summary, "derived_from": "crawl"}
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

        print(f"  [{self.name}] Graph built from crawl: {len(nodes)} nodes, {len(edges)} edges.")

        return {"knowledge_graph_output": {
            **payload,
            "artifact_path": to_relative(str(path)),
        }}


if __name__ == "__main__":  # pragma: no cover
    ok = fail = 0

    def check(name, cond, detail=""):
        global ok, fail
        if cond:
            ok += 1; print(f"  PASS  {name}")
        else:
            fail += 1; print(f"  FAIL  {name}  {detail}")

    facts = [{"id": "f1", "url": "https://x.test/", "selector": ".p", "tag": "span",
              "kind": "price", "text": "$99/month", "sha256": "a"}]
    ctx = {"workflow_id": "wf-kg-selftest", "explorer_output": {
        "entity": "X", "target_url": "https://x.test", "facts": facts,
        "pages": [{"url": "https://x.test/", "title": "Home", "depth": 0},
                  {"url": "https://x.test/pricing", "title": "Pricing", "depth": 1}],
        "navigation_graph": {"nodes": [{"id": "p1", "url": "https://x.test/", "title": "Home", "depth": 0},
                                       {"id": "p2", "url": "https://x.test/pricing", "title": "Pricing", "depth": 1}]},
        "forms": [{"method": "POST", "page_url": "https://x.test/", "fields": [{"name": "e"}]}],
        "workflows": [{"name": "Signup", "start_url": "https://x.test/",
                       "end_url": "https://x.test/pricing", "step_count": 2}],
    }}

    out = KnowledgeGraphAgent().run(ctx)["knowledge_graph_output"]
    m = out["metrics"]
    check("nodes built", m["total_nodes"] > 0)
    check("pages counted", m["pages"] == 2, str(m["pages"]))
    check("form node present", m["forms"] == 1)
    check("workflow node present", m["workflows"] == 1)
    check("fact node present", m["facts"] == 1)
    check("edges created", m["total_edges"] > 0)
    check("derived from crawl not llm", out["derived_from"] == "crawl")
    check("density computed", isinstance(m["graph_density"], float))
    check("artifact written", bool(out["artifact_path"]))
    labels = {n["label"] for n in out["nodes"]}
    check("real titles used", "Home" in labels and "Pricing" in labels)

    empty = KnowledgeGraphAgent().run({"workflow_id": "wf-kg-selftest2"})["knowledge_graph_output"]
    check("degrades with no data", empty["metrics"]["total_nodes"] == 0)

    print(f"\n{ok} passed, {fail} failed")
    raise SystemExit(1 if fail else 0)
