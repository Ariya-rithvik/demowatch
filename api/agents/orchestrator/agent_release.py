"""Release agent: detects drift against the previous crawl and decides what to rebuild.

Its stated job was always to diff versions and report what changed, but the previous
implementation only asked an LLM to summarise the other agents' output. This does the
real comparison: it finds the last crawl of the same target, diffs the truth sets, and
resolves changed facts to the exact artifacts and agents that are now stale.

That resolution is the point. Without it a change means "regenerate everything"; with it
a changed price rebuilds one scene and one guide section.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .artifacts import workflow_dir, to_relative, artifact_root
from .media_store import DependencyRegistry
from .truth_set import load_truth_set, diff_truth_sets, extract_truth_set

SNAPSHOT_NAME = "truth_snapshot.json"


class ReleaseAgent:
    """Compares this crawl to the previous one and reports the blast radius."""

    def __init__(self, llm: Any = None):
        self.name = "Release"
        self.llm = llm

    # ── finding the previous observation ─────────────────────────────────────

    def _find_previous(self, target_url: str, current_wf: str) -> Optional[Tuple[str, List[Dict[str, Any]]]]:
        """Most recent earlier workflow that crawled the same URL, with its facts.

        Prefers a stored snapshot (cheap, already condensed) and falls back to
        re-extracting from that run's raw crawl.
        """
        root = artifact_root()
        if not root.is_dir() or not target_url:
            return None

        candidates: List[Tuple[float, str, Path]] = []
        for wf_dir in root.iterdir():
            if not wf_dir.is_dir() or wf_dir.name == current_wf:
                continue
            raw = wf_dir / "exploration" / "ExplorationResult.json"
            if not raw.is_file():
                continue
            try:
                with open(raw, "r", encoding="utf-8") as fh:
                    summary = (json.load(fh) or {}).get("summary") or {}
            except (OSError, ValueError):
                continue
            if (summary.get("target_url") or "").rstrip("/") != target_url.rstrip("/"):
                continue
            candidates.append((raw.stat().st_mtime, wf_dir.name, raw))

        if not candidates:
            return None

        candidates.sort(reverse=True)
        _, wf_name, raw_path = candidates[0]

        snapshot = root / wf_name / "release" / SNAPSHOT_NAME
        if snapshot.is_file():
            try:
                return wf_name, json.loads(snapshot.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass

        try:
            with open(raw_path, "r", encoding="utf-8") as fh:
                return wf_name, extract_truth_set(json.load(fh))
        except (OSError, ValueError):
            return None

    # ── changelog ────────────────────────────────────────────────────────────

    def _changelog(
        self,
        entity: str,
        target_url: str,
        is_baseline: bool,
        prev_wf: Optional[str],
        diff: Dict[str, Any],
        stale: List[Dict[str, Any]],
        rerun: List[str],
    ) -> str:
        lines = [f"# {entity} — change report", "", f"Source: `{target_url}`", ""]

        if is_baseline:
            lines += [
                "## Baseline observation",
                "",
                "This is the first recorded crawl of this source, so there is nothing to "
                "compare against yet. The truth set captured here becomes the reference "
                "for the next run.",
                "",
                f"Facts recorded: **{len(diff.get('unchanged', []))}**",
                "",
            ]
            return "\n".join(lines)

        counts = diff["counts"]
        lines += [
            f"Compared against workflow `{prev_wf}`.", "",
            "## Summary", "",
            f"- Changed: **{counts['changed']}**",
            f"- Removed: **{counts['removed']}**",
            f"- Added: **{counts['added']}**",
            f"- Unchanged: {counts['unchanged']}",
            "",
        ]

        if diff["changed"]:
            lines += ["## What changed", ""]
            for c in diff["changed"][:40]:
                lines.append(f"- **{c['kind']}** on `{c['url']}`: "
                             f"“{c['was']}” → “{c['now']}”")
            lines.append("")

        if diff["removed"]:
            lines += ["## Removed", ""]
            lines += [f"- **{r['kind']}** on `{r['url']}`: “{r['was']}”" for r in diff["removed"][:20]]
            lines.append("")

        if diff["added"]:
            lines += ["## Added", ""]
            lines += [f"- **{a['kind']}** on `{a['url']}`: “{a['now']}”" for a in diff["added"][:20]]
            lines.append("")

        lines += ["## Impact", ""]
        if not diff["has_drift"]:
            lines += ["No published claim was invalidated. Nothing needs rebuilding.", ""]
        elif stale:
            lines.append(f"{len(stale)} artifact(s) are now stale:")
            lines.append("")
            for s in stale[:30]:
                lines.append(f"- `{s['artifact_id']}` (produced by **{s['produced_by']}**)")
            lines += ["", f"Agents to re-run: {', '.join(f'**{a}**' for a in rerun)}", ""]
        else:
            lines += [
                "Source content changed, but no recorded artifact cited the affected facts, "
                "so nothing needs rebuilding.", "",
            ]
        return "\n".join(lines)

    # ── main ─────────────────────────────────────────────────────────────────

    def run(self, context: Dict[str, Any]) -> Dict[str, Any]:
        workflow_id = context.get("workflow_id", "adhoc")
        explorer = context.get("explorer_output") or {}
        facts = load_truth_set(explorer)
        entity = explorer.get("entity") or "This product"
        target_url = explorer.get("target_url") or ""

        out_dir = workflow_dir(workflow_id, "release")

        # Snapshot first so this run can be the baseline for the next one, even if
        # everything below finds nothing to compare.
        (out_dir / SNAPSHOT_NAME).write_text(
            json.dumps(facts, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        previous = self._find_previous(target_url, str(workflow_id))
        is_baseline = previous is None

        if is_baseline:
            # Console output stays ASCII: Windows terminals default to cp1252 and a
            # stray dash or arrow here crashes the whole run.
            print(f"  [{self.name}] Baseline observation - no earlier crawl of {target_url or 'this source'}.")
            diff = {"changed": [], "removed": [], "added": [], "unchanged": [f["id"] for f in facts],
                    "has_drift": False,
                    "counts": {"changed": 0, "removed": 0, "added": 0, "unchanged": len(facts)}}
            prev_wf, stale, rerun = None, [], []
        else:
            prev_wf, prev_facts = previous
            diff = diff_truth_sets(prev_facts, facts)
            changed_ids = [c["id"] for c in diff["changed"]] + [r["id"] for r in diff["removed"]]
            prev_registry = DependencyRegistry.load(prev_wf)
            stale = prev_registry.affected_by(changed_ids)
            rerun = prev_registry.agents_to_rerun(changed_ids)
            print(f"  [{self.name}] vs {prev_wf}: {diff['counts']['changed']} changed, "
                  f"{diff['counts']['removed']} removed -> {len(stale)} stale artifact(s).")

        changelog = self._changelog(entity, target_url, is_baseline, prev_wf, diff, stale, rerun)
        path = out_dir / "changelog.md"
        path.write_text(changelog, encoding="utf-8")

        # Release runs last, so it records what the finished package contains.
        package = sorted(
            p.relative_to(workflow_dir(workflow_id)).as_posix()
            for p in workflow_dir(workflow_id).rglob("*")
            if p.is_file() and p.name != "ExplorationResult.json"
        )

        if is_baseline:
            summary = f"Baseline: {len(facts)} facts recorded for {target_url or 'this source'}."
        elif diff["has_drift"]:
            summary = (f"{diff['counts']['changed']} changed and {diff['counts']['removed']} removed "
                       f"since {prev_wf}; {len(stale)} artifact(s) stale.")
        else:
            summary = f"No drift since {prev_wf}."

        return {"release_output": {
            "is_baseline": is_baseline,
            "previous_workflow_id": prev_wf,
            "has_drift": diff["has_drift"],
            "diff": diff,
            "stale_artifacts": stale,
            "agents_to_rerun": rerun,
            "artifact_path": to_relative(str(path)),
            "summary": summary,
            "package": package,
        }}


if __name__ == "__main__":  # pragma: no cover
    import shutil, tempfile
    ok = fail = 0

    def check(name, cond, detail=""):
        global ok, fail
        if cond:
            ok += 1; print(f"  PASS  {name}")
        else:
            fail += 1; print(f"  FAIL  {name}  {detail}")

    tmp = Path(tempfile.mkdtemp(prefix="adip_rel_"))
    os.environ["ADIP_ARTIFACT_DIR"] = str(tmp)

    URL = "https://x.test"

    def facts_v(price: str):
        return [
            {"id": "f1", "url": URL, "selector": ".p", "tag": "span", "kind": "price",
             "text": price, "sha256": price},
            {"id": "f2", "url": URL, "selector": "h1", "tag": "h1", "kind": "heading",
             "text": "Simple pricing", "sha256": "h"},
        ]

    def seed_crawl(wf: str):
        d = tmp / wf / "exploration"
        d.mkdir(parents=True, exist_ok=True)
        (d / "ExplorationResult.json").write_text(
            json.dumps({"summary": {"target_url": URL}}), encoding="utf-8")

    agent = ReleaseAgent()

    print("first run = baseline")
    seed_crawl("wf-r1")
    r1 = agent.run({"workflow_id": "wf-r1", "explorer_output": {
        "entity": "X", "target_url": URL, "facts": facts_v("$99/month")}})["release_output"]
    check("baseline detected", r1["is_baseline"] is True)
    check("baseline has no drift", r1["has_drift"] is False)
    check("changelog written", bool(r1["artifact_path"]))

    print("second run, price drifted")
    reg = DependencyRegistry("wf-r1")
    reg.declare("demo#scene1", "demo", ["f1"])
    reg.declare("guide.md#Pricing", "documentation", ["f1"])
    reg.declare("demo#scene2", "demo", ["f2"])
    reg.save()

    seed_crawl("wf-r2")
    r2 = agent.run({"workflow_id": "wf-r2", "explorer_output": {
        "entity": "X", "target_url": URL, "facts": facts_v("$129/month")}})["release_output"]
    check("not baseline", r2["is_baseline"] is False)
    check("previous found", r2["previous_workflow_id"] == "wf-r1", str(r2["previous_workflow_id"]))
    check("drift detected", r2["has_drift"] is True)
    check("one fact changed", r2["diff"]["counts"]["changed"] == 1, str(r2["diff"]["counts"]))
    check("selective: 2 stale not 3", len(r2["stale_artifacts"]) == 2, str(len(r2["stale_artifacts"])))
    check("both agents flagged", set(r2["agents_to_rerun"]) == {"demo", "documentation"})
    cl = (tmp / "wf-r2" / "release" / "changelog.md").read_text(encoding="utf-8")
    check("changelog names the change", "$99/month" in cl and "$129/month" in cl)

    print("third run, nothing changed")
    seed_crawl("wf-r3")
    r3 = agent.run({"workflow_id": "wf-r3", "explorer_output": {
        "entity": "X", "target_url": URL, "facts": facts_v("$129/month")}})["release_output"]
    check("no drift when stable", r3["has_drift"] is False, str(r3["diff"]["counts"]))
    check("nothing stale", r3["stale_artifacts"] == [])

    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{ok} passed, {fail} failed")
    raise SystemExit(1 if fail else 0)
