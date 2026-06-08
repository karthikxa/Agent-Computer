"""Semantic File System (SFS) for SuperAgent.

Inspired by agiresearch/AIOS Logical Semantic File System (LSFS).

Allows agents to store, search, and retrieve workspace files using
natural-language queries rather than exact path lookups, powered by
lightweight TF-IDF vector embeddings (no external API needed).

For production workloads, swap ``_TFIDFIndex`` with a dense-vector store
(e.g. ChromaDB, FAISS) by subclassing ``EmbeddingIndex``.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator


# ---------------------------------------------------------------------------
# Embedding index (TF-IDF, zero-dependency)
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> list[str]:
    return re.findall(r"\w+", text.lower())


class _TFIDFIndex:
    """In-memory TF-IDF index for semantic search over file contents."""

    def __init__(self) -> None:
        self._docs: dict[str, list[str]] = {}          # doc_id -> tokens
        self._idf: dict[str, float] = {}

    def add(self, doc_id: str, text: str) -> None:
        self._docs[doc_id] = _tokenize(text)
        self._rebuild_idf()

    def remove(self, doc_id: str) -> None:
        self._docs.pop(doc_id, None)
        self._rebuild_idf()

    def query(self, text: str, top_k: int = 5) -> list[tuple[str, float]]:
        q_tokens = _tokenize(text)
        if not q_tokens or not self._docs:
            return []
        scores: dict[str, float] = {}
        for doc_id, tokens in self._docs.items():
            tf = Counter(tokens)
            total = len(tokens) or 1
            score = sum(
                (tf[t] / total) * self._idf.get(t, 0.0)
                for t in q_tokens
            )
            if score > 0:
                scores[doc_id] = score
        ranked = sorted(scores.items(), key=lambda x: -x[1])
        return ranked[:top_k]

    def _rebuild_idf(self) -> None:
        n = len(self._docs)
        if n == 0:
            self._idf = {}
            return
        df: dict[str, int] = defaultdict(int)
        for tokens in self._docs.values():
            for t in set(tokens):
                df[t] += 1
        self._idf = {t: math.log((n + 1) / (cnt + 1)) + 1.0 for t, cnt in df.items()}


# ---------------------------------------------------------------------------
# SFS file record
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class SFSFile:
    """Metadata + content for a file stored in the Semantic File System."""

    file_id: str
    name: str
    content: str
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Semantic File System
# ---------------------------------------------------------------------------

class SemanticFileSystem:
    """A file system layer that supports natural-language search.

    Files are stored in SQLite and indexed via TF-IDF for fast semantic
    recall without requiring an external embedding model.

    Usage::

        sfs = SemanticFileSystem(Path(".superagent/sfs.db"))
        sfs.write("notes/design.md", "# Design\\nUse a microkernel approach.")
        results = sfs.search("microkernel architecture")
        # -> [SFSFile(name="notes/design.md", ...)]
    """

    def __init__(self, db_path: Path | str = ":memory:") -> None:
        self.db_path = Path(db_path) if db_path != ":memory:" else db_path  # type: ignore[arg-type]
        if isinstance(self.db_path, Path):
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._index = _TFIDFIndex()
        self._init_db()
        self._load_index()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def write(self, name: str, content: str, *, tags: list[str] | None = None, metadata: dict[str, Any] | None = None) -> SFSFile:
        """Create or overwrite a file by name."""
        file_id = name.replace("/", "_").replace("\\", "_")
        f = SFSFile(
            file_id=file_id,
            name=name,
            content=content,
            tags=tags or [],
            metadata=metadata or {},
        )
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO sfs_files(file_id, name, content, tags, metadata)
                    VALUES(?, ?, ?, ?, ?)
                    ON CONFLICT(file_id) DO UPDATE SET
                        name=excluded.name,
                        content=excluded.content,
                        tags=excluded.tags,
                        metadata=excluded.metadata
                    """,
                    (f.file_id, f.name, f.content, json.dumps(f.tags), json.dumps(f.metadata)),
                )
        finally:
            conn.close()
        self._index.add(file_id, content + " " + " ".join(tags or []))
        return f

    def read(self, name: str) -> SFSFile | None:
        """Read a file by exact name."""
        file_id = name.replace("/", "_").replace("\\", "_")
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT file_id, name, content, tags, metadata FROM sfs_files WHERE file_id=?",
                (file_id,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        return self._row_to_file(row)

    def delete(self, name: str) -> bool:
        """Delete a file. Returns True if the file existed."""
        file_id = name.replace("/", "_").replace("\\", "_")
        conn = self._connect()
        try:
            with conn:
                cursor = conn.execute("DELETE FROM sfs_files WHERE file_id=?", (file_id,))
                deleted = cursor.rowcount > 0
        finally:
            conn.close()
        if deleted:
            self._index.remove(file_id)
        return deleted

    def search(self, query: str, top_k: int = 5) -> list[SFSFile]:
        """Return files ranked by semantic similarity to *query*."""
        ranked = self._index.query(query, top_k=top_k)
        if not ranked:
            return []
        ids = [r[0] for r in ranked]
        placeholders = ",".join("?" * len(ids))
        conn = self._connect()
        try:
            rows = conn.execute(
                f"SELECT file_id, name, content, tags, metadata FROM sfs_files WHERE file_id IN ({placeholders})",
                ids,
            ).fetchall()
        finally:
            conn.close()
        row_map = {r["file_id"]: r for r in rows}
        return [self._row_to_file(row_map[fid]) for fid, _ in ranked if fid in row_map]

    def list_all(self) -> list[SFSFile]:
        """Return all stored files."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT file_id, name, content, tags, metadata FROM sfs_files ORDER BY name"
            ).fetchall()
        finally:
            conn.close()
        return [self._row_to_file(r) for r in rows]

    def nl_command(self, command: str) -> str:
        """Execute a natural-language file command and return a text result.

        Supported patterns
        ------------------
        - ``write <name>: <content>``  → writes a file
        - ``read <name>``             → returns file content
        - ``delete <name>``           → deletes a file
        - ``search <query>``          → returns matching file names
        - ``list``                    → lists all file names
        """
        cmd = command.strip()
        if cmd.lower().startswith("write "):
            rest = cmd[6:]
            if ":" in rest:
                name, _, content = rest.partition(":")
                f = self.write(name.strip(), content.strip())
                return f"Written: {f.name}"
            return "Usage: write <name>: <content>"

        if cmd.lower().startswith("read "):
            name = cmd[5:].strip()
            f = self.read(name)
            return f.content if f else f"File not found: {name}"

        if cmd.lower().startswith("delete "):
            name = cmd[7:].strip()
            ok = self.delete(name)
            return f"Deleted: {name}" if ok else f"Not found: {name}"

        if cmd.lower().startswith("search "):
            query = cmd[7:].strip()
            results = self.search(query)
            if not results:
                return "No matching files."
            return "\n".join(f.name for f in results)

        if cmd.lower() == "list":
            files = self.list_all()
            if not files:
                return "No files stored."
            return "\n".join(f.name for f in files)

        return f"Unknown command: {cmd}"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        path = str(self.db_path)
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS sfs_files (
                        file_id TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        content TEXT NOT NULL,
                        tags TEXT NOT NULL DEFAULT '[]',
                        metadata TEXT NOT NULL DEFAULT '{}'
                    )
                    """
                )
        finally:
            conn.close()

    def _load_index(self) -> None:
        """Rebuild the in-memory TF-IDF index from persisted files."""
        conn = self._connect()
        try:
            rows = conn.execute("SELECT file_id, content, tags FROM sfs_files").fetchall()
        finally:
            conn.close()
        for row in rows:
            text = row["content"] + " " + " ".join(json.loads(row["tags"] or "[]"))
            self._index.add(row["file_id"], text)

    @staticmethod
    def _row_to_file(row: sqlite3.Row) -> SFSFile:
        return SFSFile(
            file_id=row["file_id"],
            name=row["name"],
            content=row["content"],
            tags=json.loads(row["tags"] or "[]"),
            metadata=json.loads(row["metadata"] or "{}"),
        )
