"""Build the hackathon submission video for Veridemo Watch.

The centrepiece is a real run: this script executes examples/watch_demo.py,
captures its actual stdout, and replays it line by line on screen. Nothing is
typed out by hand, so if the audit ever stopped catching the stale price the
video would visibly stop showing it.

    python tools/build_submission_video.py

Output: docs/submission/veridemo_watch_submission.mp4
"""

from __future__ import annotations

import asyncio
import base64
import html
import io
import json
import shutil
import subprocess
import sys
from pathlib import Path

import edge_tts
from PIL import Image
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
PROOF = ROOT / "docs" / "proof-run"
OUT = ROOT / "docs" / "submission"
WORK = OUT / ".work"

W, H, FPS = 1920, 1080, 30
VOICE = "en-US-AriaNeural"
RATE = "+6%"

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"


def run(cmd):
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr[-4000:])
        raise SystemExit("command failed: " + " ".join(cmd[:6]) + " ...")


def duration(path):
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True).stdout.strip()
    return float(out)


def data_uri(path, max_w=900):
    img = Image.open(path).convert("RGB")
    if img.width > max_w:
        img = img.resize((max_w, int(img.height * max_w / img.width)), Image.LANCZOS)
    if img.height > max_w:
        img = img.crop((0, 0, img.width, max_w))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=82)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


CSS = """
* { margin:0; padding:0; box-sizing:border-box; }
body {
  width:1920px; height:1080px; overflow:hidden;
  background:#0b0f14; color:#e6edf3;
  font-family:"Segoe UI", -apple-system, system-ui, sans-serif;
  display:flex; flex-direction:column; justify-content:center;
  padding:96px 120px;
}
body::before {
  content:""; position:fixed; inset:0;
  background:radial-gradient(1200px 700px at 78% 12%, rgba(168,85,247,.13), transparent 62%),
             radial-gradient(900px 620px at 8% 92%, rgba(56,189,248,.09), transparent 60%);
  pointer-events:none;
}
.kicker { font-size:26px; letter-spacing:.32em; text-transform:uppercase;
          color:#c084fc; font-weight:700; margin-bottom:28px; }
h1 { font-size:104px; line-height:1.04; font-weight:700; letter-spacing:-.025em; }
h2 { font-size:70px; line-height:1.1; font-weight:700; letter-spacing:-.02em; margin-bottom:38px; }
.sub { font-size:38px; line-height:1.48; color:#9fb0c0; margin-top:34px; max-width:1500px; }
.big { font-size:46px; line-height:1.45; color:#cdd9e5; max-width:1560px; }
.accent { color:#c084fc; }
.cyan { color:#38bdf8; }
.good { color:#4ade80; }
.bad { color:#f87171; }
.dim { color:#64748b; }
mark { background:rgba(192,132,252,.16); color:#d8b4fe; padding:0 10px; border-radius:6px; }
.mono { font-family:"Cascadia Code", Consolas, monospace; }
.card { background:rgba(255,255,255,.035); border:1px solid rgba(255,255,255,.10);
        border-radius:18px; padding:36px 42px; }
pre { font-family:"Cascadia Code", Consolas, monospace; font-size:30px; line-height:1.6;
      white-space:pre; color:#cdd9e5; }
ul { list-style:none; }
li { font-size:40px; line-height:1.85; color:#cdd9e5; display:flex; gap:24px; align-items:baseline; }
li b { color:#e6edf3; }
.num { color:#c084fc; font-family:"Cascadia Code",Consolas,monospace; font-size:32px;
       min-width:56px; font-weight:700; }
.grid2 { display:grid; grid-template-columns:1fr 1fr; gap:34px; }
.foot { position:fixed; left:120px; bottom:56px; font-size:26px; color:#64748b;
        font-family:"Cascadia Code",Consolas,monospace; }
.rule { height:4px; width:150px; background:#c084fc; border-radius:2px; margin:34px 0; }
/* terminal */
.term { background:#05080c; border:1px solid rgba(255,255,255,.12); border-radius:16px;
        padding:0; overflow:hidden; }
.termbar { background:rgba(255,255,255,.05); padding:16px 26px; font-size:24px;
           color:#8fa3b5; font-family:"Cascadia Code",Consolas,monospace;
           border-bottom:1px solid rgba(255,255,255,.09); display:flex; gap:14px;
           align-items:center; }
.dot { width:14px; height:14px; border-radius:50%; display:inline-block; }
.termbody { padding:30px 36px; min-height:640px; }
/* the captured output contains one very long line (the creator's script), so it
   wraps rather than running off the card; the text itself is never altered */
.termbody pre { font-size:24px; line-height:1.5; white-space:pre-wrap;
                overflow-wrap:break-word; }
.caret { background:#c084fc; color:#c084fc; }
"""


