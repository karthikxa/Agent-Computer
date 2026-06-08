"""Session persistence."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class Session:
    """Serialized agent session state."""

    session_id: str
    created_at: str
    updated_at: str
    state: dict[str, Any] = field(default_factory=dict)


class SessionManager:
    """Persist and restore agent sessions from disk."""

    def __init__(self, session_dir: str | Path) -> None:
        self.session_dir = Path(session_dir)
        self.session_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, session_id: str) -> Path:
        return self.session_dir / f"{session_id}.json"

    async def save(self, session: Session) -> None:
        await asyncio.to_thread(self._save_sync, session)

    def _save_sync(self, session: Session) -> None:
        self._path(session.session_id).write_text(json.dumps(asdict(session), indent=2), encoding="utf-8")

    async def load(self, session_id: str) -> Session | None:
        return await asyncio.to_thread(self._load_sync, session_id)

    def _load_sync(self, session_id: str) -> Session | None:
        path = self._path(session_id)
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return Session(**data)

    async def create(self, session_id: str, state: dict[str, Any] | None = None) -> Session:
        now = datetime.now(timezone.utc).isoformat()
        session = Session(session_id=session_id, created_at=now, updated_at=now, state=state or {})
        await self.save(session)
        return session

    async def update(self, session_id: str, state: dict[str, Any]) -> Session:
        session = await self.load(session_id)
        if session is None:
            session = await self.create(session_id, state)
        else:
            session.updated_at = datetime.now(timezone.utc).isoformat()
            session.state.update(state)
            await self.save(session)
        return session

    async def list_sessions(self) -> list[Session]:
        sessions: list[Session] = []
        for path in sorted(self.session_dir.glob("*.json")):
            loaded = await self.load(path.stem)
            if loaded is not None:
                sessions.append(loaded)
        return sessions
