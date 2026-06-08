"""SQLite task database for orchestration and worker state."""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class TaskDatabase:
    """SQLite-backed workforce database."""

    db_path: Path

    def __post_init__(self) -> None:
        self.db_path = Path(self.db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    command TEXT NOT NULL,
                    subtask_instruction TEXT NOT NULL,
                    assigned_agent TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    priority INTEGER NOT NULL DEFAULT 100,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    result TEXT,
                    error TEXT
                );
                CREATE TABLE IF NOT EXISTS agents (
                    id TEXT PRIMARY KEY,
                    container_id TEXT,
                    status TEXT NOT NULL DEFAULT 'idle',
                    current_task_id INTEGER,
                    last_heartbeat TEXT,
                    total_tasks_done INTEGER NOT NULL DEFAULT 0,
                    total_errors INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER NOT NULL,
                    agent_id TEXT NOT NULL,
                    output TEXT,
                    files TEXT,
                    screenshots TEXT,
                    tokens_used INTEGER DEFAULT 0,
                    cost REAL DEFAULT 0,
                    timestamp TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent_id TEXT NOT NULL,
                    site TEXT NOT NULL,
                    cookies TEXT,
                    localStorage TEXT,
                    saved_at TEXT NOT NULL
                );
                """
            )

    async def init(self) -> None:
        """Compatibility async initializer used by verification scripts."""

        self._init_db()

    async def create_task(self, command: str, instruction: str, priority: int) -> int:
        """Create a new task."""

        def _create() -> int:
            with self._connect() as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO tasks(command, subtask_instruction, status, priority, created_at)
                    VALUES(?, ?, 'pending', ?, ?)
                    """,
                    (command, instruction, priority, _utcnow()),
                )
                return int(cursor.lastrowid)

        return await asyncio.to_thread(_create)

    async def assign_task(self, task_id: int, agent_id: str) -> None:
        """Assign a task to an agent."""

        def _assign() -> None:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE tasks SET assigned_agent=?, status='running', started_at=? WHERE id=?",
                    (agent_id, _utcnow(), task_id),
                )
                conn.execute(
                    "INSERT INTO agents(id, status, current_task_id, last_heartbeat) VALUES(?, 'busy', ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET status='busy', current_task_id=excluded.current_task_id, last_heartbeat=excluded.last_heartbeat",
                    (agent_id, task_id, _utcnow()),
                )

        await asyncio.to_thread(_assign)

    async def complete_task(self, task_id: int, result: str) -> None:
        """Mark a task as completed."""

        def _complete() -> None:
            with self._connect() as conn:
                row = conn.execute("SELECT assigned_agent FROM tasks WHERE id=?", (task_id,)).fetchone()
                agent_id = row["assigned_agent"] if row else None
                conn.execute(
                    "UPDATE tasks SET status='completed', completed_at=?, result=? WHERE id=?",
                    (_utcnow(), result, task_id),
                )
                if agent_id:
                    conn.execute(
                        "UPDATE agents SET status='idle', current_task_id=NULL, total_tasks_done=total_tasks_done+1 WHERE id=?",
                        (agent_id,),
                    )
                conn.execute(
                    "INSERT INTO results(task_id, agent_id, output, files, screenshots, tokens_used, cost, timestamp) VALUES(?, ?, ?, '[]', '[]', 0, 0, ?)",
                    (task_id, agent_id or "", result, _utcnow()),
                )

        await asyncio.to_thread(_complete)

    async def fail_task(self, task_id: int, error: str) -> bool:
        """Mark a task as failed and retry up to three times."""

        def _fail() -> bool:
            with self._connect() as conn:
                row = conn.execute("SELECT retry_count, assigned_agent FROM tasks WHERE id=?", (task_id,)).fetchone()
                retry_count = int(row["retry_count"]) if row else 0
                agent_id = row["assigned_agent"] if row else None
                retry_count += 1
                if retry_count <= 3:
                    conn.execute(
                        "UPDATE tasks SET retry_count=?, status='pending', assigned_agent=NULL, error=? WHERE id=?",
                        (retry_count, error, task_id),
                    )
                    if agent_id:
                        conn.execute(
                            "UPDATE agents SET status='error', total_errors=total_errors+1, current_task_id=NULL WHERE id=?",
                            (agent_id,),
                        )
                    return True
                conn.execute(
                    "UPDATE tasks SET retry_count=?, status='failed', error=? WHERE id=?",
                    (retry_count, error, task_id),
                )
                return False

        return await asyncio.to_thread(_fail)

    async def get_next_pending(self) -> dict[str, Any] | None:
        """Return the highest-priority pending task."""

        def _get() -> dict[str, Any] | None:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM tasks WHERE status='pending' AND assigned_agent IS NULL ORDER BY priority ASC, created_at ASC LIMIT 1"
                ).fetchone()
                return dict(row) if row else None

        return await asyncio.to_thread(_get)

    async def get_all_pending(self) -> list[dict[str, Any]]:
        """Return all pending tasks."""

        def _get() -> list[dict[str, Any]]:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT * FROM tasks WHERE status='pending' AND assigned_agent IS NULL ORDER BY priority ASC, created_at ASC"
                ).fetchall()
                return [dict(row) for row in rows]

        return await asyncio.to_thread(_get)

    async def update_heartbeat(self, agent_id: str) -> None:
        """Update agent heartbeat."""

        def _update() -> None:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO agents(id, last_heartbeat) VALUES(?, ?) ON CONFLICT(id) DO UPDATE SET last_heartbeat=excluded.last_heartbeat",
                    (agent_id, _utcnow()),
                )

        await asyncio.to_thread(_update)

    async def get_dead_agents(self, timeout: int = 30) -> list[dict[str, Any]]:
        """Return agents whose heartbeat is older than the timeout."""

        def _get() -> list[dict[str, Any]]:
            cutoff = datetime.now(timezone.utc).timestamp() - timeout
            with self._connect() as conn:
                rows = conn.execute("SELECT * FROM agents").fetchall()
                dead: list[dict[str, Any]] = []
                for row in rows:
                    last = row["last_heartbeat"]
                    if not last:
                        dead.append(dict(row))
                        continue
                    try:
                        ts = datetime.fromisoformat(last).timestamp()
                    except ValueError:
                        dead.append(dict(row))
                        continue
                    if ts < cutoff:
                        dead.append(dict(row))
                return dead

        return await asyncio.to_thread(_get)

    async def get_agent_tasks(self, agent_id: str) -> list[dict[str, Any]]:
        """Return all tasks for one agent."""

        def _get() -> list[dict[str, Any]]:
            with self._connect() as conn:
                rows = conn.execute("SELECT * FROM tasks WHERE assigned_agent=? ORDER BY created_at DESC", (agent_id,)).fetchall()
                return [dict(row) for row in rows]

        return await asyncio.to_thread(_get)

    async def get_workforce_status(self) -> dict[str, Any]:
        """Return a workforce summary."""

        def _get() -> dict[str, Any]:
            with self._connect() as conn:
                agents = [dict(row) for row in conn.execute("SELECT * FROM agents").fetchall()]
                pending = conn.execute("SELECT COUNT(*) AS c FROM tasks WHERE status='pending'").fetchone()["c"]
                running = conn.execute("SELECT COUNT(*) AS c FROM tasks WHERE status='running'").fetchone()["c"]
                done = conn.execute("SELECT COUNT(*) AS c FROM tasks WHERE status='completed'").fetchone()["c"]
                failed = conn.execute("SELECT COUNT(*) AS c FROM tasks WHERE status='failed'").fetchone()["c"]
                return {"agents": agents, "tasks": {"pending": pending, "running": running, "completed": done, "failed": failed}}

        return await asyncio.to_thread(_get)

    async def save_session(self, agent_id: str, site: str, cookies: str, local_storage: str) -> None:
        """Persist browser session state."""

        def _save() -> None:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO sessions(agent_id, site, cookies, localStorage, saved_at) VALUES(?, ?, ?, ?, ?)",
                    (agent_id, site, cookies, local_storage, _utcnow()),
                )

        await asyncio.to_thread(_save)

    async def load_session(self, agent_id: str, site: str) -> dict[str, Any] | None:
        """Load a saved browser session."""

        def _load() -> dict[str, Any] | None:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM sessions WHERE agent_id=? AND site=? ORDER BY saved_at DESC LIMIT 1",
                    (agent_id, site),
                ).fetchone()
                return dict(row) if row else None

        return await asyncio.to_thread(_load)


class TaskDB(TaskDatabase):
    """Compatibility alias that matches the historical public API."""

    async def init(self) -> None:
        """Compatibility async initializer used by older callers."""

        await super().init()
