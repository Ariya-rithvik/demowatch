"""Artifact storage and the dependency registry that makes selective rebuild possible.

Two things live here because they are the two halves of "what did we produce, and what
does it depend on":

  * MediaStore          - writes artifacts under the workflow's artifact directory.
  * DependencyRegistry  - records which truth-set facts each artifact asserts, so a
                          drift event can rebuild only the artifacts that actually broke.

Storage is deliberately a thin seam. Everything goes through `put_*`, so swapping in an
object store later means implementing one class rather than touching every agent.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Union

from .artifacts import workflow_dir, to_relative, artifact_root

_UNSAFE = re.compile(r"[^A-Za-z0-9._/-]")


def _safe_key(key: str) -> str:
    """Normalise an object key: forward slashes, no traversal, no odd characters."""
    key = str(key or "artifact").replace("\\", "/")
    key = "/".join(part for part in key.split("/") if part not in ("", ".", ".."))
    return _UNSAFE.sub("_", key) or "artifact"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def s3_config() -> Dict[str, Optional[str]]:
    """Object-storage settings, read from the environment without requiring them.

    Zerops injects these for its S3-compatible object storage service; any other
    S3 provider works with the same four values.
    """
    return {
        "bucket": os.getenv("S3_BUCKET") or os.getenv("OBJECT_STORAGE_BUCKET"),
        "endpoint": os.getenv("S3_ENDPOINT") or os.getenv("OBJECT_STORAGE_ENDPOINT"),
        "access_key": os.getenv("S3_ACCESS_KEY") or os.getenv("OBJECT_STORAGE_ACCESS_KEY_ID"),
        "secret_key": os.getenv("S3_SECRET_KEY") or os.getenv("OBJECT_STORAGE_SECRET_ACCESS_KEY"),
    }


def s3_configured() -> bool:
    c = s3_config()
    return all(c.values())


class MediaStore:
    """Writes artifacts to object storage when configured, local disk otherwise.

    A local copy is always written because that is what the HTTP layer serves;
    object storage is what makes artifacts survive a container restart or redeploy,
    since containers have ephemeral filesystems.
    """

    def __init__(self, workflow_id: str):
        self.workflow_id = str(workflow_id or "adhoc")
        self._cfg = s3_config()
        self._client = None
        self._backend = "s3" if (s3_configured() and self._s3_client() is not None) else "local"

    @property
    def backend(self) -> str:
        return self._backend

    def _s3_client(self):
        if self._client is not None:
            return self._client
        if not s3_configured():
            return None
        try:
            import boto3  # type: ignore
            from botocore.config import Config  # type: ignore
            endpoint = self._cfg["endpoint"] or ""
            if endpoint and not endpoint.startswith("http"):
                endpoint = f"https://{endpoint}"
            self._client = boto3.client(
                "s3",
                endpoint_url=endpoint or None,
                aws_access_key_id=self._cfg["access_key"],
                aws_secret_access_key=self._cfg["secret_key"],
                config=Config(signature_version="s3v4"),
            )
            return self._client
        except Exception as exc:
            print(f"  [MediaStore] object storage unavailable ({exc}); using local disk.")
            return None

    def describe(self) -> Dict[str, Any]:
        return {
            "backend": self._backend,
            "bucket": self._cfg.get("bucket"),
            "configured": s3_configured(),
            "root": str(artifact_root()),
        }

    def put_bytes(
        self,
        data: bytes,
        key: str,
        content_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Persist raw bytes under `key`, returning where it landed and its hash."""
        key = _safe_key(f"{self.workflow_id}/{key}")
        content_type = content_type or mimetypes.guess_type(key)[0] or "application/octet-stream"

        path = artifact_root() / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

        uri = f"file:///{str(path).replace(chr(92), '/')}"
        stored_remotely = False
        if self._backend == "s3":
            try:
                self._s3_client().put_object(
                    Bucket=self._cfg["bucket"], Key=key, Body=data, ContentType=content_type,
                )
                uri = f"s3://{self._cfg['bucket']}/{key}"
                stored_remotely = True
            except Exception as exc:
                print(f"  [MediaStore] upload failed for {key} ({exc}); kept local copy.")

        return {
            "key": key,
            "uri": uri,
            "artifact_path": to_relative(str(path)),
            "sha256": _sha256_bytes(data),
            "bytes": len(data),
            "content_type": content_type,
            "backend": self._backend,
            "stored_remotely": stored_remotely,
        }

    def fetch_to_local(self, key: str) -> Optional[Path]:
        """Pull an object back to disk.

        After a redeploy the container's filesystem is empty, so serving an old
        artifact means restoring it from object storage first.
        """
        if self._backend != "s3":
            return None
        path = artifact_root() / _safe_key(key)
        if path.is_file():
            return path
        try:
            obj = self._s3_client().get_object(Bucket=self._cfg["bucket"], Key=_safe_key(key))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(obj["Body"].read())
            return path
        except Exception:
            return None

    def put_file(self, local_path: Union[str, Path], key: Optional[str] = None) -> Dict[str, Any]:
        p = Path(local_path)
        if not p.is_file():
            raise FileNotFoundError(f"No such artifact to store: {p}")
        return self.put_bytes(p.read_bytes(), key or p.name)

    def put_json(self, obj: Any, key: str) -> Dict[str, Any]:
        data = json.dumps(obj, indent=2, ensure_ascii=False).encode("utf-8")
        return self.put_bytes(data, key, "application/json")

    def put_text(self, text: str, key: str, content_type: str = "text/plain; charset=utf-8") -> Dict[str, Any]:
        return self.put_bytes(text.encode("utf-8"), key, content_type)


