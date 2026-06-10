"""Persistent agent memory backed by SQLite and FTS5."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class MemoryRecord:
    """A single memory item."""

    memory_id: str
    text: str
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


class AgentMemory:
    """Store and retrieve memories using SQLite with FTS5 where available."""

    def __init__(self, db_path: str | Path, max_records: int = 1000) -> None:
        self.db_path = Path(db_path)
        self.max_records = max_records
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS memories (
                        memory_id TEXT PRIMARY KEY,
                        text TEXT NOT NULL,
                        tags TEXT NOT NULL DEFAULT '[]',
                        metadata TEXT NOT NULL DEFAULT '{}',
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                try:
                    conn.execute(
                        """
                        CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts
                        USING fts5(memory_id, text, tags)
                        """
                    )
                except sqlite3.OperationalError:
                    pass
        finally:
            conn.close()

    async def store(self, record: MemoryRecord) -> None:
        """Persist a memory record."""

        await asyncio.to_thread(self._store_sync, record)

    def _store_sync(self, record: MemoryRecord) -> None:
        conn = self._connect()
        try:
            with conn:
                # Quota Eviction
                cursor = conn.execute("SELECT COUNT(*) FROM memories")
                count = cursor.fetchone()[0]
                if count >= self.max_records:
                    evict_cursor = conn.execute("SELECT memory_id FROM memories ORDER BY updated_at ASC LIMIT ?", (count - self.max_records + 1,))
                    evicted_ids = [row[0] for row in evict_cursor.fetchall()]
                    for eid in evicted_ids:
                        conn.execute("DELETE FROM memories WHERE memory_id = ?", (eid,))
                        try:
                            conn.execute("DELETE FROM memories_fts WHERE memory_id = ?", (eid,))
                        except Exception:
                            pass

                conn.execute(
                    """
                    INSERT INTO memories(memory_id, text, tags, metadata, updated_at)
                    VALUES(?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(memory_id) DO UPDATE SET
                        text=excluded.text,
                        tags=excluded.tags,
                        metadata=excluded.metadata,
                        updated_at=CURRENT_TIMESTAMP
                    """,
                    (
                        record.memory_id,
                        record.text,
                        json.dumps(record.tags),
                        json.dumps(record.metadata),
                    ),
                )
                try:
                    conn.execute("DELETE FROM memories_fts WHERE memory_id = ?", (record.memory_id,))
                    conn.execute(
                        "INSERT INTO memories_fts(memory_id, text, tags) VALUES(?, ?, ?)",
                        (record.memory_id, record.text, json.dumps(record.tags)),
                    )
                except sqlite3.OperationalError:
                    pass
        finally:
            conn.close()

    async def recall(self, query: str, *, limit: int = 10) -> list[MemoryRecord]:
        """Search memories using FTS5 or a fallback LIKE query."""

        return await asyncio.to_thread(self._recall_sync, query, limit)

    def _recall_sync(self, query: str, limit: int) -> list[MemoryRecord]:
        conn = self._connect()
        try:
            try:
                cursor = conn.execute(
                    """
                    SELECT memory_id, text, tags, metadata
                    FROM memories
                    WHERE memory_id IN (
                        SELECT memory_id FROM memories_fts WHERE memories_fts MATCH ? LIMIT ?
                    )
                    ORDER BY updated_at DESC
                    """,
                    (query, limit),
                )
            except sqlite3.OperationalError:
                cursor = conn.execute(
                    """
                    SELECT memory_id, text, tags, metadata
                    FROM memories
                    WHERE text LIKE ? OR tags LIKE ?
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (f"%{query}%", f"%{query}%", limit),
                )
            rows = cursor.fetchall()
        finally:
            conn.close()
        return [
            MemoryRecord(
                memory_id=row["memory_id"],
                text=row["text"],
                tags=json.loads(row["tags"] or "[]"),
                metadata=json.loads(row["metadata"] or "{}"),
            )
            for row in rows
        ]

    async def close(self) -> None:
        """SQLite connections are short lived, so there is nothing to close."""

        return None

    # ------------------------------------------------------------------
    # Feature #60 — Context switch: save/restore memory state per task
    # ------------------------------------------------------------------

    async def save_context(self, context_name: str) -> bool:
        """Snapshot the current memory state to a named context slot.

        Allows an agent to switch tasks without memory contamination.
        The snapshot is stored as a JSON blob in a ``context_snapshots``
        table inside the same SQLite database.

        Parameters
        ----------
        context_name:
            Arbitrary label for this snapshot (e.g. task ID or agent-task pair).
        """
        def _save() -> bool:
            conn = self._connect()
            try:
                with conn:
                    # Ensure snapshot table exists
                    conn.execute("""
                        CREATE TABLE IF NOT EXISTS context_snapshots (
                            name       TEXT PRIMARY KEY,
                            snapshot   TEXT NOT NULL,
                            saved_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        )
                    """)
                    # Dump all current memories to JSON
                    rows = conn.execute(
                        "SELECT memory_id, text, tags, metadata FROM memories"
                    ).fetchall()
                    snapshot = [
                        {
                            "memory_id": r[0],
                            "text": r[1],
                            "tags": r[2],
                            "metadata": r[3],
                        }
                        for r in rows
                    ]
                    import json as _json
                    conn.execute(
                        "INSERT OR REPLACE INTO context_snapshots (name, snapshot) VALUES (?, ?)",
                        (context_name, _json.dumps(snapshot)),
                    )
                return True
            finally:
                conn.close()

        return await asyncio.to_thread(_save)

    async def restore_context(self, context_name: str, *, clear_current: bool = True) -> int:
        """Restore a previously saved context snapshot.

        Parameters
        ----------
        context_name:
            The name passed to save_context().
        clear_current:
            If True, clears all current memories before restoring
            (full context switch). If False, merges snapshot on top.

        Returns
        -------
        int
            Number of memory records restored.
        """
        def _restore() -> int:
            import json as _json
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT snapshot FROM context_snapshots WHERE name=?",
                    (context_name,),
                ).fetchone()
                if not row:
                    return 0
                records = _json.loads(row[0])
                with conn:
                    if clear_current:
                        conn.execute("DELETE FROM memories")
                        try:
                            conn.execute("DELETE FROM memories_fts")
                        except Exception:
                            pass
                    for rec in records:
                        conn.execute(
                            """INSERT INTO memories(memory_id, text, tags, metadata, updated_at)
                               VALUES(?, ?, ?, ?, CURRENT_TIMESTAMP)
                               ON CONFLICT(memory_id) DO UPDATE SET
                               text=excluded.text, tags=excluded.tags,
                               metadata=excluded.metadata, updated_at=CURRENT_TIMESTAMP""",
                            (rec["memory_id"], rec["text"], rec["tags"], rec["metadata"]),
                        )
                return len(records)
            finally:
                conn.close()

        return await asyncio.to_thread(_restore)

    async def list_contexts(self) -> list[str]:
        """List all saved context snapshot names."""
        def _list() -> list[str]:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT name FROM context_snapshots ORDER BY saved_at DESC"
                ).fetchall()
                return [r[0] for r in rows]
            except Exception:
                return []
            finally:
                conn.close()

        return await asyncio.to_thread(_list)

    async def delete_context(self, context_name: str) -> bool:
        """Delete a saved context snapshot."""
        def _delete() -> bool:
            conn = self._connect()
            try:
                with conn:
                    cur = conn.execute(
                        "DELETE FROM context_snapshots WHERE name=?", (context_name,)
                    )
                return cur.rowcount > 0
            finally:
                conn.close()

        return await asyncio.to_thread(_delete)



