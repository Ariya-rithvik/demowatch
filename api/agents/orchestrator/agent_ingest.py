"""Ingest agent: turns a creator's existing content into verifiable claims.

This is the piece Veridemo Watch needed that Veridemo didn't: Veridemo narrates its
*own* actions, so it always knows which fact each line is about. A creator's existing
script/post/transcript was written by a human with no fact ids attached at all.

Same principle as Cutlist's segmentation+scoring (one model pass does both, because
splitting them doubles latency and the model picks better boundaries when it knows
what it's scoring for): one Gemini pass turns raw content into (claim, candidate fact
ids) pairs. That pass is not trusted on its own -- every claim it proposes still goes
through `verify_claim`, the same numeric-support guard `check_narration` uses. An LLM
that mismatches a claim to the wrong fact produces a claim that fails verification, not
a claim that silently ships.

No API key required: without GEMINI_API_KEY this runs a deterministic keyword/number
overlap matcher instead of a model pass. Same output shape either way, so callers
(and the MCP tool) don't need to know which path ran -- consistent with the rest of
this repo, which never requires a key to produce a real, verifiable result.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from .truth_set import normalize_text, verify_claim

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
MAX_FACTS_IN_PROMPT = 60
MAX_CLAIMS = 40


def _split_sentences(content: str) -> List[str]:
    parts = [normalize_text(s) for s in _SENTENCE_SPLIT.split(content or "")]
    return [s for s in parts if len(s) >= 8][:MAX_CLAIMS]


def _keyword_overlap_match(claim: str, facts: List[Dict[str, Any]]) -> List[str]:
    """Deterministic fallback: score facts by shared words/numbers, keep the best."""
    claim_words = set(re.findall(r"[a-z0-9]+", claim.lower()))
    if not claim_words:
        return []

    scored = []
    for f in facts:
        fact_words = set(re.findall(r"[a-z0-9]+", (f.get("text") or "").lower()))
        overlap = claim_words & fact_words
        if overlap:
            scored.append((len(overlap), f["id"]))

    scored.sort(key=lambda pair: -pair[0])
    return [fid for _, fid in scored[:3]]


def _llm_match(llm: Any, content: str, facts: List[Dict[str, Any]]) -> Optional[List[Dict[str, Any]]]:
    if not llm or not getattr(llm, "available", False):
        return None

    fact_lines = "\n".join(
        f'{f["id"]}: {(f.get("text") or "")[:100]}' for f in facts[:MAX_FACTS_IN_PROMPT]
    )
    prompt = f"""You are checking a creator's existing script against facts captured from a live product page.

FACTS (id: text) -- these are the only things that may be cited:
{fact_lines}

CREATOR'S CONTENT:
{content[:4000]}

Break the content into individual factual claims (skip greetings, filler, opinion with no
factual content). For each claim, list the fact ids it is actually about -- only ids from
the list above, only when the claim is plausibly describing that specific fact. A claim
about something not covered by any fact gets an empty fact_ids list.

Return JSON: {{"claims": [{{"text": "...", "fact_ids": ["f_..."]}}]}}"""

    result = llm.generate_json(prompt, fallback=None)
    claims = (result or {}).get("claims")
    return claims if isinstance(claims, list) else None


class ContentIngestAgent:
    """Turns arbitrary creator content into (claim, fact_ids, verified) triples."""

    def __init__(self, llm: Any = None):
        self.name = "Ingest"
        self.llm = llm

    def ingest(self, content: str, facts: List[Dict[str, Any]]) -> Dict[str, Any]:
        by_id = {f["id"]: f for f in facts}

        raw_claims = _llm_match(self.llm, content, facts)
        used_llm = raw_claims is not None
        if raw_claims is None:
            raw_claims = [
                {"text": sentence, "fact_ids": _keyword_overlap_match(sentence, facts)}
                for sentence in _split_sentences(content)
            ]

        checked = []
        for c in raw_claims[:MAX_CLAIMS]:
            text = normalize_text(c.get("text", ""))
            if not text:
                continue
            ids = [i for i in (c.get("fact_ids") or []) if isinstance(i, str)]
            known_ids = [i for i in ids if i in by_id]
            unknown_ids = [i for i in ids if i not in by_id]
            cited_facts = [by_id[i] for i in known_ids]

            verdict = verify_claim(text, cited_facts)
            checked.append({
                "text": text,
                "cited_ids": known_ids,
                "unknown_ids": unknown_ids,
                "verified": verdict["verified"] and not unknown_ids,
                "unsupported_numbers": verdict["unsupported_numbers"],
                "reason": verdict["reason"] if not unknown_ids else
                    f"cites fact ids that were never captured: {', '.join(unknown_ids)}",
            })

        verified_count = sum(1 for c in checked if c["verified"])
        return {
            "generated_by": "gemini" if used_llm else "deterministic",
            "total_claims": len(checked),
            "verified": verified_count,
            "failed": len(checked) - verified_count,
            "claims": checked,
        }

    def run(self, context: Dict[str, Any]) -> Dict[str, Any]:
        content = context.get("content", "")
        facts = context.get("facts") or []
        return {"ingest_output": self.ingest(content, facts)}


if __name__ == "__main__":
    # Proves the guard catches a wrong claim even when fact ids are supplied correctly,
    # exactly like agent_qa's $129-vs-$99 test -- same guarantee, different entry point.
    demo_facts = [
        {"id": "f_price", "text": "Pro plan: $99/seat per month", "kind": "price", "url": "https://x.test"},
        {"id": "f_cta", "text": "Start free trial", "kind": "cta", "url": "https://x.test"},
    ]
    agent = ContentIngestAgent(llm=None)  # force deterministic path for a reproducible test
    result = agent.ingest(
        "The Pro plan costs $99 per seat every month. Click start free trial to begin. "
        "It also costs $129 if you pay annually which this source never mentions.",
        demo_facts,
    )
    import json
    print(json.dumps(result, indent=2))
    assert result["total_claims"] >= 2, "expected at least 2 claims extracted"
    assert any(c["verified"] and "99" in c["text"] for c in result["claims"]), \
        "the $99 claim, correctly cited, must verify"
    assert any((not c["verified"]) and "129" in c["text"] for c in result["claims"]), \
        "the $129 claim, unsupported by any fact, must fail"
    print("OK: ingest agent extracts claims and the guard rejects the unsupported one")