class DependencyRegistry:
    """Which artifacts assert which facts.

    Without this a drift event can only say "something changed, regenerate
    everything". With it, a changed fact resolves to the exact artifacts and agents
    that must re-run.
    """

    FILENAME = "dependencies.json"

    def __init__(self, workflow_id: str, entries: Optional[List[Dict[str, Any]]] = None):
        self.workflow_id = str(workflow_id or "adhoc")
        self._entries: List[Dict[str, Any]] = list(entries or [])

    def declare(
        self,
        artifact_id: str,
        produced_by: str,
        depends_on: Iterable[str],
        meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Record that `artifact_id` asserts the given fact ids. Re-declaring an
        artifact replaces its previous entry so reruns stay idempotent."""
        fact_ids = sorted({f for f in (depends_on or []) if f})
        self._entries = [e for e in self._entries if e["artifact_id"] != artifact_id]
        self._entries.append({
            "artifact_id": artifact_id,
            "produced_by": produced_by,
            "depends_on": fact_ids,
            "meta": meta or {},
        })

    def entries(self) -> List[Dict[str, Any]]:
        return list(self._entries)

    def affected_by(self, changed_fact_ids: Iterable[str]) -> List[Dict[str, Any]]:
        changed = {f for f in (changed_fact_ids or []) if f}
        if not changed:
            return []
        return [e for e in self._entries if changed.intersection(e["depends_on"])]

    def agents_to_rerun(self, changed_fact_ids: Iterable[str]) -> List[str]:
        return sorted({e["produced_by"] for e in self.affected_by(changed_fact_ids)})

    def save(self) -> Dict[str, Any]:
        path = workflow_dir(self.workflow_id) / self.FILENAME
        payload = {"workflow_id": self.workflow_id, "entries": self._entries}
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        return {"path": str(path), "artifact_path": to_relative(str(path)), "count": len(self._entries)}

    @staticmethod
    def load(workflow_id: str) -> "DependencyRegistry":
        path = workflow_dir(workflow_id) / DependencyRegistry.FILENAME
        if not path.is_file():
            return DependencyRegistry(workflow_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return DependencyRegistry(workflow_id, payload.get("entries") or [])
        except Exception:
            return DependencyRegistry(workflow_id)


if __name__ == "__main__":  # pragma: no cover - manual smoke test
    ok = fail = 0

    def check(name: str, cond: bool, detail: str = "") -> None:
        global ok, fail
        if cond:
            ok += 1
            print(f"  PASS  {name}")
        else:
            fail += 1
            print(f"  FAIL  {name}  {detail}")

    print("MediaStore")
    store = MediaStore("wf-selftest")
    r = store.put_text("hello world", "demo/hello.txt")
    check("put_text returns sha", len(r["sha256"]) == 64)
    check("put_text returns artifact_path", bool(r["artifact_path"]))
    check("bytes recorded", r["bytes"] == 11)
    check("put_json works", store.put_json({"a": 1}, "demo/data.json")["bytes"] > 0)
    check("key traversal stripped", ".." not in store.put_text("x", "../../etc/passwd")["key"])

    print("DependencyRegistry")
    reg = DependencyRegistry("wf-selftest")
    reg.declare("demo#scene1", "demo", ["f_a", "f_b"])
    reg.declare("demo#scene2", "demo", ["f_c"])
    reg.declare("guide.md#Pricing", "documentation", ["f_a"])
    check("entries recorded", len(reg.entries()) == 3)
    check("affected_by finds citers", len(reg.affected_by(["f_a"])) == 2)
    check("agents_to_rerun dedupes", reg.agents_to_rerun(["f_a"]) == ["demo", "documentation"])
    check("unrelated fact affects nothing", reg.affected_by(["f_zzz"]) == [])
    check("empty change set is inert", reg.affected_by([]) == [])
    reg.declare("demo#scene1", "demo", ["f_x"])
    check("redeclare replaces", len(reg.entries()) == 3)
    saved = reg.save()
    check("save writes file", Path(saved["path"]).is_file())
    check("load round-trips", len(DependencyRegistry.load("wf-selftest").entries()) == 3)

    print(f"\n{ok} passed, {fail} failed")
    raise SystemExit(1 if fail else 0)
