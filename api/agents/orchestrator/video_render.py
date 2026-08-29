"""Renders demo scenes into an MP4.

A product demo is about *what the user does*, so scenes carry an optional focus region -
the element being clicked or typed into. Those scenes push in on that element and mark
it, instead of drifting across a full-page screenshot. Scenes without a focus (intro and
outro) get a gentle wide move.

Narration is Edge TTS and compositing is FFmpeg, so rendering needs no API keys and no
network beyond the TTS call.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path
from typing import Any, Dict, List, Optional

WIDTH, HEIGHT, FPS = 1280, 720, 30
VOICE = "en-US-GuyNeural"
MIN_SCENE_SEC = 3.0
CAPTION_WIDTH = 56

# Rendering happens on an upscaled copy so pushing in stays sharp.
SUPERSAMPLE = 2

HIGHLIGHT = (64, 67, 255)


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def tts_available() -> bool:
    try:
        import edge_tts  # noqa: F401
        return True
    except Exception:
        return False


def pillow_available() -> bool:
    try:
        import PIL  # noqa: F401
        return True
    except Exception:
        return False


def video_possible() -> bool:
    """True if a real MP4 can actually be produced right now."""
    return ffmpeg_available() and tts_available()


def _run(cmd: List[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed: {(proc.stderr or '')[-400:]}")


def _probe_duration(path: Path) -> float:
    """Audio length decides scene length, so a caption never outruns the voice."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True,
        )
        return max(float(out.stdout.strip()), MIN_SCENE_SEC)
    except Exception:
        return MIN_SCENE_SEC


async def _synthesize(text: str, out_path: Path) -> None:
    import edge_tts
    await edge_tts.Communicate(text, VOICE).save(str(out_path))


def _narrate(text: str, out_path: Path) -> Optional[float]:
    try:
        asyncio.run(_synthesize(text, out_path))
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_synthesize(text, out_path))
        finally:
            loop.close()
    except Exception as exc:
        print(f"  [video] TTS failed: {exc}")
        return None
    return _probe_duration(out_path) if out_path.is_file() else None


def _prepare_frame(src: Path, dst: Path, focus: Optional[Dict[str, float]]) -> Optional[Dict[str, float]]:
    """Normalise the screenshot to a fixed canvas and mark the focused element.

    Returns the focus centre in canvas pixels, which is what the zoom expression
    needs. Drawing the marker here (rather than in FFmpeg) keeps it pinned to the
    element while the camera moves.
    """
    from PIL import Image, ImageDraw

    canvas_w = WIDTH * SUPERSAMPLE
    img = Image.open(src).convert("RGB")

    # Tall full-page captures would shrink the actual UI to nothing; keep the top,
    # which is the part a demo is about.
    max_h = int(canvas_w * 1.6)
    if img.height > max_h:
        img = img.crop((0, 0, img.width, max_h))

    scale = canvas_w / img.width
    img = img.resize((canvas_w, max(1, int(img.height * scale))), Image.LANCZOS)

    centre = None
    if focus:
        x = float(focus.get("x", 0)) * scale
        y = float(focus.get("y", 0)) * scale
        w = max(float(focus.get("w", 0)) * scale, 8)
        h = max(float(focus.get("h", 0)) * scale, 8)

        if 0 <= y <= img.height:
            draw = ImageDraw.Draw(img, "RGBA")
            pad = 10
            box = (x - pad, y - pad, x + w + pad, y + h + pad)
            draw.rectangle(box, fill=(*HIGHLIGHT, 38))
            for i in range(4):  # thicker outline than a 1px rectangle gives
                draw.rectangle(
                    (box[0] - i, box[1] - i, box[2] + i, box[3] + i),
                    outline=(*HIGHLIGHT, 230),
                )
            centre = {"cx": x + w / 2, "cy": y + h / 2}

    img.save(dst)
    return centre


def _placeholder(path: Path, title: str) -> None:
    """A scene without a screenshot still needs a frame, or the concat breaks."""
    safe = title.replace(":", r"\:").replace("'", "")[:60]
    _run([
        "ffmpeg", "-y", "-f", "lavfi",
        "-i", f"color=c=0x0f1117:s={WIDTH}x{HEIGHT}",
        "-vf", f"drawtext=text='{safe}':fontcolor=white:fontsize=44:x=(w-text_w)/2:y=(h-text_h)/2",
        "-frames:v", "1", str(path),
    ])


def _caption_filter(text: str) -> str:
    """Wrapped, bottom-anchored caption drawn onto the frame."""
    lines = textwrap.wrap(text, CAPTION_WIDTH)[:3]
    if not lines:
        return "null"
    parts = []
    base_y = HEIGHT - 34 - (len(lines) * 44)
    for i, line in enumerate(lines):
        safe = (line.replace("\\", "").replace(":", r"\:").replace("'", "")
                    .replace("%", "").replace(",", r"\,"))
        parts.append(
            f"drawtext=text='{safe}':fontcolor=white:fontsize=29:"
            f"box=1:boxcolor=black@0.66:boxborderw=13:"
            f"x=(w-text_w)/2:y={base_y + i * 44}"
        )
    return ",".join(parts)


