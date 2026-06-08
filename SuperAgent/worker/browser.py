"""Browser automation worker built on Playwright."""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image

from superagent.providers import OSAtlasProvider


@dataclass(slots=True)
class BrowserWorker:
    """Automate a Chromium browser on the KasmVNC desktop."""

    vision_provider: OSAtlasProvider | None = None
    downloads: list[dict[str, Any]] = field(default_factory=list)
    _playwright: Any | None = None
    _browser: Any | None = None
    _context: Any | None = None
    _page: Any | None = None

    async def _ensure(self) -> None:
        """Create Playwright objects on demand."""

        if self._page is not None:
            return
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=False)
        self._context = await self._browser.new_context(viewport={"width": 1920, "height": 1080})
        self._context.on("download", lambda download: self._track_download(download))
        self._page = await self._context.new_page()

    def _track_download(self, download: Any) -> None:
        self.downloads.append({
            "url": download.url,
            "suggested_filename": download.suggested_filename,
            "path": None,
            "status": "started"
        })
        asyncio.create_task(self._wait_for_download(download))

    async def _wait_for_download(self, download: Any) -> None:
        try:
            path = await download.path()
            for dl in self.downloads:
                if dl["url"] == download.url:
                    dl["path"] = str(path)
                    dl["status"] = "completed"
        except Exception as e:
            for dl in self.downloads:
                if dl["url"] == download.url:
                    dl["status"] = f"failed: {e}"

    async def open(self, url: str) -> None:
        """Open a URL in a headed Chromium window."""

        await self._ensure()
        await self._page.goto(url, wait_until="networkidle")

    async def navigate(self, url: str) -> None:
        """Navigate to a URL."""

        await self._ensure()
        await self._page.goto(url, wait_until="networkidle")

    async def click(self, description: str) -> None:
        """Click an element described in natural language."""

        await self._ensure()
        try:
            locator = self._page.get_by_text(description, exact=False).first
            if await locator.count():
                await locator.click()
                return
        except Exception:
            pass
        screenshot = await self.screenshot()
        if self.vision_provider is None:
            raise RuntimeError(f"Could not locate '{description}' and no vision provider configured")
        x, y = await self.vision_provider.locate(base64.b64encode(screenshot).decode("ascii"), description)
        await self._page.mouse.click(x, y)

    async def fill_form(self, fields: dict[str, str]) -> None:
        """Fill form fields by label."""

        await self._ensure()
        for label, value in fields.items():
            try:
                await self._page.get_by_label(label, exact=False).fill(value)
            except Exception:
                await self.click(label)
                await self._page.keyboard.type(value)

    async def scroll(self, direction: str, amount: int) -> None:
        """Scroll the page."""

        await self._ensure()
        delta = amount if direction.lower() in {"down", "right"} else -amount
        await self._page.mouse.wheel(0, delta)

    async def wait_for(self, description: str, timeout: int = 30) -> None:
        """Wait until described content appears."""

        await self._ensure()
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            try:
                if await self._page.get_by_text(description, exact=False).count():
                    return
            except Exception:
                pass
            await asyncio.sleep(1)
        raise TimeoutError(description)

    async def extract_text(self, region: tuple[int, int, int, int]) -> str:
        """Extract text from a region using OCR."""

        from superagent.ocr import OCRLayer

        screenshot = await self.screenshot()
        image = Image.open(BytesIO(screenshot))
        crop = image.crop(region)
        return await OCRLayer().extract_text(crop)

    async def download(self, url: str) -> str:
        """Download a file and return the local path."""

        await self._ensure()
        path = await self._page.evaluate(
            """async (url) => {
                const response = await fetch(url);
                const blob = await response.blob();
                const arrayBuffer = await blob.arrayBuffer();
                return Array.from(new Uint8Array(arrayBuffer));
            }""",
            url,
        )
        out = Path("/tmp") / Path(url).name
        out.write_bytes(bytes(path))
        return str(out)

    async def get_page_text(self) -> str:
        """Return all visible page text."""

        await self._ensure()
        return await self._page.text_content("body") or ""

    async def screenshot(self) -> bytes:
        """Return a browser-only PNG screenshot."""

        await self._ensure()
        return await self._page.screenshot(type="png", full_page=True)

    async def new_tab(self, url: str | None = None) -> int:
        """Open a new tab and return its index."""
        await self._ensure()
        page = await self._context.new_page()
        if url:
            await page.goto(url, wait_until="networkidle")
        self._page = page
        return len(self._context.pages) - 1

    async def switch_tab(self, index: int) -> None:
        """Switch active tab to index."""
        await self._ensure()
        pages = self._context.pages
        if 0 <= index < len(pages):
            self._page = pages[index]
        else:
            raise IndexError("Tab index out of range")

    async def close_tab(self, index: int) -> None:
        """Close tab at index."""
        await self._ensure()
        pages = self._context.pages
        if 0 <= index < len(pages):
            await pages[index].close()
            if self._page not in self._context.pages:
                self._page = self._context.pages[-1] if self._context.pages else None

    async def upload_file(self, element_description: str, file_path: str) -> None:
        """Locate file input and upload file."""
        await self._ensure()
        try:
            await self._page.get_by_label(element_description, exact=False).set_input_files(file_path)
        except Exception:
            await self._page.locator("input[type=file]").first.set_input_files(file_path)

    async def close(self) -> None:
        """Close browser resources."""

        if self._browser is not None:
            await self._browser.close()
        if self._playwright is not None:
            await self._playwright.stop()
