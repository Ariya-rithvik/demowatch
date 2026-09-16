"""Build the 10-slide submission deck for Veridemo Watch.

Imports its slides from build_submission_video.py, including the terminal slide,
which means the deck runs the real audit too: the output printed on the slide is
captured from an actual execution rather than pasted in.

    python tools/build_deck.py

Output: docs/submission/veridemo_watch_deck.pdf
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from playwright.async_api import async_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_submission_video import (  # noqa: E402
    OUT, build_slides, capture_real_run, slide,
)

WORK = OUT / ".deck"

STACK = slide(
    '<div class="kicker">How it is built</div>'
    "<h2>Real infrastructure, not a prompt chain</h2>"
    '<div class="grid2">'
    '<div class="card"><pre style="font-size:27px">'
    "<span class='accent'>ingest</span>         agent_ingest.py\n"
    "<span class='accent'>orchestrator</span>   FastAPI + priority queues\n"
    "<span class='accent'>job state</span>      SQLite / Postgres\n"
    "<span class='accent'>crawl</span>          Playwright, real Chromium\n"
    "<span class='accent'>reasoning</span>      Gemini, with a zero-key\n"
    "               deterministic fallback\n"
    "<span class='accent'>guard</span>          check_narration\n"
    "<span class='accent'>agent surface</span>  MCP tool: audit_content\n"
    "<span class='accent'>deploy</span>         Zerops (zerops.yml)</pre></div>"
    '<div class="card">'
    '<div style="font-size:34px;line-height:1.55;color:#cdd9e5">'
    "Watch is not a wrapper around a chat call. It is an agent surface: "
    "<b>audit_content</b> is exposed over MCP, so another agent can hand it a "
    "script and get back a per-claim verdict with citations."
    '<div class="rule"></div>'
    "Run it end to end with no API key and no account:"
    '<div class="mono" style="font-size:30px;color:#c084fc;margin-top:18px">'
    "python examples/watch_demo.py</div></div></div></div>",
    "api/agents/orchestrator/agent_ingest.py &middot; harness/veridemo_mcp.py",
)

IMPACT = slide(
    '<div class="kicker">Who this is for</div>'
    "<h2>The back catalogue nobody re-reads</h2>"
    '<div class="grid2">'
    '<div class="card">'
    '<div style="font-size:30px;color:#8fa3b5;letter-spacing:.14em;'
    'text-transform:uppercase;margin-bottom:18px">Today</div>'
    '<div style="font-size:34px;line-height:1.6;color:#cdd9e5">'
    "A creator's old videos, docs and posts keep making claims on their behalf "
    "forever. Nobody re-watches a two-year-old review to check whether its prices "
    "still hold, so the correction only ever comes from an annoyed "
    "comment.</div></div>"
    '<div class="card">'
    '<div style="font-size:30px;color:#8fa3b5;letter-spacing:.14em;'
    'text-transform:uppercase;margin-bottom:18px">With Watch</div>'
    '<div style="font-size:34px;line-height:1.6;color:#cdd9e5">'
    "Every claim is bound to a fact id at publish time. Re-crawl on a schedule and "
    "the hash diff names <b>the exact sentence</b> that went false, in which "
    "asset &mdash; and the Release agent can regenerate just that "
    "wording.</div></div></div>"
    '<div class="big" style="margin-top:44px">Correction stops being archaeology and '
    "becomes <mark>a diff</mark>.</div>",
)

DECK_ONLY = {"04b_stack": STACK, "06b_impact": IMPACT}

ORDER = ["01_title", "02_problem", "03_idea", "04_engine", "04b_stack",
         "05_terminal", "06_evidence", "06b_impact", "07_sibling", "08_close"]


async def build():
    WORK.mkdir(parents=True, exist_ok=True)
    print("running the real audit...")
    html_by_name = {s["name"]: s["html"] for s in build_slides(capture_real_run())}
    html_by_name.update(DECK_ONLY)

    missing = [n for n in ORDER if n not in html_by_name]
    if missing:
        raise SystemExit("unknown slides: " + ", ".join(missing))

    pages = "".join(
        '<div class="page">' + html_by_name[n].split("<body>")[1].split("</body>")[0]
        + "</div>" for n in ORDER)
    css = html_by_name[ORDER[0]].split("<style>")[1].split("</style>")[0]

    doc = ("<html><head><meta charset='utf-8'><style>" + css + """
        @page { size: 1920px 1080px; margin: 0; }
        html, body { width:auto; height:auto; display:block; padding:0; background:#0b0f14; }
        body::before { display:none; }
        .page {
          position:relative; width:1920px; height:1080px; overflow:hidden;
          page-break-after:always; break-after:page; background:#0b0f14;
          display:flex; flex-direction:column; justify-content:center;
          padding:96px 120px;
        }
        .page::before {
          content:""; position:absolute; inset:0;
          background:radial-gradient(1200px 700px at 78% 12%, rgba(168,85,247,.13), transparent 62%),
                     radial-gradient(900px 620px at 8% 92%, rgba(56,189,248,.09), transparent 60%);
        }
        .page > * { position:relative; }
        .page:last-child { page-break-after:auto; break-after:auto; }
        .foot { position:absolute; left:120px; bottom:56px; }
        </style></head><body>""" + pages + "</body></html>")

    src = WORK / "deck.html"
    src.write_text(doc, encoding="utf-8")

    out = OUT / "veridemo_watch_deck.pdf"
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page(viewport={"width": 1920, "height": 1080})
        await page.goto(src.as_uri())
        await page.wait_for_timeout(400)
        await page.pdf(path=str(out), width="1920px", height="1080px",
                       print_background=True,
                       margin={"top": "0", "bottom": "0", "left": "0", "right": "0"})
        await browser.close()

    print("%s  (%d slides, %.1f MB)" % (out, len(ORDER), out.stat().st_size / 1e6))
    if len(ORDER) > 10:
        print("WARNING: over the 10 slide submission limit")


if __name__ == "__main__":
    asyncio.run(build())
