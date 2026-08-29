"""Register the Veridemo MCP server and agent with a running TrueForge.

One command so a reviewer can reproduce the whole harness setup:

    python harness/setup_trueforge.py

Both registrations use PUT, so running it twice is not an error - it replaces what
is there. The script reports what is still missing (a model provider, a sandbox)
rather than pretending the setup is complete, because those need credentials that
do not belong in a repo.

Environment:
  TRUEFORGE_URL     default http://localhost:8790
  VERIDEMO_MCP_URL  default http://127.0.0.1:9077/mcp
                    Set this to the Windows host IP if TrueForge runs in WSL and
                    the MCP server runs on Windows - inside WSL, localhost is WSL.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

TRUEFORGE = os.getenv("TRUEFORGE_URL", "http://localhost:8790").rstrip("/")
MCP_URL = os.getenv("VERIDEMO_MCP_URL", "http://127.0.0.1:9077/mcp")
AGENT_FILE = Path(__file__).resolve().parent / "trueforge" / "agent.json"

MCP_NAME = "veridemo"
MCP_DESCRIPTION = (
    "Crawl a product page, list the facts it states, check narration against those "
    "facts, and publish only verified claims."
)


def _request(method: str, path: str, payload: Optional[Dict[str, Any]] = None,
             timeout: int = 30) -> Tuple[int, Any]:
    """Return (status, parsed body). HTTP errors are values here, not exceptions."""
    url = f"{TRUEFORGE}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode()
            return resp.status, (json.loads(body) if body else None)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode()
        try:
            return exc.code, json.loads(body)
        except ValueError:
            return exc.code, body
    except urllib.error.URLError as exc:
        return 0, f"{exc.reason}"


def _ok(msg: str) -> None:
    print(f"  ok      {msg}")


def _warn(msg: str) -> None:
    print(f"  todo    {msg}")


def _fail(msg: str) -> None:
    print(f"  FAILED  {msg}")


def check_harness() -> bool:
    status, _ = _request("GET", "/api/v1/models", timeout=10)
    if status == 0:
        _fail(f"no TrueForge at {TRUEFORGE} - start it with: npx @truefoundry/trueforge@latest")
        return False
    _ok(f"TrueForge reachable at {TRUEFORGE}")
    return True


def register_mcp() -> bool:
    status, body = _request("PUT", "/api/v1/settings/mcp-servers", {
        "manifest": {
            "type": "remote",
            "name": MCP_NAME,
            "url": MCP_URL,
            "description": MCP_DESCRIPTION,
        }
    })
    if status not in (200, 201):
        _fail(f"could not register MCP server: {status} {body}")
        return False
    _ok(f"MCP server '{MCP_NAME}' -> {MCP_URL}")
    return True


def verify_tools() -> bool:
    """Discovery is the real check: it proves TrueForge can reach the server."""
    status, body = _request("GET", f"/api/v1/mcp-servers/{MCP_NAME}/tools", timeout=45)
    if status != 200:
        _fail(f"TrueForge cannot reach the MCP server at {MCP_URL} ({status})")
        print("          If TrueForge runs in WSL, localhost is WSL, not Windows.")
        print("          Set VERIDEMO_MCP_URL to the Windows host IP, e.g.")
        print("          http://$(ip route show default | awk '{print $3}'):9077/mcp")
        return False

    tools = (body or {}).get("data") or []
    gated = [t["name"] for t in tools if (t.get("annotations") or {}).get("destructiveHint")]
    _ok(f"{len(tools)} tools discovered: {', '.join(t['name'] for t in tools)}")
    if gated:
        _ok(f"marked destructive, so the harness will hold for approval: {', '.join(gated)}")
    else:
        _warn("no tool is marked destructive - the approval gate will not trigger")
    return True


def register_agent() -> bool:
    if not AGENT_FILE.is_file():
        _fail(f"agent manifest missing at {AGENT_FILE}")
        return False
    spec = json.loads(AGENT_FILE.read_text(encoding="utf-8"))
    name = spec["name"]
    status, body = _request("PUT", f"/api/v1/agents/{name}", {"manifest": spec["manifest"]})
    if status not in (200, 201):
        # PUT may not exist on this build; fall back to create.
        status, body = _request("POST", "/api/v1/agents", spec)
    if status not in (200, 201):
        _fail(f"could not register agent '{name}': {status} {body}")
        return False
    _ok(f"agent '{name}' registered ({spec['manifest']['model']['name']})")
    return True


def report_prerequisites() -> None:
    """Say plainly what still needs credentials. These cannot live in the repo."""
    status, body = _request("GET", "/api/v1/settings/model-providers", timeout=10)
    providers = (body or {}).get("data") or [] if status == 200 else []
    if providers:
        _ok(f"model provider configured ({len(providers)})")
    else:
        _warn("no model provider - add a Google Gemini API key in the TrueForge UI")

    status, body = _request("GET", "/api/v1/settings/sandbox-providers", timeout=10)
    if status == 200 and body and not (isinstance(body, dict) and body.get("error")):
        _ok("sandbox provider configured")
    else:
        _warn("no sandbox - configure Daytona, or for the local sandbox install:")
        print("          sudo apt-get install -y bubblewrap socat ripgrep")


def main() -> int:
    print(f"Setting up TrueForge at {TRUEFORGE}\n")
    if not check_harness():
        return 1

    steps_ok = register_mcp()
    if steps_ok:
        steps_ok = verify_tools() and steps_ok
    steps_ok = register_agent() and steps_ok

    print()
    report_prerequisites()
    print(f"\nOpen {TRUEFORGE} and start a session with the 'veridemo' agent.")
    return 0 if steps_ok else 1


if __name__ == "__main__":
    sys.exit(main())
