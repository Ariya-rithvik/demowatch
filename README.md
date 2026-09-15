# Veridemo — demo videos you can actually verify

**Built for the [AI Builders Hackathon 2026](https://ai-builders-hackathon-2026.devpost.com/).**

Give it a URL. It opens your product in a real browser, walks the flows a user would
take, and produces a narrated demo video in which **every spoken claim traces back to a
captured page element** — and any claim it cannot verify is dropped before the video ships.

---

## The problem

AI-generated product demos hallucinate. They describe features that don't exist and quote
prices that have changed, and nobody notices until a customer does. Every "AI demo video"
tool on the market records something and narrates over it — none of them can tell you
*which specific claim* in a finished video is now false, or prove that a claim was ever
true in the first place.

Manually recording, editing, and re-recording demos every time a product changes is the
alternative, and it doesn't scale: sales teams need a demo per segment, onboarding needs
one per feature release, and every UI change makes the old recording a liability instead
of an asset.

## The idea: claims are not free — they're citations

Veridemo doesn't let an AI say anything it can't point at. Every scene is bound to the
DOM elements it asserts:

```json
{ "index": 2,
  "kind": "action",
  "narration": "Select Create Demo.",
  "asserts": ["f_6ba191cc38a21a37", "f_7f373b67509f32df"] }
```

A **fact id** is a hash of `url + css_selector` — a stable pointer to one specific claim
on the page. A **content hash** is a hash of what that element currently says. That split
is the whole system:

> **Drift = same fact id, different content hash.**

Re-crawl the product later and Veridemo knows exactly which claims went false, which
video scenes cited them, and which need to be rebuilt. One changed price rebuilds one
scene — not the whole video.

## Six agents, one pipeline

A central orchestrator (FastAPI, priority queues, SQLite/Postgres-backed job state) runs
six agents in sequence, each one a real, independently-testable module:

| # | Agent | Does |
|---|---|---|
| 1 | **Explorer** | Drives real Chromium via Playwright — clicks, types, screenshots the actual product, and builds a hashed **truth set** of every fact on the page |
| 2 | **Knowledge graph** | Builds a navigation graph from the crawl's *actual* edges — not a guess at site structure |
| 3 | **Documentation** | Writes a user guide, recording which facts each section cites |
| 4 | **Demo** | Turns the recorded user actions into narrated video scenes, zooming into the element each line describes; a scene with no supporting fact is **dropped**, never shipped |
| 5 | **QA** | Re-checks every claim against the truth set. It genuinely fails — a hallucinated `$129` against a source that says `$99` produces `status: FAILED`, not an inflated pass rate |
| 6 | **Release** | Diffs against the previous crawl, and resolves exactly which stale facts invalidate which artifacts |

No API key is required to run the core pipeline — narration is Edge TTS and video
compositing is FFmpeg, so it produces a complete, verified, narrated MP4 out of the box.

## Demo

We pointed the running pipeline at `https://httpbin.org` with no prior data. In one
unattended pass it:

- Crawled **8 pages**, extracted **356 real DOM elements**, executed **8 real click actions**
- Built a knowledge graph and a 3-scene narrated demo video with AI voiceover
- **QA passed 11/11 checks (100%)** — every narrated claim verified against its cited
  facts, every screenshot confirmed present, zero dangling citations

That full run ships in this repo as evidence:

- [`docs/proof-run/demo.mp4`](docs/proof-run/demo.mp4) — the rendered, narrated video
- [`docs/proof-run/verification.json`](docs/proof-run/verification.json) — the QA agent's
  full pass/fail report for every claim
- [`docs/proof-run/screenshots/`](docs/proof-run/screenshots) — raw crawl screenshots the
  video scenes were built from

## Architecture

```
web (static)  ──HTTP──▶  api (python, FastAPI orchestrator)  ──▶  db (SQLite locally / Postgres in prod)
                              │
                              └──────────▶  storage (local disk / S3-compatible object storage)
```

- **`db`** — Workflow and task state. The orchestrator re-queues anything left `RUNNING`
  on restart, so a redeploy resumes in-flight work instead of losing it. `persistence.py`
  runs on SQLite locally and switches to PostgreSQL when `DATABASE_URL` is set — same code,
  both environments.
- **`storage`** — Rendered MP4s, screenshots, truth sets, and verification reports, mirrored
  to S3-compatible storage where configured so artifacts survive a redeploy.
- **`api`** — Needs Chromium + FFmpeg in the runtime; a crawl takes 60–90s and a render
  ~30s, well past any serverless timeout, so this runs as a real long-lived process.
- **`web`** — Deployed separately so redeploying the pipeline never takes the UI offline.

## Run it locally

```bash
pip install -r api/requirements.txt
python -m playwright install chromium          # FFmpeg must be on PATH

cd api && python -m uvicorn main:app --port 8080     # API
cd web && python -m http.server 8098                 # UI, then open :8098?api=http://localhost:8080
```

Paste any public product URL into the landing page and watch the pipeline run live.

## Optional: run it as an actual agent, not a script

The pipeline above runs a fixed sequence — nothing decides anything at runtime, which
makes it a script rather than an agent. `harness/veridemo_mcp.py` exposes the same
capabilities as MCP tools instead, so an LLM agent composes the pipeline itself:

| Tool | Annotation | What it does |
|---|---|---|
| `crawl_product` | read-only | Drives real Chromium and captures the facts a page states |
| `list_facts` | read-only | The facts the agent is allowed to cite |
| `check_narration` | read-only | **The guard** — accepts or rejects one proposed line |
| `review_script` | read-only | The lines accepted so far |
| `publish_demo` | **destructive** | The irreversible step — held for human approval |

**The loop is the point.** The agent writes a line, cites facts, and calls
`check_narration`. A line that states a number the source never mentions comes back
rejected with that number named, and has to be rewritten before it can ship:

```
check_narration("It costs $4999 per seat.", ["f_4e1b531b…"])
  → accepted: false
    unsupported_numbers: ["4999"]
    reason: "numbers not found in source: 4999"
```

`publish_demo` carries MCP's `destructiveHint`, so any MCP-compatible agent harness stops
and asks a person before anything ships. This is exposed via a standard MCP server
(`python harness/veridemo_mcp.py`, served on `:9077`) and can be wired into Claude,
TrueForge, or any other MCP-capable agent runtime — see `harness/` for a worked example
against TrueForge.

### Crawling safely

The stock explorer planner types `"Test Input"` into any text field it finds and clicks
whatever it can reach — fine on a page you own, not fine anywhere else. `crawl_product`
is therefore **observation-only unless `interact=true` is passed explicitly**. Capping
actions at zero doesn't achieve this on its own (extraction and action share one loop),
so read-only mode instead lets the crawl read the page and gives the planner nothing to
act on — a full extraction with provably zero interaction.

## API

| Route | Purpose |
|---|---|
| `GET /health` | Per-service status; 503 when the database is down |
| `POST /workflows` | `{"goal": "demo of https://example.com"}` |
| `GET /workflows/{id}` | Live progress + the finished package |
| `GET /showcase` | Completed packages, read off disk |
| `GET /artifacts/{path}` | Serves artifacts, refuses paths outside the artifact root |

## Tests

Every agent module runs standalone and proves a specific claim about itself:

```bash
cd api
python -m agents.orchestrator.agent_qa           # proves QA can genuinely FAIL
python -m agents.orchestrator.agent_demo         # proves unverifiable scenes are dropped
python -m agents.orchestrator.agent_release      # proves selective rebuild (2 stale, not 3)
python -m agents.orchestrator.persistence        # both DB backends
python -m agents.orchestrator.media_store
python -m agents.orchestrator.agent_documentation
python -m agents.orchestrator.agent_knowledge_graph
python -m agents.orchestrator.video_render
```

## Honest limitations

- Claim verification checks numeric support and citation integrity, not semantic
  entailment — a non-numeric claim that is merely *implied* can still pass.
- Screenshots are captured per page, not per action, so an action scene zooms into a page
  capture rather than showing the true before/after of each click.
- Crawls are bounded (4 actions / 3 pages by default). Unbounded exploration of a large
  site took ~188s, too slow for interactive evaluation.
- Authenticated sites are supported by config but untested.

## Impact & roadmap

Today this targets the demo-video use case, but the underlying mechanism — bind every
generated claim to a hashed source fact, verify before shipping, diff on re-crawl — applies
anywhere an AI narrates a live product: release notes, sales collateral, support
documentation, changelogs. The `Release` agent already resolves drift for exactly this
reason. Next: semantic (not just numeric) claim checking, per-action screenshots instead
of per-page, and authenticated-site crawling.

## Team

Built by the ADIP-agent team (originally an autonomous product-intelligence platform)
and rebuilt around fact-verified generation for this submission.

## AI assistance

This project was built with substantial assistance from Claude (Anthropic), used for
implementation, debugging, and refactoring. Disclosed per hackathon rules.
