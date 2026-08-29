"""Exercise the Veridemo MCP tools directly, including the paths that must fail."""
import json, os, sys

os.environ["ADIP_ARTIFACT_DIR"] = r"D:\veridemo-zerops\.artifacts"
sys.path.insert(0, r"D:\veridemo-zerops\harness")

import veridemo_mcp as _v  # noqa: E402

# @mcp.tool wraps each function in a FunctionTool; .fn is the original callable.
_unwrap = lambda t: getattr(t, "fn", t)
crawl_product = _unwrap(_v.crawl_product)
list_facts = _unwrap(_v.list_facts)
check_narration = _unwrap(_v.check_narration)
review_script = _unwrap(_v.review_script)
publish_demo = _unwrap(_v.publish_demo)

def show(label, raw):
    print(f"\n--- {label} ---")
    d = json.loads(raw)
    print(json.dumps(d, indent=2)[:900])
    return d

SID = "test"
ok = fail = 0
def check(name, cond, detail=""):
    global ok, fail
    if cond: ok += 1; print(f"  PASS  {name}")
    else: fail += 1; print(f"  FAIL  {name}  {detail}")

print("=" * 74)
print("1. tools must refuse to work before a crawl")
print("=" * 74)
d = show("list_facts (no crawl)", list_facts(session_id=SID))
check("list_facts refuses without crawl", d["ok"] is False)
d = show("publish_demo (no crawl)", publish_demo(session_id=SID))
check("publish refuses without crawl", d["ok"] is False)

print()
print("=" * 74)
print("2. bad URL is rejected, not crawled")
print("=" * 74)
d = show("crawl_product('not a url')", crawl_product("banana", session_id=SID))
check("bad url rejected", d["ok"] is False)

print()
print("=" * 74)
print("3. real read-only crawl")
print("=" * 74)
d = show("crawl_product(local page)", crawl_product("http://127.0.0.1:8098/index.html", session_id=SID))
check("crawl succeeded", d.get("ok") is True, str(d)[:200])
check("mode is read-only by default", d.get("mode") == "read-only", str(d.get("mode")))
check("facts captured", (d.get("facts_captured") or 0) > 0)
check("no errors on target", d.get("errors_detected") == 0)

facts = json.loads(list_facts(session_id=SID, kind="price", limit=5))
show("list_facts(kind=price)", json.dumps(facts))
check("price facts listed", facts["ok"] and facts["total"] >= 1)
price = facts["facts"][0]
print(f"\n  using price fact: {price['id']} = {price['text']!r}")

print()
print("=" * 74)
print("4. THE GUARD — a true line passes, a hallucinated one is rejected")
print("=" * 74)
true_line = f"Pricing starts at {price['text']}."
d = show("check_narration (truthful)", check_narration(true_line, [price["id"]], session_id=SID))
check("truthful line accepted", d["accepted"] is True, d.get("reason"))

d = show("check_narration (invented number)",
         check_narration("It costs $4999 per seat.", [price["id"]], session_id=SID))
check("hallucinated number REJECTED", d["accepted"] is False)
check("names the bad number", "4999" in (d.get("unsupported_numbers") or []), str(d.get("unsupported_numbers")))

d = show("check_narration (no citation)", check_narration("It is the best tool ever.", [], session_id=SID))
check("uncited line REJECTED", d["accepted"] is False)

d = show("check_narration (fake fact id)",
         check_narration("Pricing is shown.", ["f_totally_made_up"], session_id=SID))
check("dangling citation REJECTED", d["accepted"] is False)
check("names the unknown id", "f_totally_made_up" in (d.get("unknown_ids") or []))

print()
print("=" * 74)
print("5. only accepted lines reach the package")
print("=" * 74)
d = show("review_script", review_script(session_id=SID))
check("exactly 1 accepted line kept", d["verified_scenes"] == 1, str(d["verified_scenes"]))
d = show("publish_demo", publish_demo(session_id=SID, title="Veridemo"))
check("publish succeeded", d["ok"] is True)
check("published only the verified line", d["scenes_published"] == 1, str(d.get("scenes_published")))

print(f"\n{'=' * 74}\n{ok} passed, {fail} failed\n{'=' * 74}")
sys.exit(1 if fail else 0)
