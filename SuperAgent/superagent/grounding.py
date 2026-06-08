"""Screen grounding helpers."""

from __future__ import annotations

import io
import json
from dataclasses import dataclass
from typing import Any, Protocol

import aiohttp
from PIL import Image

from .ocr import OCRLayer


@dataclass(slots=True)
class GroundingResult:
    """Grounding result with normalized coordinates."""

    x: int
    y: int
    label: str = ""
    confidence: float = 0.0
    metadata: dict[str, Any] | None = None


class GroundingModel:
    """Base class for grounding models."""

    async def locate(self, image: Image.Image | bytes, query: str) -> GroundingResult:
        raise NotImplementedError


class CoordinateGrounding(GroundingModel):
    """Fallback grounding model based on OCR and simple heuristics."""

    def __init__(self, ocr: OCRLayer | None = None) -> None:
        self.ocr = ocr or OCRLayer()

    async def locate(self, image: Image.Image | bytes, query: str) -> GroundingResult:
        width, height = _image_size(image)
        lower = query.lower().strip()
        if lower.startswith("center"):
            return GroundingResult(x=width // 2, y=height // 2, label=query, confidence=0.3)
        if "," in query:
            try:
                x_str, y_str = query.split(",", 1)
                return GroundingResult(x=int(float(x_str)), y=int(float(y_str)), label=query, confidence=0.9)
            except ValueError:
                pass
        text = await self.ocr.extract_text(image)
        if query.lower() in text.lower():
            return GroundingResult(x=width // 2, y=height // 2, label=query, confidence=0.5)
        return GroundingResult(x=width // 2, y=height // 2, label=query, confidence=0.1)


class OSAtlasGrounding(GroundingModel):
    """Grounding implementation for OS-Atlas style services."""

    def __init__(self, endpoint: str | None = None, *, fallback: GroundingModel | None = None) -> None:
        self.endpoint = endpoint
        self.fallback = fallback or CoordinateGrounding()

    async def locate(self, image: Image.Image | bytes, query: str) -> GroundingResult:
        if not self.endpoint:
            return await self.fallback.locate(image, query)

        payload = {
            "query": query,
            "image": _image_payload(image),
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(self.endpoint, json=payload) as response:
                response.raise_for_status()
                data = await response.json()
        x = int(data.get("x", 0))
        y = int(data.get("y", 0))
        return GroundingResult(x=x, y=y, label=query, confidence=float(data.get("confidence", 0.0)), metadata=data)


def _image_size(image: Image.Image | bytes) -> tuple[int, int]:
    if isinstance(image, Image.Image):
        return image.size
    with Image.open(io.BytesIO(image)) as img:
        return img.size


def _image_payload(image: Image.Image | bytes) -> dict[str, Any]:
    if isinstance(image, Image.Image):
        return {"mode": image.mode, "size": image.size}
    return {"bytes": len(image)}
