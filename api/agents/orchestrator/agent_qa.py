"""QA agent: verifies that generated artifacts only claim things the crawl supports.

This replaces an agent that fabricated its own results - it hardcoded PASSED for every
test, always returned total_failed=0, and reported things like "GDPR & SOC2 compliance
verified" without executing anything. It was structurally incapable of failing.

The rule here is the opposite: every number reported is counted from real checks, and
failure states are reachable. If there is nothing to verify, that is reported as
NO_CLAIMS rather than dressed up as a pass.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Iterable, List, Optional

from .artifacts import workflow_dir, to_relative
from .truth_set import load_truth_set, index_by_id, verify_claim


class QAAgent:
    """Checks demo scenes and documentation sections against the truth set."""

    def __init__(self, llm: Any = None):
        self.name = "QA"
        self.llm = llm

    # ── claim collection ─────────────────────────────────────────────────────

    def _collect_claims(self, context: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Gather every assertion downstream agents made, with its citations."""
        claims: List[Dict[str, Any]] = []

        demo = context.get("demo_output") or {}
        for scene in (demo.get("scenes") or []):
            text = (scene.get("narration") or "").strip()
            if not text:
                continue
            claims.append({
                "source": "demo",
                "ref": f"demo#scene{scene.get('index', len(claims))}",
                "text": text,
                "cited_ids": list(scene.get("asserts") or []),
            })

        docs = context.get("documentation_output") or {}
        for dep in (docs.get("section_deps") or []):
            text = (dep.get("claim") or dep.get("section") or "").strip()
            if not text:
                continue
            claims.append({
                "source": "documentation",
                "ref": f"guide.md#{dep.get('section', '')}",
                "text": text,
                "cited_ids": list(dep.get("fact_ids") or []),
            })

        return claims

    # ── structural checks ────────────────────────────────────────────────────

    def _check_screenshots(self, context: Dict[str, Any]) -> List[Dict[str, Any]]:
        """A referenced screenshot that is not on disk is a real, reportable defect."""
        results = []
        explorer = context.get("explorer_output") or {}
        for shot in (explorer.get("screenshots") or []):
            path = shot.get("file_path")
            exists = bool(path) and os.path.isfile(path)
            results.append({
                "check": "screenshot_exists",
                "target": shot.get("url") or path,
                "passed": exists,
                "reason": "file present" if exists else f"missing file: {path}",
            })
        return results

    # ── main ─────────────────────────────────────────────────────────────────

    def run(self, context: Dict[str, Any]) -> Dict[str, Any]:
        workflow_id = context.get("workflow_id", "adhoc")
        explorer = context.get("explorer_output") or {}
        facts = load_truth_set(explorer)
        by_id = index_by_id(facts)

        print(f"  [{self.name}] Verifying claims against {len(facts)} source facts...")

        claims = self._collect_claims(context)
        checks: List[Dict[str, Any]] = []
        verified = failed = uncited = dangling = 0

        for claim in claims:
            cited_ids = claim["cited_ids"]
            known = [by_id[cid] for cid in cited_ids if cid in by_id]
            missing_ids = [cid for cid in cited_ids if cid not in by_id]

            result = verify_claim(claim["text"], known)

            # A citation pointing at a fact that no longer exists is its own failure:
            # it means the artifact references something the source no longer says.
            is_dangling = bool(missing_ids)
            passed = result["verified"] and not is_dangling

            if not cited_ids:
                uncited += 1
                severity = "high"
                reason = "claim cites no source facts"
            elif is_dangling:
                dangling += 1
                severity = "high"
                reason = f"cites unknown fact ids: {', '.join(missing_ids)}"
            elif not result["verified"]:
                severity = "high"
                reason = result["reason"]
            else:
                severity = "none"
                reason = result["reason"]

            if passed:
                verified += 1
            else:
                failed += 1

            checks.append({
                "check": "claim_supported",
                "source": claim["source"],
                "ref": claim["ref"],
                "claim": claim["text"][:240],
                "cited_ids": cited_ids,
                "unknown_ids": missing_ids,
                "unsupported_numbers": result["unsupported_numbers"],
                "passed": passed,
                "severity": severity,
                "reason": reason,
            })

        structural = self._check_screenshots(context)
        checks.extend(structural)
        struct_failed = sum(1 for c in structural if not c["passed"])

        total_claims = len(claims)
        if total_claims == 0:
            # Nothing asserted anything, so there is no pass rate to report. Saying
            # "100%" here would be exactly the fabrication this agent exists to prevent.
            status = "NO_CLAIMS"
            pass_rate = None
            verifiable = False
        else:
            verifiable = True
            status = "PASSED" if (failed == 0 and struct_failed == 0) else "FAILED"
            pass_rate = f"{(verified / total_claims) * 100:.1f}%"

        output: Dict[str, Any] = {
            "status": status,
            "verifiable": verifiable,
            "facts_available": len(facts),
            "total_claims": total_claims,
            "verified": verified,
            "failed": failed,
            "uncited": uncited,
            "dangling_citations": dangling,
            "structural_checks": len(structural),
            "structural_failures": struct_failed,
            "pass_rate": pass_rate,
            "checks": checks,
        }

        out_dir = workflow_dir(workflow_id, "qa")
        report = out_dir / "verification.json"
        report.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
        output["artifact_path"] = to_relative(str(report))

        print(
            f"  [{self.name}] {status}: {verified}/{total_claims} claims verified, "
            f"{failed} failed, {struct_failed} structural failures."
        )
        return {"qa_output": output}


