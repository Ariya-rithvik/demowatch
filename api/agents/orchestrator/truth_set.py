"""Truth set: the checkable facts a generated artifact is allowed to assert.

Every downstream agent (Demo, Documentation, QA, Release) is only permitted to make
claims that cite a fact from this set. That citation is what makes drift detection
possible later: re-crawl, re-hash, and any artifact citing a changed fact is stale.

Two different hashes matter here and conflating them breaks everything:

  * `id`     - derived from (url, selector). STABLE across crawls, so the same
               element keeps its identity even when its text changes.
  * `sha256` - derived from the normalised text. CHANGES when the content changes.

Drift is precisely: same `id`, different `sha256`.
"""

import hashlib
import re
from typing import Any, Dict, Iterable, List, Optional

# Facts are meant to be human-checkable claims, so ignore text that is too short
# to assert anything or long enough to be prose rather than a fact.
MIN_TEXT = 1
MAX_TEXT = 180

# Cap per page so a huge site cannot produce an unbounded truth set.
MAX_FACTS_PER_PAGE = 60
MAX_FACTS_TOTAL = 250

_CURRENCY = re.compile(r'[$£€¥₹]\s?\d|(?:\d+(?:[.,]\d+)?)\s?(?:USD|EUR|GBP|INR)\b', re.I)
_NUMBERY = re.compile(r'\d')
_WS = re.compile(r'\s+')

_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_CTA_TAGS = {"button", "a"}
_INPUTY_TAGS = {"input", "select", "textarea"}


def normalize_text(text: Optional[str]) -> str:
    """Collapse whitespace so trivial formatting changes are not read as drift."""
    if not text:
        return ""
    return _WS.sub(" ", str(text)).strip()


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def fact_id(url: str, selector: str) -> str:
    """Identity of a fact: where it lives, not what it currently says."""
    return "f_" + _sha(f"{url}||{selector}")[:16]


def classify(tag: str, text: str, element_type: str = "") -> str:
    """Best-effort kind, used for prioritisation and nicer UI labels."""
    tag = (tag or "").lower()
    if _CURRENCY.search(text):
        return "price"
    if tag in _HEADING_TAGS:
        return "heading"
    if tag in _CTA_TAGS and len(text) <= 40:
        return "cta"
    if tag == "li":
        return "feature"
    if tag in _INPUTY_TAGS or (element_type or "").lower() in _INPUTY_TAGS:
        return "field"
    if _NUMBERY.search(text):
        return "number"
    return "label"


# Higher scores survive truncation. Prices and headings are the claims that
# actually get people in trouble, so they rank above generic labels.
_KIND_RANK = {"price": 6, "heading": 5, "cta": 4, "feature": 3, "number": 2, "field": 1, "label": 0}


