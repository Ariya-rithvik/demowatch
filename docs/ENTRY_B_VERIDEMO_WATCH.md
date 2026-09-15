# Veridemo Watch — a second submission built on the same verified-fact engine

**Submitted separately from [Veridemo](../README.md) to the AI Builders Hackathon 2026.**
Same core engine (crawl → hashed truth set → fact-checked claims → drift diff), a
different product surface and a different user.

> Disclosure: Veridemo Watch and Veridemo share one codebase — this repo. Veridemo
> *generates* a new demo video from a product URL. Veridemo Watch *audits and maintains*
> content a creator already published. We're submitting both because they're genuinely
> different products for different users, not two names for the same demo.

---

## The problem

Content creators — course authors, affiliate reviewers, YouTubers doing product
walkthroughs — publish content that states facts about a live product: prices, feature
names, button labels, plan limits. The product keeps changing after publication. Nobody
re-watches their own back catalog to catch what's now wrong, so stale claims sit live
indefinitely — a reviewer still quoting a discontinued price, a tutorial pointing at a
button that got renamed. The cost isn't hypothetical: viewers act on what they were told,
and being visibly wrong in public erodes trust fast.

## The idea

Point Veridemo Watch at a piece of content (a script, post, or the narration from an
existing demo) and the product URL it's about:

1. **Explorer** crawls the live product and builds a hashed **truth set** — one fact id
   per `url + css_selector`, one content hash per what that element currently says.
2. Each claim in the creator's existing content is checked against that truth set via
   `check_narration` — the same guard the video-generation pipeline uses. A claim that
   states a number the source doesn't support comes back rejected, with the offending
   number named.
3. The truth set is stored, not discarded. On a later re-crawl, the **Release** agent
   diffs the new truth set against the stored one and resolves exactly which facts
   changed to exactly which claims cited them — *"a changed price rebuilds one scene,
   not the whole video"* is the literal design goal of that agent (see
   `api/agents/orchestrator/agent_release.py`).
4. The creator gets notified with the specific line that's now wrong and why — not
   "something changed," but *"you said $99/seat; the page now says $149/seat"* — and only
   that line/scene is regenerated with AI, everything else stays untouched.

## Why this is a different product, not a reskin

| | Veridemo | Veridemo Watch |
|---|---|---|
| Input | A product URL | Existing published content + a product URL |
| Primary action | Generate a new narrated demo | Audit existing claims, then monitor them |
| Trigger | On demand | Ongoing — re-crawl detects drift over time |
| Output | A new verified video | A diff report + regenerated line/scene |
| User | Product/marketing teams | Individual content creators |

The shared piece is deliberate: both products refuse to let an AI assert something the
live product doesn't support. That refusal mechanism (`check_narration` + the hashed
truth set) is the actual innovation; these are two applications of it.

## What's implemented vs. what's pitched

Implemented and demonstrated in this repo today:
- Live crawl → truth set extraction (`api/agents/orchestrator/truth_set.py`)
- Claim verification against arbitrary supplied text, not just AI-generated narration
  (`check_narration` in `harness/veridemo_mcp.py` takes any `text` + `fact_ids`)
- Drift diff between two crawls of the same target, resolved to stale artifacts
  (`agent_release.py`)

Pitched, not yet built as a standalone product: the creator-facing notification surface
(email/webhook on drift) and a content-ingestion step that turns an arbitrary
script/post into a list of (claim, cited-selector) pairs automatically instead of the
creator supplying fact ids by hand. Both are thin layers over agents that already exist
— see [Roadmap](#roadmap).

## Roadmap

1. Content ingestion: given a script/post/video transcript, use an LLM to extract
   candidate claims and propose the `css_selector` each one is most likely describing,
   then run them all through `check_narration` in one batch.
2. Scheduled re-crawl per tracked product URL, diffed via the existing Release agent.
3. Notification surface: webhook/email the creator when drift is detected, with the
   specific old-vs-new fact and which of their claims it invalidates.
4. Regeneration: for content that was itself AI-narrated (e.g. a Veridemo-made video),
   automatically rebuild only the affected scene, exactly as the Release agent already
   resolves for the video-generation pipeline.

## Team & AI assistance

Same team and disclosure as [Veridemo](../README.md): built with substantial assistance
from Claude (Anthropic) for implementation, debugging, and refactoring.