if __name__ == "__main__":  # pragma: no cover - manual smoke test
    ok = fail = 0

    def check(name: str, cond: bool, detail: str = "") -> None:
        global ok, fail
        if cond:
            ok += 1
            print(f"  PASS  {name}")
        else:
            fail += 1
            print(f"  FAIL  {name}  {detail}")

    facts = [
        {"id": "f1", "url": "u", "selector": ".price", "tag": "span", "kind": "price",
         "text": "$99/month", "sha256": "x"},
        {"id": "f2", "url": "u", "selector": ".mins", "tag": "li", "kind": "feature",
         "text": "50 AI minutes", "sha256": "y"},
    ]
    base = {"workflow_id": "wf-qa-selftest", "explorer_output": {"facts": facts, "screenshots": []}}
    agent = QAAgent()

    print("truthful claim")
    r = agent.run({**base, "demo_output": {"scenes": [
        {"index": 1, "narration": "Pro is $99 a month with 50 AI minutes.", "asserts": ["f1", "f2"]}]}})["qa_output"]
    check("status PASSED", r["status"] == "PASSED", r["status"])
    check("pass rate computed", r["pass_rate"] == "100.0%", str(r["pass_rate"]))

    print("hallucinated number  <-- the agent MUST be able to fail")
    r = agent.run({**base, "demo_output": {"scenes": [
        {"index": 1, "narration": "Pro is $129 a month.", "asserts": ["f1"]}]}})["qa_output"]
    check("status FAILED", r["status"] == "FAILED", r["status"])
    check("failure counted", r["failed"] == 1)
    check("unsupported number named", "129" in r["checks"][0]["unsupported_numbers"])

    print("uncited claim")
    r = agent.run({**base, "demo_output": {"scenes": [
        {"index": 1, "narration": "We offer unlimited exports.", "asserts": []}]}})["qa_output"]
    check("uncited fails", r["status"] == "FAILED" and r["uncited"] == 1)

    print("dangling citation")
    r = agent.run({**base, "demo_output": {"scenes": [
        {"index": 1, "narration": "Pro is $99.", "asserts": ["f_gone"]}]}})["qa_output"]
    check("dangling fails", r["status"] == "FAILED" and r["dangling_citations"] == 1)

    print("no claims at all")
    r = agent.run(base)["qa_output"]
    check("status NO_CLAIMS", r["status"] == "NO_CLAIMS", r["status"])
    check("no fake pass rate", r["pass_rate"] is None)
    check("marked unverifiable", r["verifiable"] is False)

    print("missing screenshot is caught")
    r = agent.run({**base, "explorer_output": {"facts": facts, "screenshots": [
        {"file_path": "Z:/definitely/not/here.png", "url": "u"}]},
        "demo_output": {"scenes": [{"index": 1, "narration": "Pro is $99.", "asserts": ["f1"]}]}})["qa_output"]
    check("structural failure detected", r["structural_failures"] == 1 and r["status"] == "FAILED")

    print("degrades with no explorer data")
    r = agent.run({"workflow_id": "wf-qa-selftest"})["qa_output"]
    check("no crash without input", r["status"] == "NO_CLAIMS")

    print(f"\n{ok} passed, {fail} failed")
    raise SystemExit(1 if fail else 0)
