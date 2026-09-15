"""Veridemo Watch, end to end, against a real crawl.

Run: python examples/watch_demo.py

Loads facts from a real crawl of netflix.com (docs/proof-run/netflix_truth_set.json --
captured once and checked in so this example doesn't need network access to prove the
point), then audits a creator's review script against it. One claim in the script still
matches the live price; one is stale. No API key needed -- without GEMINI_API_KEY this
runs the deterministic keyword/number matcher instead of a model pass, same guarantee
either way: a claim's numbers must appear in the facts it's matched to, or it fails.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "api"))

from agents.orchestrator.agent_ingest import ContentIngestAgent  # noqa: E402
from agents.orchestrator.registry import GeminiClient  # noqa: E402

FACTS_FILE = Path(__file__).resolve().parent.parent / "docs" / "proof-run" / "netflix_truth_set.json"

CREATOR_SCRIPT = (
    "Netflix starts at just 149 rupees a month, which is a steal for the content library. "
    "You can cancel at any time with no penalty. "
    "Honestly Netflix now costs 499 rupees for the basic plan which is way too much."
)


def main() -> None:
    facts = json.loads(FACTS_FILE.read_text(encoding="utf-8"))
    print(f"Loaded {len(facts)} facts from a real crawl of netflix.com\n")
    print("Creator's script:")
    print(f"  {CREATOR_SCRIPT}\n")

    agent = ContentIngestAgent(llm=GeminiClient())
    result = agent.ingest(CREATOR_SCRIPT, facts)

    print(f"Engine: {result['generated_by']}")
    print(f"{result['verified']}/{result['total_claims']} claims verified\n")
    for c in result["claims"]:
        mark = "PASS" if c["verified"] else "FAIL"
        print(f"[{mark}] {c['text']}")
        if not c["verified"]:
            print(f"       -> {c['reason']}")

    assert result["failed"] >= 1, "the stale Rs 499 claim should fail verification"
    print("\nOK: the outdated price claim was caught before it could ship unnoticed.")


if __name__ == "__main__":
    main()