def slide(body, foot=""):
    f = '<div class="foot">' + foot + "</div>" if foot else ""
    return ("<html><head><meta charset='utf-8'><style>" + CSS
            + "</style></head><body>" + body + f + "</body></html>")


# --------------------------------------------------------------------------
# the real run
# --------------------------------------------------------------------------

def capture_real_run():
    """Execute the actual audit and return its stdout lines."""
    proc = subprocess.run([sys.executable, str(ROOT / "examples" / "watch_demo.py")],
                          capture_output=True, text=True, cwd=str(ROOT), timeout=600)
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout + proc.stderr)
        raise SystemExit("watch_demo.py failed; refusing to build a video around it")
    lines = proc.stdout.replace("\r\n", "\n").rstrip("\n").split("\n")
    print("  captured %d lines of real output" % len(lines))
    return lines


def colorize(line):
    """Style the captured line without changing a character of its text."""
    esc = html.escape(line)
    if esc.startswith("[PASS]"):
        return "<span class='good'>[PASS]</span>" + esc[6:]
    if esc.startswith("[FAIL]"):
        return "<span class='bad'>[FAIL]</span>" + esc[6:]
    if esc.lstrip().startswith("-&gt;"):
        return "<span class='bad'>" + esc + "</span>"
    if esc.startswith("Engine:") or esc.startswith("Loaded"):
        return "<span class='cyan'>" + esc + "</span>"
    if "claims verified" in esc:
        return "<span class='accent'>" + esc + "</span>"
    if esc.startswith("OK:"):
        return "<span class='good'>" + esc + "</span>"
    return esc


def terminal_html(lines, upto, caret=True):
    shown = [colorize(l) for l in lines[:upto]]
    if caret and upto < len(lines):
        shown.append("<span class='caret'>&nbsp;</span>")
    body = "\n".join(shown)
    return slide(
        '<div class="kicker">A real run, captured live</div>'
        "<h2>The audit, as it actually runs</h2>"
        '<div class="term">'
        '<div class="termbar">'
        "<span class='dot' style='background:#f87171'></span>"
        "<span class='dot' style='background:#fbbf24'></span>"
        "<span class='dot' style='background:#4ade80'></span>"
        "<span style='margin-left:14px'>python examples/watch_demo.py</span></div>"
        '<div class="termbody"><pre>' + body + "</pre></div></div>",
        "output captured by tools/build_submission_video.py &mdash; not retyped",
    )


# --------------------------------------------------------------------------
# slides
# --------------------------------------------------------------------------

