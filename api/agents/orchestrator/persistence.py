"""Durable storage for workflows and tasks.

Runs on PostgreSQL when `DATABASE_URL` is set (Zerops provides one for the managed
database service) and falls back to a local SQLite file otherwise, so the same code
runs in production and on a laptop with no services running.

The schema is deliberately portable: `TEXT`/`INTEGER`, `CURRENT_TIMESTAMP` and
`INSERT ... ON CONFLICT ... DO UPDATE` all mean the same thing in both engines. Only
three things actually differ, and they are isolated here:

  * parameter placeholders - `?` for SQLite, `%s` for PostgreSQL
  * how a row becomes a dict
  * connection handling - a pool for PostgreSQL, a per-call connection for SQLite
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from typing import Any, Dict, List, Optional

_PG_PREFIXES = ("postgres://", "postgresql://")


def _is_postgres(url: Optional[str]) -> bool:
    return bool(url) and url.startswith(_PG_PREFIXES)


class DatabaseManager:
    """Persistent storage for workflows and tasks, on PostgreSQL or SQLite."""

    def __init__(self, db_path: Optional[str] = None, database_url: Optional[str] = None):
        self.database_url = database_url or os.getenv("DATABASE_URL") or ""
        self.backend = "postgresql" if _is_postgres(self.database_url) else "sqlite"

        # Writes are serialised. This is a job orchestrator, not a high-QPS service,
        # and a single lock avoids a class of interleaving bugs for no real cost.
        self._lock = threading.Lock()
        self._pool = None

        if self.backend == "postgresql":
            self._init_pool()
        else:
            self.db_path = db_path or os.getenv("DB_PATH") or (
                "orchestrator_jobs.db" if os.access(".", os.W_OK) else "/tmp/orchestrator_jobs.db"
            )

        self._init_db()

    # ── connection handling ──────────────────────────────────────────────────

    def _init_pool(self) -> None:
        from psycopg_pool import ConnectionPool  # type: ignore

        # Opened eagerly so a bad DATABASE_URL fails at startup rather than on the
        # first workflow submission.
        self._pool = ConnectionPool(self.database_url, min_size=1, max_size=8, open=True)

    def _placeholder(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.backend == "postgresql" else sql

    class _Cursor:
        """Uniform `execute` + dict rows across both drivers."""

        def __init__(self, outer: "DatabaseManager"):
            self.outer = outer
            self._conn = None
            self._pg_ctx = None

        def __enter__(self):
            if self.outer.backend == "postgresql":
                from psycopg.rows import dict_row  # type: ignore
                self._pg_ctx = self.outer._pool.connection()
                self._conn = self._pg_ctx.__enter__()
                self.cur = self._conn.cursor(row_factory=dict_row)
            else:
                self._conn = sqlite3.connect(self.outer.db_path, check_same_thread=False)
                self._conn.row_factory = sqlite3.Row
                self.cur = self._conn.cursor()
            return self

        def execute(self, sql: str, params: tuple = ()):
            self.cur.execute(self.outer._placeholder(sql), params)
            return self.cur

        def fetchone(self) -> Optional[Dict[str, Any]]:
            row = self.cur.fetchone()
            return dict(row) if row else None

        def fetchall(self) -> List[Dict[str, Any]]:
            return [dict(r) for r in self.cur.fetchall()]

        def __exit__(self, exc_type, exc, tb):
            try:
                if exc_type is None:
                    self._conn.commit()
                else:
                    self._conn.rollback()
            finally:
                if self.outer.backend == "postgresql":
                    self._pg_ctx.__exit__(exc_type, exc, tb)
                else:
                    self._conn.close()
            return False

    def _cursor(self) -> "DatabaseManager._Cursor":
        return DatabaseManager._Cursor(self)

    # ── schema ───────────────────────────────────────────────────────────────

    def _init_db(self) -> None:
        with self._lock, self._cursor() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS workflows (
                    workflow_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    goal TEXT NOT NULL,
                    status TEXT NOT NULL,
                    priority INTEGER DEFAULT 1,
                    plan TEXT,
                    context TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    workflow_id TEXT NOT NULL,
                    agent_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    priority INTEGER DEFAULT 1,
                    input_data TEXT,
                    output_data TEXT,
                    error_msg TEXT,
                    retry_count INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # Status lookups run on every scheduler tick.
            c.execute("CREATE INDEX IF NOT EXISTS idx_workflows_status ON workflows(status)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_tasks_workflow ON tasks(workflow_id)")

    # ── writes ───────────────────────────────────────────────────────────────

    def save_workflow(self, workflow_id: str, user_id: str, goal: str, status: str,
                      priority: int = 1, plan: Optional[List[str]] = None,
                      context: Optional[Dict[str, Any]] = None) -> None:
        with self._lock, self._cursor() as c:
            c.execute("""
                INSERT INTO workflows (workflow_id, user_id, goal, status, priority, plan, context, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT (workflow_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    plan = COALESCE(EXCLUDED.plan, workflows.plan),
                    context = EXCLUDED.context,
                    updated_at = CURRENT_TIMESTAMP
            """, (workflow_id, user_id, goal, status, priority,
                  # NULL, not "[]", when no plan is supplied - the COALESCE above
                  # only preserves the stored plan if this is genuinely NULL, and a
                  # status update would otherwise erase it.
                  json.dumps(plan) if plan is not None else None,
                  json.dumps(context or {})))

    def save_task(self, task_id: str, workflow_id: str, agent_name: str, status: str,
                  priority: int = 1, input_data: Optional[Dict[str, Any]] = None,
                  output_data: Optional[Dict[str, Any]] = None,
                  error_msg: Optional[str] = None, retry_count: int = 0) -> None:
        with self._lock, self._cursor() as c:
            c.execute("""
                INSERT INTO tasks (task_id, workflow_id, agent_name, status, priority,
                                   input_data, output_data, error_msg, retry_count, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT (task_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    output_data = EXCLUDED.output_data,
                    error_msg = EXCLUDED.error_msg,
                    retry_count = EXCLUDED.retry_count,
                    updated_at = CURRENT_TIMESTAMP
            """, (task_id, workflow_id, agent_name, status, priority,
                  json.dumps(input_data or {}),
                  json.dumps(output_data) if output_data else None,
                  error_msg, retry_count))

    # ── reads ────────────────────────────────────────────────────────────────

    @staticmethod
    def _decode_workflow(row: Dict[str, Any]) -> Dict[str, Any]:
        row["context"] = json.loads(row["context"]) if row.get("context") else {}
        row["plan"] = json.loads(row["plan"]) if row.get("plan") else []
        return row

    @staticmethod
    def _decode_task(row: Dict[str, Any]) -> Dict[str, Any]:
        row["input_data"] = json.loads(row["input_data"]) if row.get("input_data") else {}
        row["output_data"] = json.loads(row["output_data"]) if row.get("output_data") else {}
        return row

    def get_workflow(self, workflow_id: str) -> Optional[Dict[str, Any]]:
        with self._lock, self._cursor() as c:
            c.execute("SELECT * FROM workflows WHERE workflow_id = ?", (workflow_id,))
            row = c.fetchone()
        return self._decode_workflow(row) if row else None

    def get_tasks_for_workflow(self, workflow_id: str) -> List[Dict[str, Any]]:
        with self._lock, self._cursor() as c:
            c.execute("SELECT * FROM tasks WHERE workflow_id = ? ORDER BY created_at ASC",
                      (workflow_id,))
            rows = c.fetchall()
        return [self._decode_task(r) for r in rows]

    def get_active_workflows(self) -> List[Dict[str, Any]]:
        with self._lock, self._cursor() as c:
            c.execute("SELECT * FROM workflows WHERE status IN ('QUEUED', 'RUNNING', 'WAITING')")
            rows = c.fetchall()
        return [self._decode_workflow(r) for r in rows]

    def get_unfinished_tasks(self) -> List[Dict[str, Any]]:
        with self._lock, self._cursor() as c:
            c.execute("SELECT * FROM tasks WHERE status IN ('QUEUED', 'RUNNING') "
                      "ORDER BY priority DESC, created_at ASC")
            rows = c.fetchall()
        return [self._decode_task(r) for r in rows]

    def list_recent_workflows(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self._lock, self._cursor() as c:
            c.execute("SELECT * FROM workflows ORDER BY created_at DESC LIMIT ?", (limit,))
            rows = c.fetchall()
        return [self._decode_workflow(r) for r in rows]

    def recover_interrupted_tasks(self) -> None:
        """Re-queue anything left RUNNING by a crash or redeploy.

        Zerops restarts containers on deploy, so this is the difference between a
        redeploy losing in-flight work and picking it back up.
        """
        with self._lock, self._cursor() as c:
            c.execute("UPDATE tasks SET status = 'QUEUED' WHERE status = 'RUNNING'")
            c.execute("UPDATE workflows SET status = 'QUEUED' WHERE status = 'RUNNING'")

    def healthy(self) -> bool:
        """Cheap connectivity probe for the health endpoint."""
        try:
            with self._cursor() as c:
                c.execute("SELECT 1")
                c.fetchone()
            return True
        except Exception:
            return False


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

    tmp = Path(tempfile.mkdtemp(prefix="adip_db_"))
    db = DatabaseManager(db_path=str(tmp / "t.db"))

    print(f"backend: {db.backend}")
    check("sqlite selected without DATABASE_URL", db.backend == "sqlite")
    check("healthy", db.healthy())

    db.save_workflow("wf1", "u", "goal", "QUEUED", 2, ["explorer", "demo"], {"a": 1})
    w = db.get_workflow("wf1")
    check("workflow round-trips", w and w["goal"] == "goal")
    check("plan decoded", w["plan"] == ["explorer", "demo"])
    check("context decoded", w["context"] == {"a": 1})

    db.save_workflow("wf1", "u", "goal", "RUNNING", 2, None, {"a": 2})
    w = db.get_workflow("wf1")
    check("upsert updates status", w["status"] == "RUNNING")
    check("upsert preserves plan when None", w["plan"] == ["explorer", "demo"])

    db.save_task("t1", "wf1", "explorer", "RUNNING", 1, {"in": 1})
    db.save_task("t2", "wf1", "demo", "QUEUED", 1, {"in": 2})
    tasks = db.get_tasks_for_workflow("wf1")
    check("tasks round-trip", len(tasks) == 2)
    check("input decoded", tasks[0]["input_data"]["in"] in (1, 2))

    check("active workflows found", len(db.get_active_workflows()) == 1)
    check("unfinished tasks found", len(db.get_unfinished_tasks()) == 2)

    db.recover_interrupted_tasks()
    check("running task requeued",
          all(t["status"] == "QUEUED" for t in db.get_tasks_for_workflow("wf1")))
    check("running workflow requeued", db.get_workflow("wf1")["status"] == "QUEUED")
    check("recent list works", len(db.list_recent_workflows()) == 1)

    check("postgres url detected", _is_postgres("postgresql://u:p@h/db"))
    check("sqlite path not detected as pg", not _is_postgres("/tmp/x.db"))

    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{ok} passed, {fail} failed")
    raise SystemExit(1 if fail else 0)