def _motion_filter(duration: float, centre: Optional[Dict[str, float]], index: int) -> str:
    """Camera move for one scene.

    With a focus centre the camera pushes in on that element; otherwise it drifts
    slowly so consecutive wide shots don't look static.
    """
    frames = max(int(duration * FPS), int(MIN_SCENE_SEC * FPS))

    if centre:
        # Ease from wide to close so the element being used is unmistakable.
        z = f"min(1+1.15*on/{frames},2.15)"
        x = f"'{centre['cx']:.0f}/{SUPERSAMPLE}-(iw/zoom/2)'"
        y = f"'{centre['cy']:.0f}/{SUPERSAMPLE}-(ih/zoom/2)'"
    else:
        z = f"1.0+0.0007*on" if index % 2 == 0 else f"1.10-0.0007*on"
        x = "'iw/2-(iw/zoom/2)'"
        y = "'ih/2-(ih/zoom/2)'"

    return (
        f"scale={WIDTH * SUPERSAMPLE}:-2,"
        f"zoompan=z='{z}':d={frames}:s={WIDTH}x{HEIGHT}:fps={FPS}:x={x}:y={y}"
    )


def _render_scene(
    image: Path,
    audio: Optional[Path],
    duration: float,
    caption: str,
    out_path: Path,
    index: int,
    centre: Optional[Dict[str, float]],
) -> None:
    vf = f"{_motion_filter(duration, centre, index)},{_caption_filter(caption)},format=yuv420p"

    cmd = ["ffmpeg", "-y", "-loop", "1", "-i", str(image)]
    if audio and audio.is_file():
        cmd += ["-i", str(audio)]
    cmd += ["-vf", vf, "-t", f"{duration:.2f}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-r", str(FPS)]
    if audio and audio.is_file():
        cmd += ["-c:a", "aac", "-b:a", "128k", "-shortest"]
    else:
        cmd += ["-an"]
    cmd += [str(out_path)]
    _run(cmd)


def render_demo_video(
    scenes: List[Dict[str, Any]],
    artifact_root: Path,
    out_path: Path,
    resolve_screenshot,
) -> Dict[str, Any]:
    """Render scenes to an MP4.

    `resolve_screenshot(scene)` returns a Path to that scene's image, or None.
    A scene may carry `visual.focus` = {x, y, w, h} in screenshot pixel coordinates
    to push in on the element it is about.

    Returns a report describing what was actually produced - it never claims a video
    that was not written.
    """
    if not scenes:
        return {"ok": False, "reason": "no scenes to render"}
    if not ffmpeg_available():
        return {"ok": False, "reason": "ffmpeg not installed"}

    work = Path(tempfile.mkdtemp(prefix="adip_render_"))
    clips: List[Path] = []
    narrated = focused = 0
    total = 0.0

    try:
        for i, scene in enumerate(scenes):
            narration = (scene.get("narration") or "").strip() or scene.get("title") or ""

            audio_path = work / f"a{i}.mp3"
            duration = _narrate(narration, audio_path) if (narration and tts_available()) else None
            if duration:
                narrated += 1
            else:
                audio_path = None
                duration = max(MIN_SCENE_SEC, len(narration.split()) / 2.6)

            visual = scene.get("visual") or {}
            focus = visual.get("focus")
            src = resolve_screenshot(scene)

            centre = None
            frame = work / f"f{i}.png"
            if src and Path(src).is_file() and pillow_available():
                try:
                    centre = _prepare_frame(Path(src), frame, focus)
                    if centre:
                        focused += 1
                except Exception as exc:
                    print(f"  [video] frame prep failed ({exc}); using raw screenshot")
                    frame = Path(src)
            elif src and Path(src).is_file():
                frame = Path(src)
            else:
                _placeholder(frame, scene.get("title") or f"Scene {i + 1}")

            clip = work / f"s{i}.mp4"
            _render_scene(frame, audio_path, duration, narration, clip, i, centre)
            clips.append(clip)
            total += duration

        if not clips:
            return {"ok": False, "reason": "no clips rendered"}

        listing = work / "clips.txt"
        listing.write_text("".join(f"file '{c.as_posix()}'\n" for c in clips), encoding="utf-8")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        _run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listing),
              "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
              "-c:a", "aac", "-b:a", "128k", str(out_path)])

        if not out_path.is_file():
            return {"ok": False, "reason": "ffmpeg produced no output file"}

        return {
            "ok": True,
            "path": str(out_path),
            "scenes_rendered": len(clips),
            "scenes_narrated": narrated,
            "scenes_focused": focused,
            "duration_sec": round(total, 1),
            "bytes": out_path.stat().st_size,
            "voice": VOICE if narrated else None,
            "narration_engine": "edge-tts" if narrated else "none",
        }
    except Exception as exc:
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":  # pragma: no cover - manual smoke test
    print(f"ffmpeg={ffmpeg_available()} edge-tts={tts_available()} pillow={pillow_available()}")
    from PIL import Image, ImageDraw

    work = Path(tempfile.mkdtemp(prefix="adip_vr_"))
    shot = work / "page.png"
    im = Image.new("RGB", (1280, 900), (245, 245, 247))
    d = ImageDraw.Draw(im)
    d.rectangle((80, 60, 1200, 150), fill=(255, 255, 255))
    d.rectangle((480, 380, 800, 450), fill=(64, 67, 255))   # the "button"
    im.save(shot)

    scenes = [
        {"index": 1, "title": "Overview", "narration": "This is the product overview.",
         "visual": {}},
        {"index": 2, "title": "Click Get Started",
         "narration": "Click Get Started to create your first project.",
         "visual": {"focus": {"x": 480, "y": 380, "w": 320, "h": 70}}},
    ]
    out = work / "out.mp4"
    report = render_demo_video(scenes, work, out, lambda s: shot)
    print(json.dumps(report, indent=2))
    ok = report.get("ok") and report.get("scenes_focused") == 1
    print("focus zoom applied:", report.get("scenes_focused"))
    shutil.rmtree(work, ignore_errors=True)
    raise SystemExit(0 if ok else 1)
