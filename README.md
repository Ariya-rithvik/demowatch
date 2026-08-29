# Veridemo — demo videos you can audit

Give it a URL. It opens your product in a real browser, walks the flows a user would
take, and produces a narrated demo video in which **every spoken claim traces back to a
captured page element** — and any claim it cannot verify is dropped before the video ships.

---

## The problem

AI-generated product demos hallucinate. They describe features that don't exist and quote
prices that have changed, and nobody notices until a customer does. Tools that auto-update
demos re-record when the UI changes; none can tell you *which specific claim* in a finished
video is now false.

## The idea

Each scene is bound to the elements it asserts:

```json
{ "index": 2,
  "kind": "action",
  "narration": "Select Create Demo.",
  "asserts": ["f_6ba191cc38a21a37", "f_7f373b67509f32df"] }
```

Fact ids are a hash of `url + css_selector`; the content hash is a hash of the text. That
split is the whole system:

> **Drift = same fact id, different content hash.**

Re-crawl later and you know exactly which claims went false, which artifacts cited them,
and which agents must re-run. One changed price rebuilds one scene, not the whole video.

## Architecture

Four Zerops services:

```
web (static)  ──HTTP──▶  api (python)  ──▶  db (postgresql@16)
                              │
                              └──────────▶  storage (object-storage, S3)
```

**`api`** runs the pipeline — six agents in order:

| Agent | Does |
|---|---|
| Explorer | Drives real Chromium via Playwright; records clicks/typing, screenshots, and a hashed **truth set** |
| Knowledge graph | Builds a graph from the crawl's actual navigation edges |
| Documentation | Writes a user guide, recording which facts each section cites |
| Demo | Scenes follow **recorded user actions**, pushing in on the element involved; unverifiable scenes are dropped |
| QA | Verifies every claim against the truth set. Genuinely fails; reports `NO_CLAIMS` rather than inventing a pass rate |
| Release | Diffs against the previous crawl, resolves changed facts to stale artifacts |

## How Zerops is used

Zerops is the runtime, not a host for a single container. Each service exists because
something in the pipeline genuinely needs it:

**`db` — postgresql@16.** Workflow and task state. The orchestrator re-queues anything left
`RUNNING` on boot, so a redeploy resumes in-flight work instead of losing it. Zerops injects
`${db_connectionString}`; `persistence.py` switches to PostgreSQL when `DATABASE_URL` is set
and falls back to SQLite locally, so the same code runs in both places.

**`storage` — object-storage.** Rendered MP4s, screenshots, truth sets and verification
reports. Container filesystems are ephemeral, so without this every redeploy would lose
previously generated packages. Wired through `MediaStore`, which writes locally *and*
mirrors to S3, and can pull an object back with `fetch_to_local()` after a restart.

**`api` — python@3.11 on Ubuntu.** Needs Chromium and FFmpeg in the runtime image, which is
why `zerops.yml` installs them in `prepareCommands` and pins
`PLAYWRIGHT_BROWSERS_PATH` into the deployed tree. A crawl takes 60–90s and a render ~30s,
well past any serverless timeout — this needs a real container.

**`web` — static.** Deployed separately so redeploying the pipeline never takes the UI
offline. The API base URL is baked in at build time via `sed` on `__API_BASE__`.

**Health as readiness.** `/health` reports each backing service and returns **503** when
PostgreSQL is unreachable, so Zerops won't route traffic to a container that can't work.

## Deploy

```bash
zcli login <token>
zcli project project-import zerops-project-import.yml
zcli push --serviceId <api-service-id>
zcli push --serviceId <web-service-id>
```

## Run locally

```bash
pip install -r api/requirements.txt
python -m playwright install chromium          # FFmpeg must be on PATH

cd api && python -m uvicorn main:app --port 8080     # API
cd web && python -m http.server 8098                 # UI
```

With no `DATABASE_URL` it uses SQLite; with no S3 vars it writes to local disk. **No API
key is required** — narration is Edge TTS and compositing is FFmpeg, so the pipeline
produces a complete verified video out of the box.

## API

| Route | Purpose |
|---|---|
| `GET /health` | Per-service status; 503 when the database is down |
| `POST /workflows` | `{"goal": "demo of https://example.com"}` |
| `GET /workflows/{id}` | Live progress + the finished package |
| `GET /showcase` | Completed packages, read off disk |
| `GET /artifacts/{path}` | Serves artifacts, refuses paths outside the artifact root |

## Tests

Every module runs standalone:

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

The important one is in `agent_qa.py`: a hallucinated `$129` against a source that says
`$99` must produce `status: FAILED`.

## Honest limitations

- Claim verification checks numeric support and citation integrity, not semantic
  entailment — a non-numeric claim that is merely *implied* can still pass.
- Screenshots are captured per page, not per action, so an action scene zooms into a page
  capture rather than showing the true before/after of each click.
- Crawls are bounded (4 actions / 3 pages by default). Unbounded exploration of a large
  site took ~188s, too slow for interactive evaluation.
- Authenticated sites are supported by config but untested.

## AI assistance

This project was built with substantial assistance from Claude (Anthropic), used for
implementation, debugging and refactoring. Disclosed per the challenge rules.