def build_slides(real_lines):
    shots = sorted(PROOF.glob("screenshots/*.png"))
    shot = data_uri(shots[1]) if len(shots) > 1 else ""

    return [
        dict(
            name="01_title", min_seconds=7.0,
            html=slide(
                '<div class="kicker">AI Builders Hackathon 2026</div>'
                "<h1>Veridemo Watch</h1>"
                '<div class="sub">Your product changed. Your published content did not. '
                "Watch finds the exact sentence that just became false.</div>",
                "github.com/Ariya-rithvik/demowatch",
            ),
            narration=(
                "Veridemo Watch. Your product changed. The content you already published "
                "did not. Watch finds the exact sentence that just became false."
            ),
        ),
        dict(
            name="02_problem", min_seconds=6.0,
            html=slide(
                '<div class="kicker">The problem</div>'
                "<h2>Published content rots silently</h2>"
                '<div class="big">A creator says a plan costs one forty nine. Six weeks later '
                "it does not. The video is still up, still ranking, still converting people on "
                "a number that no longer exists &mdash; and <mark>nothing anywhere tells the "
                "creator which sentence to fix</mark>.</div>",
            ),
            narration=(
                "A creator says a plan costs a hundred and forty nine. Six weeks later, it "
                "doesn't. The video is still up, still ranking, still convincing people with a "
                "number that no longer exists. And nothing anywhere tells the creator which "
                "sentence to fix."
            ),
        ),
        dict(
            name="03_idea", min_seconds=9.0,
            html=slide(
                '<div class="kicker">The idea</div>'
                "<h2>A script is not prose. It is a list of claims.</h2>"
                '<div class="grid2">'
                '<div class="card"><pre>'
                "<span class='dim'>raw script</span>\n"
                "     |\n"
                "     v\n"
                "<span class='accent'>segment</span>   into individual claims\n"
                "     |\n"
                "     v\n"
                "<span class='accent'>match</span>     each claim to a fact\n"
                "     |         from a live crawl\n"
                "     v\n"
                "<span class='accent'>guard</span>     numbers must appear\n"
                "               in the source</pre></div>"
                '<div class="card">'
                '<div style="font-size:34px;line-height:1.55;color:#cdd9e5">'
                "Once a sentence is bound to a <b>fact id</b>, going stale is not a judgement "
                "call any more &mdash; it is a hash comparison."
                '<div class="rule"></div>'
                "<b>Drift = same fact id, different content hash.</b> Re-crawl the product and "
                "Watch can name every published claim that just went false, and nothing "
                "else.</div></div></div>",
            ),
            narration=(
                "So Watch treats a script as a list of claims, not as prose. It segments the "
                "script, matches every claim to a fact from a live crawl of the product, and "
                "then guards it: a number stated in the content has to actually appear in the "
                "source. Once a sentence is bound to a fact id, going stale stops being a "
                "judgement call and becomes a hash comparison. Drift is the same fact id with "
                "a different content hash."
            ),
        ),
        dict(
            name="04_engine", min_seconds=9.0,
            html=slide(
                '<div class="kicker">How it works</div>'
                "<h2>One model pass, then a check the model cannot talk its way past</h2>"
                '<div class="grid2">'
                '<div class="card">'
                '<div style="font-size:30px;color:#8fa3b5;letter-spacing:.14em;'
                'text-transform:uppercase;margin-bottom:18px">The model pass</div>'
                '<div style="font-size:34px;line-height:1.6;color:#cdd9e5">'
                "A single Gemini call does segmentation <i>and</i> fact-matching together, "
                "because splitting them loses the context that makes the match good."
                "<br><br>No API key? A deterministic keyword and number-overlap matcher takes "
                "over, so the audit still produces a real result with zero setup.</div></div>"
                '<div class="card">'
                '<div style="font-size:30px;color:#8fa3b5;letter-spacing:.14em;'
                'text-transform:uppercase;margin-bottom:18px">The guard</div>'
                '<div style="font-size:34px;line-height:1.6;color:#cdd9e5">'
                "Every claim the model extracts is then run through the same numeric-support "
                "check the generator uses."
                '<div class="rule"></div>'
                "<b>An LLM mismatch fails verification.</b> It does not ship. The model "
                "proposes; the hash decides.</div></div></div>",
                "api/agents/orchestrator/agent_ingest.py &middot; MCP tool: audit_content",
            ),
            narration=(
                "One Gemini call does segmentation and fact matching together, because "
                "splitting them throws away the context that makes the match good. With no API "
                "key, a deterministic matcher takes over, so the audit still produces a real "
                "result with zero setup. And either way, every claim the model extracts is run "
                "through the same numeric support check the generator uses. An LLM mismatch "
                "fails verification. The model proposes; the hash decides."
            ),
        ),
        dict(
            name="05_terminal", min_seconds=14.0, terminal=real_lines,
            html=terminal_html(real_lines, len(real_lines), caret=False),
            narration=(
                "Here it is running for real. Sixty facts loaded from an actual crawl of "
                "Netflix, against a creator script with three claims. The first two check out. "
                "The third says the basic plan now costs four hundred ninety nine, and that "
                "number appears nowhere in the source, so it fails. That is the whole product: "
                "the stale claim gets named, before anyone acts on it."
            ),
        ),
        dict(
            name="06_evidence", min_seconds=8.0,
            html=slide(
                '<div class="kicker">Where the facts come from</div>'
                "<h2>A crawl, not a corpus</h2>"
                '<div class="grid2" style="align-items:center">'
                '<div><img src="' + shot + '" style="width:100%;max-height:700px;'
                "object-fit:cover;object-position:top;border-radius:14px;"
                'border:1px solid rgba(255,255,255,.14)"></div>'
                '<div class="card"><pre>'
                "<span class='cyan'>60</span>   facts in the Netflix truth set\n"
                "<span class='cyan'>356</span>  DOM elements hashed per crawl\n"
                "<span class='cyan'>8</span>    pages walked by a real browser\n\n"
                "<span class='dim'>docs/proof-run/</span>\n"
                "  netflix_truth_set.json\n"
                "  verification.json\n"
                "  screenshots/</pre>"
                '<div style="font-size:30px;color:#9fb0c0;margin-top:26px;line-height:1.5">'
                "Committed to the repo. Clone it and every number in this video is "
                "reproducible.</div></div></div>",
            ),
            narration=(
                "The facts are not a scraped corpus. They come from a real browser walking the "
                "product: eight pages, three hundred and fifty six hashed DOM elements, sixty "
                "facts in the Netflix truth set. All of it is committed to the repository, so "
                "you can clone it and reproduce every number in this video."
            ),
        ),
        dict(
            name="07_sibling", min_seconds=9.0,
            html=slide(
                '<div class="kicker">Disclosed, not hidden</div>'
                "<h2>Two entries, one engine</h2>"
                '<div class="big">We are also submitting <b>Veridemo</b>, which uses the same '
                "verified-fact engine to <i>generate</i> demo videos. Same mechanism, different "
                "input and different user &mdash; and we would rather say so plainly than "
                "present them as unrelated.</div>"
                '<div class="grid2" style="margin-top:46px">'
                '<div class="card"><div style="font-size:28px;color:#8fa3b5;letter-spacing:.14em;'
                'text-transform:uppercase;margin-bottom:14px">Generate</div>'
                '<div class="mono cyan" style="font-size:34px">'
                "github.com/Ariya-rithvik/demo</div></div>"
                '<div class="card"><div style="font-size:28px;color:#8fa3b5;letter-spacing:.14em;'
                'text-transform:uppercase;margin-bottom:14px">Maintain &mdash; this one</div>'
                '<div class="mono accent" style="font-size:34px">'
                "github.com/Ariya-rithvik/demowatch</div></div></div>",
            ),
            narration=(
                "One disclosure. We are also submitting Veridemo, which uses this same verified "
                "fact engine to generate demo videos rather than audit them. Same mechanism, "
                "different input, different user. We would rather say that plainly than present "
                "the two as unrelated."
            ),
        ),
        dict(
            name="08_close", min_seconds=9.0,
            html=slide(
                '<div class="kicker">Veridemo Watch</div>'
                "<h2>Someone should be checking</h2>"
                '<div class="big">Every creator has a back catalogue quietly making claims on '
                "their behalf. Watch is the thing that <mark>re-reads all of it every time the "
                "product ships</mark> &mdash; and tells you the one sentence that needs to "
                "change.</div>"
                '<div class="sub mono" style="margin-top:56px">'
                "git clone github.com/Ariya-rithvik/demowatch<br>"
                "python examples/watch_demo.py</div>",
            ),
            narration=(
                "Every creator has a back catalogue quietly making claims on their behalf. "
                "Watch is the thing that re-reads all of it every time the product ships, and "
                "tells you the one sentence that needs to change. It is public, it runs with no "
                "API key, and the evidence is in the repo. Thank you for watching."
            ),
        ),
    ]


