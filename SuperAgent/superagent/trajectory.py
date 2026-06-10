"""Agent trajectory recording for replay and training.

Feature #57 — inspired by trycua/cua.

Records every agent step (screenshot, action, result) to a structured
JSONL file. Trajectories can be replayed for debugging or used as
training data for fine-tuning vision-action models.

Usage::

    recorder = TrajectoryRecorder(agent_id="agent-1", output_dir=".superagent/trajectories")
    await recorder.start()

    # Inside the agent loop:
    frame_id = await recorder.record_step(
        screenshot_bytes, action, result, objective
    )

    await recorder.stop()

    # Replay:
    replayer = TrajectoryReplayer(".superagent/trajectories/agent-1_20240101T120000.jsonl")
    async for frame in replayer.steps():
        print(frame.action)
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class TrajectoryFrame:
    """One recorded step in a trajectory."""

    frame_id: int
    agent_id: str
    objective: str
    timestamp: float
    action: dict[str, Any]
    result: str                    # "success" | "error" | "pending"
    screenshot_b64: str = ""       # base64 PNG, optional (can be stripped for size)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, data: str) -> "TrajectoryFrame":
        d = json.loads(data)
        return cls(**d)


@dataclass
class TrajectoryMeta:
    """Header metadata for a trajectory file."""

    agent_id: str
    start_time: float
    objective: str
    total_frames: int = 0
    end_time: float | None = None
    success: bool | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self))


# ---------------------------------------------------------------------------
# Recorder
# ---------------------------------------------------------------------------

class TrajectoryRecorder:
    """Records agent trajectories to a JSONL file.

    File format
    -----------
    Line 0: TrajectoryMeta JSON
    Lines 1+: TrajectoryFrame JSON (one per step)
    """

    def __init__(
        self,
        agent_id: str,
        output_dir: str | Path = ".superagent/trajectories",
        *,
        include_screenshots: bool = True,
        compress: bool = False,
    ) -> None:
        self.agent_id = agent_id
        self.output_dir = Path(output_dir)
        self.include_screenshots = include_screenshots
        self.compress = compress
        self._frame_count = 0
        self._file: Any = None
        self._path: Path | None = None
        self._start_time = 0.0
        self._objective = ""

    async def start(self, objective: str = "") -> Path:
        """Begin recording a new trajectory."""
        self.output_dir.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%dT%H%M%S")
        filename = f"{self.agent_id}_{ts}.jsonl"
        self._path = self.output_dir / filename
        self._start_time = time.time()
        self._objective = objective
        self._frame_count = 0

        # Write metadata header
        meta = TrajectoryMeta(
            agent_id=self.agent_id,
            start_time=self._start_time,
            objective=objective,
        )
        self._file = await asyncio.to_thread(open, self._path, "w", encoding="utf-8")
        await asyncio.to_thread(self._file.write, meta.to_json() + "\n")
        logger.info("TrajectoryRecorder: started → %s", self._path)
        return self._path

    async def record_step(
        self,
        screenshot: bytes | None,
        action: Any,
        result: str = "success",
        objective: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> int:
        """Record one step and return the frame ID."""
        if self._file is None:
            await self.start(objective)

        self._frame_count += 1
        screenshot_b64 = ""
        if screenshot and self.include_screenshots:
            screenshot_b64 = base64.b64encode(screenshot).decode()

        # Serialize action
        action_dict: dict[str, Any]
        if hasattr(action, "__dict__"):
            action_dict = vars(action)
            action_dict["kind"] = type(action).__name__.lower().replace("action", "")
        elif isinstance(action, dict):
            action_dict = action
        else:
            action_dict = {"raw": str(action)}

        frame = TrajectoryFrame(
            frame_id=self._frame_count,
            agent_id=self.agent_id,
            objective=objective or self._objective,
            timestamp=time.time(),
            action=action_dict,
            result=result,
            screenshot_b64=screenshot_b64,
            metadata=metadata or {},
        )
        await asyncio.to_thread(self._file.write, frame.to_json() + "\n")
        await asyncio.to_thread(self._file.flush)
        return self._frame_count

    async def stop(self, success: bool | None = None) -> None:
        """Finalise the trajectory file."""
        if self._file is None:
            return
        await asyncio.to_thread(self._file.close)
        self._file = None

        # Patch the metadata header with final stats
        if self._path and self._path.exists():
            await asyncio.to_thread(
                self._patch_meta, self._path, success
            )
        logger.info(
            "TrajectoryRecorder: stopped (%d frames, success=%s)",
            self._frame_count, success,
        )

    @staticmethod
    def _patch_meta(path: Path, success: bool | None) -> None:
        """Re-write the first line with updated metadata."""
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        if not lines:
            return
        try:
            meta = json.loads(lines[0])
            meta["end_time"] = time.time()
            meta["total_frames"] = len(lines) - 1
            meta["success"] = success
            lines[0] = json.dumps(meta) + "\n"
            path.write_text("".join(lines), encoding="utf-8")
        except Exception:
            pass

    @property
    def frame_count(self) -> int:
        return self._frame_count

    @property
    def path(self) -> Path | None:
        return self._path


# ---------------------------------------------------------------------------
# Replayer
# ---------------------------------------------------------------------------

class TrajectoryReplayer:
    """Replay a recorded trajectory from a JSONL file."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._meta: TrajectoryMeta | None = None

    def load_meta(self) -> TrajectoryMeta:
        """Load and return trajectory metadata from the first line."""
        if self._meta:
            return self._meta
        with open(self.path, encoding="utf-8") as f:
            first = f.readline().strip()
        d = json.loads(first)
        self._meta = TrajectoryMeta(**{k: d.get(k) for k in TrajectoryMeta.__dataclass_fields__})
        return self._meta

    async def steps(self) -> AsyncIterator[TrajectoryFrame]:
        """Async iterator over all trajectory frames."""
        lines = await asyncio.to_thread(self.path.read_text, encoding="utf-8")
        for i, line in enumerate(lines.splitlines()):
            if i == 0:
                continue  # skip meta header
            if not line.strip():
                continue
            yield TrajectoryFrame.from_json(line)

    async def replay(
        self,
        on_frame: Any,  # async callable(frame: TrajectoryFrame)
        *,
        speed: float = 1.0,
        skip_screenshots: bool = False,
    ) -> None:
        """Replay trajectory frames, calling on_frame for each step.

        Parameters
        ----------
        on_frame:
            Async callback called with each TrajectoryFrame.
        speed:
            Playback speed multiplier (1.0 = real time, 2.0 = double speed).
        skip_screenshots:
            If True, strip screenshots from frames before passing to callback.
        """
        prev_ts: float | None = None
        async for frame in self.steps():
            if prev_ts is not None and speed > 0:
                delay = (frame.timestamp - prev_ts) / speed
                if 0 < delay < 10:
                    await asyncio.sleep(delay)
            prev_ts = frame.timestamp

            if skip_screenshots:
                frame.screenshot_b64 = ""

            await on_frame(frame)


# ---------------------------------------------------------------------------
# Trajectory index helper
# ---------------------------------------------------------------------------

class TrajectoryIndex:
    """Index all trajectories in a directory."""

    def __init__(self, directory: str | Path = ".superagent/trajectories") -> None:
        self.directory = Path(directory)

    def list_all(self) -> list[dict[str, Any]]:
        """Return metadata for all trajectories in the directory."""
        entries = []
        for jsonl_file in sorted(self.directory.glob("*.jsonl")):
            try:
                replayer = TrajectoryReplayer(jsonl_file)
                meta = replayer.load_meta()
                entries.append({
                    "file": str(jsonl_file),
                    "agent_id": meta.agent_id,
                    "objective": meta.objective,
                    "start_time": meta.start_time,
                    "total_frames": meta.total_frames,
                    "success": meta.success,
                })
            except Exception:
                pass
        return entries

    def find_by_agent(self, agent_id: str) -> list[dict[str, Any]]:
        """Return trajectories for a specific agent."""
        return [e for e in self.list_all() if e["agent_id"] == agent_id]
