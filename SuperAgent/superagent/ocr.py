"""OCR helpers with pytesseract and EasyOCR fallbacks."""

from __future__ import annotations

import asyncio
import io
from dataclasses import dataclass
from typing import Any

from PIL import Image

try:  # pragma: no cover - optional dependency
    import pytesseract  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    pytesseract = None  # type: ignore[assignment]

try:  # pragma: no cover - optional dependency
    import easyocr  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    easyocr = None  # type: ignore[assignment]


@dataclass(slots=True)
class OCRHit:
    """OCR text result."""

    text: str
    confidence: float = 0.0
    metadata: dict[str, Any] | None = None


class OCRLayer:
    """Extract text from screenshots using available OCR engines."""

    def __init__(self, languages: list[str] | None = None) -> None:
        self.languages = languages or ["en"]
        self._reader = None

    async def extract_text(self, image: Image.Image | bytes | str) -> str:
        """Return recognized text from the supplied image."""

        pil_image = await asyncio.to_thread(_to_image, image)
        if pytesseract is not None:
            try:
                return await asyncio.to_thread(pytesseract.image_to_string, pil_image)
            except Exception:
                pass
        if easyocr is not None:
            try:
                if self._reader is None:
                    self._reader = easyocr.Reader(self.languages, gpu=False)
                lines = await asyncio.to_thread(self._reader.readtext, _image_to_array(pil_image))
                return "\n".join(line[1] for line in lines if len(line) > 1)
            except Exception:
                pass
        return ""

    async def locate_text(self, image: Image.Image | bytes | str, needle: str) -> OCRHit | None:
        """Best-effort text search helper."""

        text = await self.extract_text(image)
        if needle.lower() not in text.lower():
            return None
        return OCRHit(text=needle, confidence=0.5, metadata={"found": True})


def _to_image(image: Image.Image | bytes | str) -> Image.Image:
    if isinstance(image, Image.Image):
        return image
    if isinstance(image, bytes):
        return Image.open(io.BytesIO(image)).convert("RGB")
    return Image.open(image).convert("RGB")


def _image_to_array(image: Image.Image) -> Any:
    try:
        import numpy as np

        return np.array(image)
    except Exception:
        return image

