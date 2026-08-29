"""Demo agent: builds a product demo from the user flows the crawler actually walked.

A product demo is about what a user *does*, so scenes follow the workflows the Explorer
recorded - click this, type there, land here - and each action scene focuses on the
element involved. Earlier versions made one scene per page and narrated a list of every
string on it, which reads like a database dump rather than a demo.

Two rules survive from the verification model and are the point of the whole pipeline:
every scene names the truth-set facts it asserts, and any scene whose narration cannot
be verified against those facts is dropped before it ships.
"""

from __future__ import annotations

import html
import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from .artifacts import workflow_dir, to_relative, artifact_root
from .media_store import MediaStore, DependencyRegistry
from .truth_set import load_truth_set, index_by_id, verify_claim, fact_id, normalize_text

MAX_SCENES = 9
MAX_ACTION_SCENES = 6


class DemoAgent:
    """Turns recorded user flows into verified, citation-backed demo scenes."""

    def __init__(self, llm: Any = None):
        self.name = "Demo"
        self.llm = llm

    # ── crawl access ─────────────────────────────────────────────────────────

    def _load_raw(self, explorer: Dict[str, Any]) -> Dict[str, Any]:
        """The full crawl, which carries workflow steps and element geometry.

        The condensed summary drops both, so the demo needs the raw file.
        """
        path = explorer.get("raw_result_file")
        if not path or not os.path.isfile(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh) or {}
        except (OSError, ValueError):
            return {}

    def _element_index(self, raw: Dict[str, Any]) -> Dict[Tuple[str, str], Dict[str, Any]]:
        """(page_url, css_selector) -> element, for joining actions to geometry."""
        index: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for page in (raw.get("pages") or []):
            url = page.get("url") or ""
            for el in (page.get("elements") or []):
                sel = el.get("css_selector")
                if sel:
                    index.setdefault((url, sel), el)
        for el in (raw.get("elements") or []):
            sel = el.get("css_selector")
            url = el.get("page_url") or ""
            if sel:
                index.setdefault((url, sel), el)
        return index

    def _shot_index(self, explorer: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        return {s.get("url"): s for s in (explorer.get("screenshots") or []) if s.get("url")}

    def _title_index(self, explorer: Dict[str, Any]) -> Dict[str, str]:
        return {p.get("url"): (p.get("title") or "") for p in (explorer.get("pages") or [])}

    # ── narration ────────────────────────────────────────────────────────────

    # Typographic characters that TTS mispronounces and FFmpeg's drawtext mangles.
    _TYPO = {
        "—": "-", "–": "-", "‒": "-", "−": "-",
        "‘": "'", "’": "'", "“": '"', "”": '"',
        "…": "...", " ": " ", "�": "",
        # Arrows and bullets read badly aloud and render as boxes in captions.
        "→": "", "←": "", "⇒": "", "»": "", "›": "", "•": "",
    }

    @classmethod
    def _clean(cls, text: str) -> str:
        """Flatten smart punctuation so narration survives TTS and caption rendering."""
        for bad, good in cls._TYPO.items():
            text = text.replace(bad, good)
        return normalize_text(text)

    @classmethod
    def _short_entity(cls, entity: str) -> str:
        """Page titles are often 'Brand - tagline'; narration only wants the brand."""
        cleaned = cls._clean(entity or "")
        for sep in (" - ", " | ", ": "):
            if sep in cleaned:
                head = cleaned.split(sep)[0].strip()
                if len(head) >= 3:
                    return head
        return cleaned

    @staticmethod
    def _speakable(text: str) -> bool:
        """Reject labels that would not survive being read aloud.

        Icon buttons often carry a private-use glyph or a replacement character, and
        narrating "Select <box>" is worse than saying nothing.
        """
        if not text:
            return False
        letters = sum(c.isalnum() for c in text)
        if letters < 2:
            return False
        odd = sum(1 for c in text if c == "�" or (not c.isprintable()) or ord(c) > 0x2100)
        return odd / len(text) < 0.2

    def _label_for(self, element: Optional[Dict[str, Any]], fallback: str = "") -> str:
        for candidate in (
            normalize_text((element or {}).get("text")),
            normalize_text((element or {}).get("aria_label")),
            normalize_text(((element or {}).get("attributes") or {}).get("placeholder")),
            normalize_text(((element or {}).get("attributes") or {}).get("name")),
        ):
            candidate = self._clean(candidate)
            if candidate and len(candidate) <= 40 and self._speakable(candidate):
                return candidate
        return fallback

    def _action_narration(self, action_type: str, label: str, dest_title: str) -> str:
        """One short spoken line describing the action, in plain language."""
        act = (action_type or "click").lower()

        if act in ("type", "fill", "input"):
            return f"Type into {label}." if label else "Fill in the highlighted field."

        if act in ("select", "check", "choose"):
            return f"Choose {label}." if label else "Pick an option here."

        if label and dest_title:
            return f"Select {label} to open {dest_title}."
        if label:
            return f"Select {label}."
        if dest_title:
            return f"That opens {dest_title}."
        return "Select the highlighted control."

    def _intro_narration(self, entity: str, headline: Optional[Dict[str, Any]]) -> str:
        # Page titles often repeat the headline verbatim; saying it twice sounds broken.
        name = self._short_entity(entity)
        if headline:
            text = self._clean(headline["text"]).rstrip(".")
            # Only add the headline if it says something the name does not.
            if text.lower() not in (name.lower(), (entity or "").strip().lower()) and len(text) > 4:
                return f"This is {name}. {text}."
        return f"Here's a walkthrough of {name}."

    def _outro_narration(self, entity: str, steps: int) -> str:
        # No step count here: any number not present in the cited facts is treated as
        # an unsupported claim and the scene gets dropped. Narration must only assert
        # what the source actually says.
        name = self._short_entity(entity)
        if steps:
            return f"That's the main path through {name}."
        return f"That's a walkthrough of {name}."

    # ── scene planning ───────────────────────────────────────────────────────

    def _focus_from(self, element: Optional[Dict[str, Any]]) -> Optional[Dict[str, float]]:
        """Element geometry in screenshot pixels, for the camera to push in on."""
        box = (element or {}).get("bounding_box") or {}
        try:
            w, h = float(box.get("width", 0)), float(box.get("height", 0))
            if w <= 0 or h <= 0:
                return None
            return {"x": float(box.get("x", 0)), "y": float(box.get("y", 0)), "w": w, "h": h}
        except (TypeError, ValueError):
            return None

    def _plan(
        self,
        explorer: Dict[str, Any],
        raw: Dict[str, Any],
        facts: List[Dict[str, Any]],
        by_id: Dict[str, Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Intro, then one scene per recorded action, then an outro."""
        shots = self._shot_index(explorer)
        titles = self._title_index(explorer)
        elements = self._element_index(raw)
        entity = explorer.get("entity") or "this product"

        by_page: Dict[str, List[Dict[str, Any]]] = {}
        for f in facts:
            by_page.setdefault(f.get("url") or "", []).append(f)

        def page_support(url: str, limit: int = 2) -> List[Dict[str, Any]]:
            """Facts that anchor a scene to its page when the action isn't a fact."""
            ranked = sorted(
                by_page.get(url, []),
                key=lambda f: {"heading": 0, "price": 1, "cta": 2}.get(f.get("kind"), 3),
            )
            return ranked[:limit]

        planned: List[Dict[str, Any]] = []

        # Intro: the product, in its own words.
        headline = next(
            (f for f in facts
             if f.get("kind") == "heading" and f.get("tag") == "h1" and len(f["text"]) > 8),
            None,
        ) or next(
            (f for f in facts if f.get("kind") == "heading" and len(f["text"]) > 12),
            None,
        )
        entry_url = explorer.get("target_url") or (explorer.get("pages") or [{}])[0].get("url", "")
        intro_shot = shots.get(entry_url) or next(iter(shots.values()), {})
        intro_facts = [f for f in ([headline] if headline else []) if f] or page_support(entry_url, 2)
        planned.append({
            "kind": "intro",
            "title": titles.get(entry_url) or entity,
            "narration": self._intro_narration(entity, headline),
            "facts": intro_facts,
            "visual": {
                "type": "screenshot" if intro_shot.get("artifact_path") else "none",
                "artifact_path": intro_shot.get("artifact_path"),
                "source_url": entry_url,
                "focus": None,
            },
        })

        # Action scenes, taken from the flows the crawler actually executed.
        seen_steps = set()
        for wf in (raw.get("workflows") or []):
            for step in (wf.get("steps") or []):
                if len(planned) - 1 >= MAX_ACTION_SCENES:
                    break
                action = step.get("action") or {}
                selector = action.get("css_selector")
                page_url = step.get("page_url") or ""
                if not selector or (page_url, selector) in seen_steps:
                    continue
                seen_steps.add((page_url, selector))

                element = elements.get((page_url, selector))
                label = self._label_for(element)
                dest = wf.get("end_url") or ""
                dest_title = titles.get(dest, "") if dest != page_url else ""

                # fact_id is a pure function of (url, selector), so the element the
                # user acted on can be cited exactly when it is part of the truth set.
                cited = []
                fid = fact_id(page_url, selector)
                if fid in by_id:
                    cited.append(by_id[fid])
                cited += [f for f in page_support(page_url, 2) if f["id"] not in {c["id"] for c in cited}]
                if not cited:
                    continue

                shot = shots.get(page_url) or {}
                planned.append({
                    "kind": "action",
                    "title": self._label_for(element, titles.get(page_url) or "Step"),
                    "narration": self._action_narration(action.get("action_type"), label, dest_title),
                    "facts": cited,
                    "visual": {
                        "type": "screenshot" if shot.get("artifact_path") else "none",
                        "artifact_path": shot.get("artifact_path"),
                        "source_url": page_url,
                        "focus": self._focus_from(element),
                        "action": action.get("action_type"),
                        "label": label,
                    },
                })

        # If the crawl recorded no usable actions, fall back to showing the pages it
        # reached - still grounded, just not a flow.
        if len(planned) == 1:
            for url, page_facts in list(by_page.items())[:3]:
                if not url or not page_facts:
                    continue
                shot = shots.get(url) or {}
                support = page_support(url, 2)
                planned.append({
                    "kind": "page",
                    "title": titles.get(url) or url,
                    "narration": f"{titles.get(url) or 'This page'} shows {support[0]['text']}."
                                 if support else f"Here is {titles.get(url) or url}.",
                    "facts": support,
                    "visual": {
                        "type": "screenshot" if shot.get("artifact_path") else "none",
                        "artifact_path": shot.get("artifact_path"),
                        "source_url": url,
                        "focus": None,
                    },
                })

        # Outro.
        action_count = sum(1 for p in planned if p["kind"] == "action")
        last_url = planned[-1]["visual"].get("source_url") or entry_url
        outro_shot = shots.get(last_url) or intro_shot
        outro_facts = page_support(last_url, 1) or intro_facts
        if outro_facts and len(planned) < MAX_SCENES:
            planned.append({
                "kind": "outro",
                "title": entity,
                "narration": self._outro_narration(entity, action_count),
                "facts": outro_facts,
                "visual": {
                    "type": "screenshot" if outro_shot.get("artifact_path") else "none",
                    "artifact_path": outro_shot.get("artifact_path"),
                    "source_url": last_url,
                    "focus": None,
                },
            })

        return planned[:MAX_SCENES]

    # ── media ────────────────────────────────────────────────────────────────

    def _render_video(
        self, scenes: List[Dict[str, Any]], workflow_id: str, store: MediaStore
    ) -> Optional[Dict[str, Any]]:
        from .video_render import render_demo_video, video_possible

        if not video_possible():
            print(f"  [{self.name}] ffmpeg/edge-tts unavailable - storyboard only.")
            return None

        def resolve(scene: Dict[str, Any]):
            rel = (scene.get("visual") or {}).get("artifact_path")
            if not rel:
                return None
            candidate = artifact_root() / rel
            return candidate if candidate.is_file() else None

        out = workflow_dir(workflow_id, "demo") / "demo.mp4"
        report = render_demo_video(scenes, artifact_root(), out, resolve)
        if not report.get("ok"):
            print(f"  [{self.name}] Video render failed: {report.get('reason')}")
            return None

        report["artifact_path"] = to_relative(report["path"])
        return report

    # ── storyboard ───────────────────────────────────────────────────────────

    def _storyboard_html(self, entity: str, target_url: str, scenes: List[Dict[str, Any]],
                         dropped: List[Dict[str, Any]]) -> str:
        def esc(v: Any) -> str:
            return html.escape(str(v or ""))

        cards = []
        for s in scenes:
            v = s["visual"]
            shot = v.get("artifact_path")
            img = (f'<img src="/artifacts/{esc(shot)}" alt="{esc(s["title"])}">'
                   if shot else '<div class="noshot">no screenshot captured</div>')
            act = v.get("action")
            tag = (f'<span class="act">{esc(act)}</span>' if act else
                   f'<span class="act act--{esc(s["kind"])}">{esc(s["kind"])}</span>')
            cites = "".join(
                f'<li><code>{esc(f["id"])}</code> <span class="k">{esc(f["kind"])}</span> '
                f'&ldquo;{esc(f["text"][:80])}&rdquo;</li>'
                for f in s["_facts"]
            )
            cards.append(f"""
    <article class="scene">
      <header><span class="num">{s['index']}</span><h2>{esc(s['title'])}</h2>
        {tag}<span class="badge ok">&#10003; verified</span></header>
      <div class="shot">{img}</div>
      <p class="narration">{esc(s['narration'])}</p>
      <details><summary>Sources ({len(s['_facts'])})</summary><ul class="cites">{cites}</ul></details>
      <div class="url">{esc(v.get('source_url'))}</div>
    </article>""")

        dropped_html = ""
        if dropped:
            items = "".join(
                f"<li>{esc(d['narration'])} &mdash; <em>{esc(d['reason'])}</em></li>" for d in dropped
            )
            dropped_html = f"""
  <section class="dropped">
    <h3>&#9888; {len(dropped)} scene(s) rejected before shipping</h3>
    <p>These made claims the crawled source does not support, so they were removed.</p>
    <ul>{items}</ul>
  </section>"""

        return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(entity)} &mdash; verified demo storyboard</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:'Instrument Sans',system-ui,sans-serif;background:#f6f6f6;color:#1a1a1a;
padding:40px 20px;line-height:1.5}}
.wrap{{max-width:920px;margin:0 auto}}
h1{{font-size:34px;font-weight:600;letter-spacing:-.02em;margin-bottom:6px}}
.sub{{color:#666;font-size:15px;margin-bottom:8px}}
.meta{{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:28px}}
.pill{{background:#fff;border:1px solid #e5e5e5;border-radius:99px;padding:5px 12px;font-size:12px;color:#444}}
.scene{{background:#fff;border:1px solid #e8e8e8;border-radius:16px;padding:20px;margin-bottom:16px;
box-shadow:0 1px 2px rgba(0,0,0,.03),0 8px 20px -12px rgba(0,0,0,.15)}}
.scene header{{display:flex;align-items:center;gap:10px;margin-bottom:12px;flex-wrap:wrap}}
.num{{width:26px;height:26px;border-radius:50%;background:#4043FF;color:#fff;display:flex;
align-items:center;justify-content:center;font-size:13px;font-weight:600;flex-shrink:0}}
.scene h2{{font-size:17px;font-weight:600;flex:1}}
.act{{font-size:11px;font-weight:600;padding:3px 9px;border-radius:99px;background:rgba(64,67,255,.1);color:#4043FF}}
.act--intro,.act--outro,.act--page{{background:#f0f0f0;color:#777}}
.badge{{font-size:11px;font-weight:600;padding:3px 9px;border-radius:99px}}
.badge.ok{{background:rgba(34,197,94,.1);color:#16a34a}}
.shot img{{width:100%;border-radius:10px;border:1px solid #eee;display:block;max-height:280px;object-fit:cover;object-position:top}}
.noshot{{padding:28px;text-align:center;color:#aaa;font-size:13px;background:#fafafa;border-radius:10px}}
.narration{{font-size:16px;margin:14px 0 10px;color:#222}}
details{{font-size:13px;color:#555}} summary{{cursor:pointer;color:#4043FF;font-weight:500}}
.cites{{list-style:none;margin-top:8px}} .cites li{{padding:4px 0;border-top:1px solid #f0f0f0}}
code{{background:#f2f2f7;padding:1px 5px;border-radius:4px;font-size:11px}}
.k{{color:#888;font-size:11px;margin:0 4px}}
.url{{font-size:11px;color:#aaa;margin-top:10px;font-family:ui-monospace,monospace;word-break:break-all}}
.dropped{{background:#fff5f5;border:1px solid #fecaca;border-radius:14px;padding:18px;margin-top:22px}}
.dropped h3{{font-size:15px;color:#b91c1c;margin-bottom:6px}}
.dropped p{{font-size:13px;color:#7f1d1d;margin-bottom:8px}}
.dropped ul{{font-size:13px;color:#7f1d1d;padding-left:18px}}
</style></head><body><div class="wrap">
  <h1>{esc(entity)}</h1>
  <p class="sub">Each scene follows a recorded user action and cites the page elements it asserts.</p>
  <div class="meta">
    <span class="pill">{len(scenes)} verified scenes</span>
    <span class="pill">{sum(1 for s in scenes if s['kind'] == 'action')} user actions</span>
    <span class="pill">{len(dropped)} rejected</span>
    <span class="pill">source: {esc(target_url)}</span>
  </div>
{''.join(cards)}
{dropped_html}
</div></body></html>"""

    # ── main ─────────────────────────────────────────────────────────────────

    def run(self, context: Dict[str, Any]) -> Dict[str, Any]:
        workflow_id = context.get("workflow_id", "adhoc")
        explorer = context.get("explorer_output") or {}
        facts = load_truth_set(explorer)
        entity = explorer.get("entity") or context.get("goal", "Demo")
        target_url = explorer.get("target_url") or ""

        if not facts:
            print(f"  [{self.name}] No truth set available - cannot build a cited demo.")
            return {"demo_output": {
                "scenes": [], "scenes_dropped": 0, "media_mode": "none",
                "generated_by": "none",
                "note": "no crawl facts available; nothing could be asserted truthfully",
            }}

        raw = self._load_raw(explorer)
        by_id = index_by_id(facts)
        store = MediaStore(workflow_id)
        registry = DependencyRegistry.load(workflow_id)

        print(f"  [{self.name}] Planning scenes from {len(facts)} facts "
              f"and {len(raw.get('workflows') or [])} recorded flow(s)...")

        scenes: List[Dict[str, Any]] = []
        dropped: List[Dict[str, Any]] = []

        for plan in self._plan(explorer, raw, facts, by_id):
            narration, cited = plan["narration"], plan["facts"]

            # The guard: a scene that cannot be verified against its own citations
            # never reaches the storyboard or the video.
            check = verify_claim(narration, cited)
            if not check["verified"]:
                dropped.append({"narration": narration, "reason": check["reason"]})
                continue

            idx = len(scenes) + 1
            scenes.append({
                "index": idx,
                "kind": plan["kind"],
                "title": plan["title"],
                "narration": narration,
                "asserts": [f["id"] for f in cited],
                "visual": plan["visual"],
                "_facts": cited,
            })
            registry.declare(
                f"demo#scene{idx}", "demo", [f["id"] for f in cited],
                {"kind": plan["kind"], "title": plan["title"],
                 "url": plan["visual"].get("source_url")},
            )

        video = self._render_video(scenes, workflow_id, store) if scenes else None
        media_mode = "video" if video else "storyboard"

        storyboard = self._storyboard_html(entity, target_url, scenes, dropped)
        out_dir = workflow_dir(workflow_id, "demo")
        sb_path = out_dir / "storyboard.html"
        sb_path.write_text(storyboard, encoding="utf-8")

        public_scenes = [{k: v for k, v in s.items() if not k.startswith("_")} for s in scenes]
        (out_dir / "scenes.json").write_text(
            json.dumps(public_scenes, indent=2, ensure_ascii=False), encoding="utf-8")
        registry.save()

        actions = sum(1 for s in scenes if s["kind"] == "action")
        detail = (f"video {video['duration_sec']}s, {video['scenes_focused']} focused"
                  if video else f"media={media_mode}")
        print(f"  [{self.name}] {len(scenes)} scenes ({actions} user actions), "
              f"{len(dropped)} rejected, {detail}.")

        return {"demo_output": {
            "scenes": public_scenes,
            "action_scenes": actions,
            "scenes_dropped": len(dropped),
            "dropped": dropped,
            "artifact_path": to_relative(str(sb_path)),
            "scenes_artifact_path": to_relative(str(out_dir / "scenes.json")),
            "media_mode": media_mode,
            "video": video,
            "video_artifact_path": (video or {}).get("artifact_path"),
            "generated_by": "deterministic",
            "line_count": len(storyboard.splitlines()),
        }}


if __name__ == "__main__":  # pragma: no cover - manual smoke test
    import tempfile
    from pathlib import Path

    ok = fail = 0

    def check(name: str, cond: bool, detail: str = "") -> None:
        global ok, fail
        if cond:
            ok += 1
            print(f"  PASS  {name}")
        else:
            fail += 1
            print(f"  FAIL  {name}  {detail}")

    URL = "https://x.test/"
    SEL = "html > body > a"

    facts = [
        {"id": fact_id(URL, "h1"), "url": URL, "selector": "h1", "tag": "h1",
         "kind": "heading", "text": "Simple pricing", "sha256": "a"},
        {"id": fact_id(URL, SEL), "url": URL, "selector": SEL, "tag": "a",
         "kind": "cta", "text": "Learn more", "sha256": "b"},
    ]

    raw = {
        "pages": [{"url": URL, "elements": [
            {"css_selector": SEL, "text": "Learn more", "tag_name": "a",
             "bounding_box": {"x": 200, "y": 400, "width": 120, "height": 30}},
        ]}],
        "workflows": [{"name": "flow", "start_url": URL, "end_url": "https://x.test/docs",
                       "steps": [{"step_number": 1, "page_url": URL,
                                  "action": {"action_type": "click", "css_selector": SEL}}]}],
    }

    tmp = Path(tempfile.mkdtemp(prefix="adip_demo_"))
    rawfile = tmp / "raw.json"
    rawfile.write_text(json.dumps(raw), encoding="utf-8")

    ctx = {
        "workflow_id": "wf-demo-selftest",
        "explorer_output": {
            "entity": "X Test", "target_url": URL, "facts": facts,
            "raw_result_file": str(rawfile),
            "pages": [{"url": URL, "title": "Home"},
                      {"url": "https://x.test/docs", "title": "Docs"}],
            "screenshots": [],
        },
    }

    out = DemoAgent().run(ctx)["demo_output"]
    kinds = [s["kind"] for s in out["scenes"]]
    narrs = [s["narration"] for s in out["scenes"]]
    print("   scenes:", kinds)
    for n in narrs:
        print("     -", n)

    check("scenes produced", len(out["scenes"]) >= 2)
    check("follows recorded action", "action" in kinds, str(kinds))
    check("has intro", kinds[0] == "intro", str(kinds))
    check("action narration is natural",
          any("Select Learn more" in n for n in narrs), str(narrs))
    check("no dumped element lists", not any("you'll see" in n for n in narrs))
    check("action scene carries focus geometry",
          any((s["visual"].get("focus") or {}).get("w") == 120 for s in out["scenes"]))
    check("every scene cites facts", all(s["asserts"] for s in out["scenes"]))
    known = {f["id"] for f in facts}
    check("no dangling citations", all(set(s["asserts"]) <= known for s in out["scenes"]))
    by = {f["id"]: f for f in facts}
    check("all shipped scenes verify",
          all(verify_claim(s["narration"], [by[i] for i in s["asserts"]])["verified"]
              for s in out["scenes"]))
    check("action count reported", out["action_scenes"] >= 1)

    empty = DemoAgent().run({"workflow_id": "wf-demo-selftest2"})["demo_output"]
    check("degrades with no facts", empty["scenes"] == [] and empty["media_mode"] == "none")

    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{ok} passed, {fail} failed")
    raise SystemExit(1 if fail else 0)
