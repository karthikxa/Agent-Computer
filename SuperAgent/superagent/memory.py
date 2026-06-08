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

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
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

    async def store(self, record: MemoryRecord) -> None:
        """Persist a memory record."""

        await asyncio.to_thread(self._store_sync, record)

    def _store_sync(self, record: MemoryRecord) -> None:
        conn = self._connect()
        try:
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
            conn.commit()
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
        with self._connect() as conn:
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

    def store(self, agent_id: str, title: str, content: str, tags: str = "") -> int:
        """Store a memory entry."""

        with self._connect() as conn:
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

    def recall(self, agent_id: str, query: str) -> list[dict[str, Any]]:
        """Recall memories for an agent using FTS5 search."""

        with self._connect() as conn:
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
