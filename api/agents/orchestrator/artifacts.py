"""Shared artifact storage for agent outputs.

Agents historically wrote straight into the system temp directory. Serving those
paths over HTTP would mean accepting an arbitrary filesystem path from a client,
so everything an agent produces is instead written under a single artifact root
that the API can safely serve from after validating containment.
"""

import os
import re
import tempfile
from pathlib import Path
from typing import Optional

_DEFAULT_ROOT = Path(tempfile.gettempdir()) / "adip_artifacts"

_SAFE_ID = re.compile(r"[^A-Za-z0-9._-]")


def artifact_root() -> Path:
    """Root directory containing every generated artifact."""
    root = Path(os.getenv("ADIP_ARTIFACT_DIR") or _DEFAULT_ROOT)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _safe(component: str) -> str:
    """Reduce an identifier to a single safe path segment."""
    return _SAFE_ID.sub("_", str(component or "adhoc"))[:80] or "adhoc"


def workflow_dir(workflow_id: str, sub: Optional[str] = None) -> Path:
    """Per-workflow artifact directory, created on demand."""
    d = artifact_root() / _safe(workflow_id)
    if sub:
        d = d / _safe(sub)
    d.mkdir(parents=True, exist_ok=True)
    return d


def resolve_within_root(candidate: str) -> Optional[Path]:
    """Resolve `candidate` and return it only if it lives inside the artifact root.

    Returns None for anything outside the root, so a caller-supplied path can
    never escape via `..`, symlinks, or an absolute path.
    """
    if not candidate:
        return None
    root = artifact_root().resolve()
    try:
        target = Path(candidate)
        if not target.is_absolute():
            target = root / target
        target = target.resolve()
        target.relative_to(root)
    except (ValueError, OSError):
        return None
    return target if target.is_file() else None


def to_relative(path: str) -> Optional[str]:
    """Express an absolute artifact path relative to the root, for use in URLs."""
    if not path:
        return None
    try:
        return Path(path).resolve().relative_to(artifact_root().resolve()).as_posix()
    except (ValueError, OSError):
        return None