# --------------------------------------------------------------------------
# render
# --------------------------------------------------------------------------

async def render(slides):
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page(viewport={"width": W, "height": H},
                                      device_scale_factor=1)

        async def shot(html_text, png_path):
            f = WORK / (png_path.stem + ".html")
            f.write_text(html_text, encoding="utf-8")
            await page.goto(f.as_uri())
            await page.wait_for_timeout(120)
            await page.screenshot(path=str(png_path))

        for s in slides:
            if "terminal" in s:
                lines = s["terminal"]
                s["frames"] = []
                for i in range(1, len(lines) + 1):
                    p = WORK / ("%s_%03d.png" % (s["name"], i))
                    await shot(terminal_html(lines, i, caret=i < len(lines)), p)
                    s["frames"].append(p)
                print("  frames %s (%d reveal steps)" % (s["name"], len(s["frames"])))
            else:
                await shot(s["html"], WORK / (s["name"] + ".png"))
                print("  frame " + s["name"])
        await browser.close()


async def voice(slides):
    for s in slides:
        out = WORK / (s["name"] + ".mp3")
        await edge_tts.Communicate(s["narration"], VOICE, rate=RATE).save(str(out))
        print("  voice " + s["name"])


def encode_still(s):
    png = WORK / (s["name"] + ".png")
    mp3 = WORK / (s["name"] + ".mp3")
    seg = WORK / ("seg_" + s["name"] + ".mp4")
    total = max(duration(mp3) + 1.2, s["min_seconds"])
    run([FFMPEG, "-y", "-loop", "1", "-framerate", str(FPS), "-i", str(png), "-i", str(mp3),
         "-filter_complex", "[1:a]adelay=350|350,apad,aresample=48000[a]",
         "-map", "0:v", "-map", "[a]", "-t", "%.3f" % total,
         "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-tune", "stillimage",
         "-pix_fmt", "yuv420p", "-r", str(FPS), "-s", "%dx%d" % (W, H),
         "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", str(seg)])
    return seg