class SQLiteMemory:
    """Synchronous SQLite memory with FTS5 search."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS memories (
                        memory_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        agent_id TEXT NOT NULL,
                        title TEXT NOT NULL,
                        content TEXT NOT NULL,
                        tags TEXT NOT NULL DEFAULT ''
                    )
                    """
                )
                try:
                    conn.execute(
                        """
                        CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts
                        USING fts5(memory_id UNINDEXED, agent_id, title, content, tags)
                        """
                    )
                except sqlite3.OperationalError:
                    pass
        finally:
            conn.close()

    def store(self, agent_id: str, title: str, content: str, tags: str = "") -> int:
        """Store a memory entry."""

        conn = self._connect()
        try:
            with conn:
                cursor = conn.execute(
                    "INSERT INTO memories(agent_id, title, content, tags) VALUES(?, ?, ?, ?)",
                    (agent_id, title, content, tags),
                )
                memory_id = int(cursor.lastrowid)
                try:
                    conn.execute(
                        "DELETE FROM memories_fts WHERE memory_id = ?",
                        (memory_id,),
                    )
                    conn.execute(
                        "INSERT INTO memories_fts(memory_id, agent_id, title, content, tags) VALUES(?, ?, ?, ?, ?)",
                        (memory_id, agent_id, title, content, tags),
                    )
                except sqlite3.OperationalError:
                    pass
                conn.commit()
                return memory_id
        finally:
            conn.close()

    def recall(self, agent_id: str, query: str) -> list[dict[str, Any]]:
        """Recall memories for an agent using FTS5 search."""

        conn = self._connect()
        try:
            try:
                cursor = conn.execute(
                    """
                    SELECT memory_id, agent_id, title, content, tags
                    FROM memories
                    WHERE agent_id = ?
                      AND memory_id IN (
                        SELECT memory_id FROM memories_fts WHERE memories_fts MATCH ?
                      )
                    ORDER BY memory_id DESC
                    """,
                    (agent_id, query),
                )
            except sqlite3.OperationalError:
                cursor = conn.execute(
                    """
                    SELECT memory_id, agent_id, title, content, tags
                    FROM memories
                    WHERE agent_id = ?
                      AND (title LIKE ? OR content LIKE ? OR tags LIKE ?)
                    ORDER BY memory_id DESC
                    """,
                    (agent_id, f"%{query}%", f"%{query}%", f"%{query}%"),
                )
            rows = cursor.fetchall()
        finally:
            conn.close()
        return [
            {
                "memory_id": int(row["memory_id"]),
                "agent_id": row["agent_id"],
                "title": row["title"],
                "content": row["content"],
                "tags": row["tags"],
            }
            for row in rows
        ]
