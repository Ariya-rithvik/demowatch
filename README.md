# Veridemo Watch — stop your content from quietly going wrong

**Built for the [AI Builders Hackathon 2026](https://ai-builders-hackathon-2026.devpost.com/).**

You published a review, a tutorial, a walkthrough video. It stated real facts about a
real product — a price, a feature, a button label. The product changed. Your content
didn't. Nobody tells you which of your own claims are now wrong, so they just sit there,
live, quietly eroding trust every time a viewer notices before you do.

Veridemo Watch reads what you already said, checks it against the live product, and
tells you exactly which claim broke and why — before your audience finds out for you.

> **Sibling project disclosure:** Veridemo Watch shares its verified-fact engine with
> our companion submission, [**Veridemo**](https://github.com/Ariya-rithvik/demo) (same
> hackathon, same team). Veridemo *generates* new demo videos from a product URL.
> Veridemo Watch *audits and maintains* content a creator already published. Different
> input, different user, different primary workflow — built on one shared verification
> mechanism. We're disclosing this plainly rather than presenting unrelated projects,
> because the mechanism itself is the actual innovation and it's honest to say so.

---

## The problem

Content creators — course authors, affiliate reviewers, YouTubers doing product
walkthroughs — publish content that states facts about a live product: prices, feature
names, button labels, plan limits. The product keeps changing after publication. Nobody
re-watches their own back catalog to catch what's now wrong. The cost isn't
hypothetical: viewers act on what they were told, and being visibly wrong in public
erodes trust fast — a reviewer still quoting a discontinued price, a tutorial pointing
at a button that got renamed.

## How it works

```
your content (script/post/transcript)          live product URL
              │                                        │
              └──────────────┐          ┌──────────────┘
                              ▼          ▼
                     Ingest agent: segment content into
                     claims, match each to a crawled fact
                     (one Gemini pass, or a deterministic
                     matcher when no API key is set)
                              │
                              ▼
                  Guard: does every number in this claim
                  appear in the fact(s) it's matched to?
                              │
                    ┌─────────┴─────────┐
                    ▼                   ▼
                VERIFIED            FLAGGED — here's
                                     exactly what's
                                     wrong and why

              (re-crawl later → Release agent diffs
               the two truth sets → know exactly which
               claims just went stale)
```

1. **Explorer** crawls the live product with real Chromium and builds a hashed **truth
   set** — one fact id per `url + css_selector`, one content hash per what that element
   currently says.
2. **Ingest** takes your existing content — a whole script, not one line at a time — and
   segments it into individual claims, matching each to the fact ids it's actually about.
   This is a genuine AI task, not string matching dressed up: with `GEMINI_API_KEY` set,
   one Gemini pass does the segmentation and fact-matching together (splitting them into
   two passes doubles latency and the model does worse at each step in isolation — the
   same principle behind this team's Cutlist project doing transcript segmentation and
   clip-scoring together). Without a key, a deterministic
   keyword/number-overlap matcher produces the same shape of output — so the demo below
   runs with **zero setup and no API key**.
3. **The guard** re-checks every claim regardless of how it was matched: a claim's
   numbers must appear in the facts it cites, or it fails, with the offending number
   named. An LLM that mismatches a claim to the wrong fact produces a claim that fails
   verification — it can't silently ship a false match.
4. **Release** stores the truth set and, on a later re-crawl, diffs it against the
   stored one — *"a changed price rebuilds one scene, not the whole video"* is the
   literal design goal of this agent (`api/agents/orchestrator/agent_release.py`).

## See it work — against a real product, right now

```bash
pip install -r api/requirements.txt
python examples/watch_demo.py
```

No API key needed. This runs against **a real crawl of netflix.com** (60 facts,
committed in `docs/proof-run/netflix_truth_set.json`) with a creator script containing
one claim that still matches the live price and one that's gone stale:

```
Creator's script:
  Netflix starts at just 149 rupees a month, which is a steal for the content library.
  You can cancel at any time with no penalty.
  Honestly Netflix now costs 499 rupees for the basic plan which is way too much.

Engine: deterministic
2/3 claims verified

[PASS] Netflix starts at just 149 rupees a month, which is a steal for the content library.
[PASS] You can cancel at any time with no penalty.
[FAIL] Honestly Netflix now costs 499 rupees for the basic plan which is way too much.
       -> numbers not found in source: 499
```

That's the whole product, working end to end, on real data.

## Submission materials

- [`docs/submission/veridemo_watch_submission.mp4`](docs/submission/veridemo_watch_submission.mp4)
  — the 2:48 demo video
- [`docs/submission/veridemo_watch_deck.pdf`](docs/submission/veridemo_watch_deck.pdf) —
  the 10-slide deck

```bash
python tools/build_submission_video.py   # -> veridemo_watch_submission.mp4
python tools/build_deck.py               # -> veridemo_watch_deck.pdf
```

Neither is hand-assembled. Both builders **execute `examples/watch_demo.py` and capture
its actual stdout**, then replay it on screen line by line — so the PASS/PASS/FAIL you
see in the video is the run that happened while the video was being built, not a
screenshot of one that happened once. If the audit ever stopped catching the stale 499,
the video would visibly stop showing it.

## Run the full pipeline locally

```bash
pip install -r api/requirements.txt
python -m playwright install chromium          # FFmpeg must be on PATH

cd api && python -m uvicorn main:app --port 8080     # API
```

Then, as an MCP tool an agent can call directly (`harness/veridemo_mcp.py`,
served on `:9077`):

| Tool | What it does |
|---|---|
| `crawl_product` | Drives real Chromium and captures the facts a page states |
| `audit_content` | **This product's core tool.** Give it a whole script/post; it segments, matches, and verifies every claim in one call, adding verified ones to the session script |
| `check_narration` | Verify one proposed line at a time against cited facts |
| `list_facts` / `review_script` | Inspect what's been captured / accepted |
| `publish_demo` | Irreversible — held for human approval (MCP `destructiveHint`) |

## What's implemented vs. what's roadmap

**Implemented and demonstrated in this repo:**
- Live crawl → hashed truth set (`api/agents/orchestrator/truth_set.py`)
- AI-driven content ingestion — arbitrary text in, verified claims out
  (`api/agents/orchestrator/agent_ingest.py`)
- The numeric-support guard, shared with the sibling project's `check_narration`
- Drift diff between two crawls, resolved to exactly which facts changed
  (`api/agents/orchestrator/agent_release.py`)
- `audit_content` MCP tool wiring content ingestion into the agent-composable harness

**Roadmap — thin layers on top of what already exists, not new mechanisms:**
1. Scheduled re-crawl per tracked product URL, diffed via the existing Release agent.
2. Notification surface: webhook/email the creator when drift is detected, with the
   specific old-vs-new fact and which of their claims it invalidates.
3. Regeneration: for content that was itself AI-narrated (e.g. a Veridemo-made video),
   automatically rebuild only the affected scene, exactly as the Release agent already
   resolves for the video pipeline.

## Honest limitations

- Claim verification checks numeric support and citation integrity, not semantic
  entailment — a non-numeric claim that is merely *implied* can still pass.
- The deterministic fallback matcher is keyword/number overlap, not comprehension — it
  will miss a paraphrased claim a model pass would catch. The verification guard behind
  it is identical either way, so a wrong match still fails rather than ships.
- Crawls are bounded (4 actions / 3 pages by default) for interactive-evaluation speed.
- Authenticated sites are supported by config but untested.

## Tests

```bash
cd api
python -m agents.orchestrator.agent_ingest       # proves claim extraction + the guard catching an unsupported claim
python -m agents.orchestrator.agent_qa           # proves QA can genuinely FAIL
python -m agents.orchestrator.agent_release      # proves selective rebuild (2 stale, not 3)
```

## Team & AI assistance

Built by the same team as [Veridemo](https://github.com/Ariya-rithvik/demo). This
project was built with substantial assistance from Claude (Anthropic), used for
implementation, debugging, and refactoring. Disclosed per hackathon rules.