def _iter_elements(crawl: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    """Yield (page_url, element) pairs from either the paged or flat shape."""
    pages = crawl.get("pages") or []
    if pages:
        for page in pages:
            page_url = page.get("url") or ""
            for el in (page.get("elements") or []):
                yield page_url, el
    else:
        for el in (crawl.get("elements") or []):
            yield el.get("page_url") or crawl.get("summary", {}).get("target_url", ""), el


def extract_truth_set(crawl: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Build the deduplicated, bounded list of checkable facts from a crawl."""
    by_page: Dict[str, List[Dict[str, Any]]] = {}
    seen_ids = set()

    for page_url, el in _iter_elements(crawl):
        selector = el.get("css_selector") or el.get("xpath") or ""
        text = normalize_text(el.get("text"))

        if not selector or not text:
            continue
        if not (MIN_TEXT <= len(text) <= MAX_TEXT):
            continue
        # Invisible elements can't be asserted about in a visual demo.
        if el.get("is_visible") is False:
            continue

        fid = fact_id(page_url, selector)
        if fid in seen_ids:
            continue
        seen_ids.add(fid)

        kind = classify(el.get("tag_name", ""), text, el.get("element_type", ""))
        by_page.setdefault(page_url, []).append({
            "id": fid,
            "url": page_url,
            "selector": selector,
            "tag": (el.get("tag_name") or "").lower(),
            "kind": kind,
            "text": text,
            "sha256": _sha(text),
            "rank": _KIND_RANK.get(kind, 0),
        })

    facts: List[Dict[str, Any]] = []
    for page_url, items in by_page.items():
        items.sort(key=lambda f: (-f["rank"], f["selector"]))
        facts.extend(items[:MAX_FACTS_PER_PAGE])

    facts.sort(key=lambda f: (-f["rank"], f["url"], f["selector"]))
    for f in facts:
        f.pop("rank", None)
    return facts[:MAX_FACTS_TOTAL]


def index_by_id(facts: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {f["id"]: f for f in facts}


def load_truth_set(explorer_output: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Recover the truth set for a run, however it was persisted.

    Explorer now emits `facts` inline, but older runs (and the topic-only explorer)
    may only have the raw crawl on disk, so fall back rather than returning nothing.
    """
    if not explorer_output:
        return []

    facts = explorer_output.get("facts")
    if facts:
        return list(facts)

    import json as _json
    import os as _os

    for key in ("truth_set_file", "raw_result_file"):
        path = explorer_output.get(key)
        if not path or not _os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as fh:
                payload = _json.load(fh)
        except (OSError, ValueError):
            continue
        if key == "truth_set_file" and isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            return extract_truth_set(payload)

    return []


def diff_truth_sets(
    before: Iterable[Dict[str, Any]],
    after: Iterable[Dict[str, Any]],
) -> Dict[str, Any]:
    """Compare two crawls of the same source.

    CHANGED is the interesting case: the element still exists but now says
    something different, which is exactly what silently falsifies a published
    artifact.
    """
    a, b = index_by_id(before), index_by_id(after)

    changed, removed, added, unchanged = [], [], [], []

    for fid, old in a.items():
        new = b.get(fid)
        if new is None:
            removed.append({"id": fid, "url": old["url"], "selector": old["selector"],
                            "was": old["text"], "kind": old["kind"]})
        elif new["sha256"] != old["sha256"]:
            changed.append({"id": fid, "url": old["url"], "selector": old["selector"],
                            "was": old["text"], "now": new["text"], "kind": old["kind"]})
        else:
            unchanged.append(fid)

    for fid, new in b.items():
        if fid not in a:
            added.append({"id": fid, "url": new["url"], "selector": new["selector"],
                          "now": new["text"], "kind": new["kind"]})

    return {
        "changed": changed,
        "removed": removed,
        "added": added,
        "unchanged": unchanged,
        "has_drift": bool(changed or removed),
        "counts": {
            "changed": len(changed),
            "removed": len(removed),
            "added": len(added),
            "unchanged": len(unchanged),
        },
    }


def verify_claim(claim_text: str, cited: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Check that every number in a claim is backed by one of its cited facts.

    This is the guard that stops an agent inventing a figure: if a claim says
    "$129" but no cited fact contains 129, the claim is unsupported.
    """
    claim_text = normalize_text(claim_text)
    cited = list(cited)
    haystack = " ".join(normalize_text(f.get("text")) for f in cited)

    claim_nums = set(re.findall(r'\d+(?:[.,]\d+)?', claim_text))
    source_nums = set(re.findall(r'\d+(?:[.,]\d+)?', haystack))
    unsupported = sorted(claim_nums - source_nums)

    return {
        "claim": claim_text,
        "cited_ids": [f.get("id") for f in cited],
        "verified": not unsupported and bool(cited),
        "unsupported_numbers": unsupported,
        "reason": (
            "no facts cited" if not cited
            else f"numbers not found in source: {', '.join(unsupported)}" if unsupported
            else "all cited facts match source"
        ),
    }
