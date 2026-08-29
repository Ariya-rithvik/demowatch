"""Exercise the Veridemo MCP tools the way the harness actually calls them.

Every call here goes through `Tool.run(...)` rather than the underlying function.
That distinction is the whole point of this file: an earlier version called the
functions directly, which runs them with no event loop, and it passed 17/17 while
the server was completely non-functional over its own transport - the crawl used
`asyncio.run` inside a sync tool, which FastMCP invokes on the running loop, so
every real call raised and dropped the coroutine. A test that bypasses the
transport cannot see that class of bug.

Run:  python harness/tests/test_veridemo_mcp.py [target_url]
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("ADIP_ARTIFACT_DIR", str(Path(__file__).resolve().parents[2] / ".artifacts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import veridemo_mcp as v  # noqa: E402

TARGET = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8098/index.html"
SID = "selftest"

_ok = _fail = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _ok, _fail
    if cond:
        _ok += 1
        print(f"  PASS  {name}")
    else:
        _fail += 1
        print(f"  FAIL  {name}  {detail}")


async def call(tool, **kwargs):
    """Invoke a tool exactly as the MCP transport does, and decode its JSON."""
    result = await tool.run(kwargs)
    text = result.content[0].text if hasattr(result, "content") else str(result)
    return json.loads(text)


async def main() -> int:
    print("=" * 74)
    print("1. tools refuse to work before a crawl")
    print("=" * 74)
    d = await call(v.list_facts, session_id=SID)
    check("list_facts refuses without a crawl", d["ok"] is False)
    d = await call(v.publish_demo, session_id=SID)
    check("publish refuses without a crawl", d["ok"] is False)

    print()
    print("=" * 74)
    print("2. bad input is rejected as data, not raised")
    print("=" * 74)
    d = await call(v.crawl_product, url="banana", session_id=SID)
    check("unusable url rejected", d["ok"] is False)
    d = await call(v.crawl_product, url="..", session_id="..")
    check("path-shaped session id does not escape as an exception", d["ok"] is False)

    print()
    print("=" * 74)
    print("3. a real read-only crawl, over the transport")
    print("=" * 74)
    d = await call(v.crawl_product, url=TARGET, session_id=SID)
    check("crawl succeeded over MCP transport", d.get("ok") is True, str(d)[:160])
    if not d.get("ok"):
        return 1
    check("session was actually stored", SID in v._SESSIONS)
    check("mode is read-only by default", d.get("mode") == "read-only")
    check("facts captured", (d.get("facts_captured") or 0) > 0)

    facts = v._SESSIONS[SID]["facts"]
    quotable = next(f for f in facts if len(f["text"]) > 8)

    print()
    print("=" * 74)
    print("4. the guard")
    print("=" * 74)
    d = await call(v.check_narration, text=quotable["text"][:60],
                   fact_ids=[quotable["id"]], session_id=SID)
    check("line quoting the page is accepted", d["accepted"] is True, d.get("reason"))

    d = await call(v.check_narration, text="It costs $4999 per seat.",
                   fact_ids=[quotable["id"]], session_id=SID)
    check("invented number rejected", d["accepted"] is False)
    check("the bad number is named", "4999" in (d.get("unsupported_numbers") or []))

    d = await call(v.check_narration, text="It is the best tool ever.",
                   fact_ids=[], session_id=SID)
    check("uncited line rejected", d["accepted"] is False)

    d = await call(v.check_narration, text="Something true.",
                   fact_ids=["f_not_a_real_id"], session_id=SID)
    check("dangling citation rejected", d["accepted"] is False)
    check("the unknown id is named", "f_not_a_real_id" in (d.get("unknown_ids") or []))

    print()
    print("=" * 74)
    print("5. the title cannot smuggle an unchecked claim past the guard")
    print("=" * 74)
    d = await call(v.publish_demo, session_id=SID,
                   title="99.99% uptime, HIPAA certified, only $9/mo")
    check("agent-authored title rejected", d["ok"] is False, str(d)[:120])
    check("rejection names what was allowed", bool(d.get("allowed")))

    print()
    print("=" * 74)
    print("6. only accepted lines ship")
    print("=" * 74)
    d = await call(v.review_script, session_id=SID)
    check("exactly one line accepted", d["verified_scenes"] == 1, str(d["verified_scenes"]))
    d = await call(v.publish_demo, session_id=SID)
    check("publish succeeded with no title", d["ok"] is True, str(d)[:120])
    check("published only the verified line", d.get("scenes_published") == 1)

    print(f"\n{'=' * 74}\n{_ok} passed, {_fail} failed\n{'=' * 74}")
    return 1 if _fail else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
