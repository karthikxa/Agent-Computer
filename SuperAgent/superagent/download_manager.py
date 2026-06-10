"""Download manager — track and retrieve agent file downloads.

Feature #76 — inspired by e2b/open-computer-use.

Tracks files downloaded by an agent (via browser automation or direct HTTP),
provides progress callbacks, stores metadata, and makes files available for
retrieval by the dashboard or orchestrator.

Usage::

    dm = DownloadManager(agent_id="agent-1", download_dir="/tmp/agent-downloads")

    # Direct HTTP download with progress
    result = await dm.download("https://example.com/file.zip")
    print(result.path, result.size_bytes)

    # Register a file already downloaded by the browser
    dm.register("report.pdf", "/path/to/report.pdf")

    # List all downloads
    for d in dm.list_downloads():
        print(d)
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

@dataclass
class DownloadRecord:
    """Metadata about a single download."""
    download_id: str
    agent_id: str
    url: str
    filename: str
    path: str
    size_bytes: int
    status: str          # "pending" | "downloading" | "complete" | "error"
    started_at: float
    completed_at: float | None = None
    error: str | None = None
    mime_type: str = ""
    progress_pct: float = 0.0


class DownloadFailedError(RuntimeError):
    """Raised when a download fails."""
    pass


# ---------------------------------------------------------------------------
# Download Manager
# ---------------------------------------------------------------------------

class DownloadManager:
    """Track, execute, and retrieve file downloads for an agent."""

    def __init__(
        self,
        agent_id: str,
        download_dir: str | Path | None = None,
    ) -> None:
        self.agent_id = agent_id
        self.download_dir = Path(download_dir or f".superagent/downloads/{agent_id}")
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self._records: dict[str, DownloadRecord] = {}

    # ------------------------------------------------------------------
    # Direct HTTP download
    # ------------------------------------------------------------------

    async def download(
        self,
        url: str,
        *,
        filename: str | None = None,
        on_progress: Callable[[float], None] | None = None,
        timeout: float = 300.0,
        headers: dict[str, str] | None = None,
    ) -> DownloadRecord:
        """Download a file from URL with progress tracking.

        Parameters
        ----------
        url:
            The URL to download from.
        filename:
            Override the saved filename (default: derived from URL).
        on_progress:
            Optional callback called with progress percentage (0–100).
        timeout:
            Total download timeout in seconds.
        headers:
            Optional HTTP headers.
        """
        import secrets
        download_id = secrets.token_hex(6)
        fname = filename or self._filename_from_url(url)
        dest_path = self.download_dir / fname
        # Avoid overwrites
        if dest_path.exists():
            stem = dest_path.stem
            suffix = dest_path.suffix
            dest_path = self.download_dir / f"{stem}_{download_id}{suffix}"

        record = DownloadRecord(
            download_id=download_id,
            agent_id=self.agent_id,
            url=url,
            filename=dest_path.name,
            path=str(dest_path),
            size_bytes=0,
            status="pending",
            started_at=time.time(),
        )
        self._records[download_id] = record

        try:
            import aiohttp
            record.status = "downloading"
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    headers=headers or {},
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as resp:
                    resp.raise_for_status()
                    total = int(resp.headers.get("Content-Length", 0))
                    record.mime_type = resp.headers.get("Content-Type", "")
                    downloaded = 0
                    with open(dest_path, "wb") as f:
                        async for chunk in resp.content.iter_chunked(65536):
                            f.write(chunk)
                            downloaded += len(chunk)
                            if total > 0:
                                record.progress_pct = (downloaded / total) * 100
                                if on_progress:
                                    on_progress(record.progress_pct)
                    record.size_bytes = downloaded
                    record.progress_pct = 100.0
            record.status = "complete"
            record.completed_at = time.time()
            logger.info(
                "DownloadManager[%s]: downloaded '%s' (%.1f KB)",
                self.agent_id, fname, record.size_bytes / 1024,
            )
        except Exception as exc:
            record.status = "error"
            record.error = str(exc)
            logger.error("DownloadManager[%s]: download failed for %s: %s", self.agent_id, url, exc)
            raise DownloadFailedError(f"Download failed: {exc}") from exc

        return record

    # ------------------------------------------------------------------
    # Register browser-downloaded files
    # ------------------------------------------------------------------

    def register(
        self,
        filename: str,
        path: str,
        *,
        url: str = "",
        mime_type: str = "",
    ) -> DownloadRecord:
        """Register an already-downloaded file (e.g. from Playwright)."""
        import secrets
        download_id = secrets.token_hex(6)
        p = Path(path)
        size = p.stat().st_size if p.exists() else 0
        record = DownloadRecord(
            download_id=download_id,
            agent_id=self.agent_id,
            url=url,
            filename=filename,
            path=path,
            size_bytes=size,
            status="complete",
            started_at=time.time(),
            completed_at=time.time(),
            mime_type=mime_type,
            progress_pct=100.0,
        )
        self._records[download_id] = record
        return record

    # ------------------------------------------------------------------
    # Retrieval
    # ------------------------------------------------------------------

    def list_downloads(self, status: str | None = None) -> list[dict[str, Any]]:
        """List all downloads, optionally filtered by status."""
        records = list(self._records.values())
        if status:
            records = [r for r in records if r.status == status]
        return [
            {
                "download_id": r.download_id,
                "filename": r.filename,
                "url": r.url,
                "path": r.path,
                "size_bytes": r.size_bytes,
                "status": r.status,
                "progress_pct": r.progress_pct,
                "started_at": r.started_at,
                "completed_at": r.completed_at,
                "mime_type": r.mime_type,
                "error": r.error,
            }
            for r in sorted(records, key=lambda r: r.started_at, reverse=True)
        ]

    def get(self, download_id: str) -> DownloadRecord | None:
        return self._records.get(download_id)

    def get_path(self, download_id: str) -> Path | None:
        record = self._records.get(download_id)
        if record and record.status == "complete":
            return Path(record.path)
        return None

    def delete(self, download_id: str) -> bool:
        record = self._records.get(download_id)
        if not record:
            return False
        try:
            Path(record.path).unlink(missing_ok=True)
        except Exception:
            pass
        del self._records[download_id]
        return True

    def total_size_bytes(self) -> int:
        return sum(r.size_bytes for r in self._records.values() if r.status == "complete")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _filename_from_url(url: str) -> str:
        parsed = urlparse(url)
        name = Path(parsed.path).name
        return name if name else "download"
