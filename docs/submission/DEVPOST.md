# Devpost submission copy — Veridemo Watch

Paste-ready text for the AI Builders Hackathon 2026 submission form.

---

## Project name

Veridemo Watch

## Elevator pitch (max 200 chars)

Your product changed; your published content didn't. Watch re-reads what a creator already shipped, binds every claim to a live fact, and names the exact sentence that just became false.

## Built with

`python` `fastapi` `playwright` `gemini` `mcp` `sqlite` `postgresql` `chromium` `zerops` `ffmpeg` `edge-tts`

## Try it out links

- https://github.com/Ariya-rithvik/demowatch
- https://github.com/Ariya-rithvik/demo (sibling submission, same engine)

---

## About the project

### Inspiration

A creator says a plan costs ₹149. Six weeks later it doesn't. The video is still up, still
ranking, still converting people on a number that no longer exists — and nothing anywhere
tells the creator *which sentence to fix*.

Every creator has a back catalogue quietly making claims on their behalf forever. Nobody
re-watches a two-year-old review to check whether its prices still hold, so the correction
only ever arrives as an annoyed comment. That felt like a problem a machine should be
handling, and nothing was handling it.

### What it does

Point Watch at content a creator has **already published**. It:

1. **Segments** the raw script into individual claims
2. **Matches** each claim to a specific fact from a live crawl of the product
3. **Guards** it — a number stated in the content must actually appear in the source
4. **Reports** per claim: verified, or stale with the reason

Once a sentence is bound to a **fact id** (`hash(url + css_selector)`), going stale stops
being a judgement call and becomes a hash comparison:

> **Drift = same fact id, different content hash.**

Re-crawl on a schedule and the diff names the exact sentence that went false, in which
asset — and the Release agent can regenerate just that wording instead of the whole asset.

### How we built it

`api/agents/orchestrator/agent_ingest.py` is the ingestion agent. **One Gemini call does
segmentation *and* fact-matching together**, because splitting them into two passes throws
away the context that makes the match good. With no API key, a deterministic keyword and
number-overlap matcher takes over, so the audit still produces a real result with zero setup.

Either way, every claim the model extracts is then run through `check_narration` — the same
numeric-support guard the generator side uses. **An LLM mismatch fails verification. It does
not ship.** The model proposes; the hash decides.

The facts themselves are not a scraped corpus. They come from a real browser walking the
product: Playwright drives Chromium, and each element is hashed into a truth set. The audit
is exposed as an MCP tool, `audit_content`, so another agent can hand it a script and get
back a per-claim verdict with citations.

Stack: FastAPI orchestrator with priority queues, SQLite/Postgres job state, Playwright,
Gemini with a deterministic fallback, MCP, deployable on Zerops.

### Challenges we ran into

**Making the guard survive the LLM.** The appealing version of this product is "ask a model
whether the claim is still true." That version is exactly as hallucination-prone as the
content it's auditing. Keeping a deterministic check *after* the model pass — so the model
can only ever propose, never certify — was the design decision the whole thing rests on.

**Running with zero setup.** A judge should not need an API key to see the product work. So
the deterministic matcher isn't a stub; it's a real, working path that produces a real
verdict, and it's what runs in our demo video.

**Proving the demo isn't staged.** Our submission video doesn't embed a transcript. The
builder script *executes* `examples/watch_demo.py`, captures its actual stdout, and replays
it line by line on screen. If the audit ever stopped catching the stale ₹499, the video
would visibly stop showing it.

### Accomplishments that we're proud of

Run `python examples/watch_demo.py` against **60 real facts from a live crawl of
netflix.com**, with a creator script containing one accurate price and one stale one:

```
Loaded 60 facts from a real crawl of netflix.com
Engine: deterministic
2/3 claims verified

[PASS] Netflix starts at just 149 rupees a month, which is a steal for the content library.
[PASS] You can cancel at any time with no penalty.
[FAIL] Honestly Netflix now costs 499 rupees for the basic plan which is way too much.
       -> numbers not found in source: 499

OK: the outdated price claim was caught before it could ship unnoticed.
```

That's the whole product working end to end, on real data, with no API key — and the exact
run you see in our demo video, because the video is built by capturing it.

### What we learned

Correction is normally archaeology: someone has to remember what was said, go find it, and
check it by hand. Binding claims to fact ids turns that into a diff. The interesting part
wasn't the AI — it was deciding what the AI is *not allowed to be responsible for*.

### What's next for Veridemo Watch

- Scheduled re-crawls with drift notifications per asset
- Ingesting published video directly via transcript, not just raw scripts
- Selective regeneration: rewrite only the affected wording and re-render one segment
- Broadening the guard past numeric support to entity and capability claims

---

## Sibling project disclosure

We are also submitting [**Veridemo**](https://github.com/Ariya-rithvik/demo) to this
hackathon — same team, same verified-fact engine, different problem. Veridemo *generates*
verified demo videos; Watch *audits* content that already exists. Different input, different
user, shared mechanism, disclosed plainly rather than presented as unrelated.

## Video demo link

Upload `docs/submission/veridemo_watch_submission.mp4` (2:48) to YouTube as unlisted and
paste the URL here.

## Image gallery

1. `docs/submission/veridemo_watch_thumbnail.png` — main gallery image
2. `docs/submission/veridemo_watch_gallery_2.png` — the live audit output
3. `docs/submission/veridemo_watch_gallery_3.png` — the claim-binding mechanism
