"""Shared storage helpers for multi-agent coordination."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class _AwaitablePath:
    """Path-like value that can also be awaited."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def __fspath__(self) -> str:
        return str(self._path)

    def __str__(self) -> str:
        return str(self._path)

    def __repr__(self) -> str:
        return repr(self._path)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._path, name)

    def __await__(self):
        async def _wrap() -> Path:
            return self._path

        return _wrap().__await__()


class _AwaitableDict(dict[str, Any]):
    """Dictionary that can also be awaited."""

    def __await__(self):
        async def _wrap() -> dict[str, Any]:
            return self

        return _wrap().__await__()


@dataclass(slots=True)
class SharedStorage:
    """Filesystem-backed shared storage for agent collaboration."""

    shared_path: Path

    def __post_init__(self) -> None:
        self.shared_path = Path(self.shared_path)
        (self.shared_path / "results").mkdir(parents=True, exist_ok=True)
        (self.shared_path / "files").mkdir(parents=True, exist_ok=True)
        (self.shared_path / "inbox").mkdir(parents=True, exist_ok=True)

    def write_result(self, agent_id: str, task_id: str, data: dict[str, Any]) -> _AwaitablePath:
        """Write a task result to shared storage."""

        path = self.shared_path / "results" / f"{task_id}.json"
        path.write_text(json.dumps({"agent_id": agent_id, **data}, indent=2), encoding="utf-8")
        return _AwaitablePath(path)

    def read_result(self, task_id: str) -> _AwaitableDict:
        """Read a task result from shared storage."""

        path = self.shared_path / "results" / f"{task_id}.json"
        return _AwaitableDict(json.loads(path.read_text(encoding="utf-8")))

    def write_file(self, agent_id: str, local_path: str | Path) -> _AwaitablePath:
        """Copy a file into shared storage."""

        source = Path(local_path)
        target_dir = self.shared_path / "files" / agent_id
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / source.name
        shutil.copy2(source, target)
        return _AwaitablePath(target)

    def read_file(self, filename: str) -> bytes:
        """Read a shared file by filename."""

        for path in (self.shared_path / "files").rglob(filename):
            if path.is_file():
                return path.read_bytes()
        raise FileNotFoundError(filename)

    def list_files(self) -> list[str]:
        """List all shared files."""

        return [str(path.relative_to(self.shared_path)) for path in (self.shared_path / "files").rglob("*") if path.is_file()]

    def agent_inbox(self, agent_id: str) -> list[dict[str, Any]]:
        """Return messages and files sent to a specific agent."""

        inbox_dir = self.shared_path / "inbox" / agent_id
        messages: list[dict[str, Any]] = []
        if inbox_dir.exists():
            for path in sorted(inbox_dir.glob("*.json")):
                messages.append(json.loads(path.read_text(encoding="utf-8")))
        return messages

    def send_to_agent(self, from_id: str, to_id: str, message: str) -> _AwaitablePath:
        """Send a message to another agent."""

        inbox_dir = self.shared_path / "inbox" / to_id
        inbox_dir.mkdir(parents=True, exist_ok=True)
        path = inbox_dir / f"{from_id}-{len(list(inbox_dir.glob('*.json')))+1}.json"
        path.write_text(json.dumps({"from": from_id, "to": to_id, "message": message}, indent=2), encoding="utf-8")
        return _AwaitablePath(path)