def encode_terminal(s):
    """Reveal the captured output line by line, then hold on the finished screen."""
    mp3 = WORK / (s["name"] + ".mp3")
    seg = WORK / ("seg_" + s["name"] + ".mp4")
    frames = s["frames"]
    total = max(duration(mp3) + 1.6, s["min_seconds"])

    # spend the first 55% revealing, hold the completed terminal for the rest
    reveal = total * 0.55
    per = reveal / len(frames)

    lines = []
    for p in frames:
        lines.append("file '" + p.as_posix() + "'")
        lines.append("duration %.3f" % per)
    lines.append("file '" + frames[-1].as_posix() + "'")
    lines.append("duration %.3f" % (total - reveal))
    lines.append("file '" + frames[-1].as_posix() + "'")  # concat demuxer needs the repeat
    listing = WORK / (s["name"] + "_frames.txt")
    listing.write_text("\n".join(lines) + "\n", encoding="utf-8")

    run([FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", str(listing), "-i", str(mp3),
         "-filter_complex", "[1:a]adelay=350|350,apad,aresample=48000[a]",
         "-map", "0:v", "-map", "[a]", "-t", "%.3f" % total,
         "-c:v", "libx264", "-preset", "medium", "-crf", "20",
         "-pix_fmt", "yuv420p", "-r", str(FPS), "-s", "%dx%d" % (W, H),
         "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", str(seg)])
    return seg


def main():
    WORK.mkdir(parents=True, exist_ok=True)
    print("running the real audit...")
    real_lines = capture_real_run()

    slides = build_slides(real_lines)
    print("rendering frames...")
    asyncio.run(render(slides))
    print("rendering narration...")
    asyncio.run(voice(slides))

    print("encoding segments...")
    order = [encode_terminal(s) if "terminal" in s else encode_still(s) for s in slides]

    listing = WORK / "concat.txt"
    listing.write_text("".join("file '" + p.as_posix() + "'\n" for p in order),
                       encoding="utf-8")

    final = OUT / "veridemo_watch_submission.mp4"
    run([FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", str(listing),
         "-c", "copy", "-movflags", "+faststart", str(final)])

    d = duration(final)
    print("\n%s  (%dm %04.1fs, %.1f MB)"
          % (final, d // 60, d % 60, final.stat().st_size / 1e6))
    if d > 300:
        print("WARNING: over the 5 minute submission limit")


if __name__ == "__main__":
    main()
