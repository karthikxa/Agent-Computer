"""Self-of-Mark (SOM) visual tagging module for SuperAgent.

Provides visual tag overlays (bounding boxes and numeric labels) on screenshots
to allow vision/action models to reference elements by short labels.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Any

@dataclass(slots=True)
class InteractiveElement:
    """Detected interactive element coordinates and description."""

    element_id: str
    x1: int
    y1: int
    x2: int
    y2: int
    label: str = ""


class SOMVisualTagger:
    """Overlay bounding boxes and numeric identifiers onto desktop screenshots."""

    def __init__(self, border_color: tuple[int, int, int] = (255, 0, 0), text_color: tuple[int, int, int] = (255, 255, 255), fill_color: tuple[int, int, int] = (0, 0, 255)) -> None:
        self.border_color = border_color
        self.text_color = text_color
        self.fill_color = fill_color

    def tag_screenshot(
        self, png_bytes: bytes, elements: list[InteractiveElement]
    ) -> tuple[bytes, dict[str, tuple[int, int]]]:
        """Overlay numeric tags on coordinates.

        Returns
        -------
        bytes
            Annotated PNG screenshot.
        dict[str, tuple[int, int]]
            Mapping from tag ID to center coordinate (x, y) for clicks.
        """
        coord_map: dict[str, tuple[int, int]] = {}
        try:
            from PIL import Image, ImageDraw
            img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
            draw = ImageDraw.Draw(img)
        except Exception:
            # Fallback: if PIL is missing, just populate coordinates
            for el in elements:
                cx = (el.x1 + el.x2) // 2
                cy = (el.y1 + el.y2) // 2
                coord_map[el.element_id] = (cx, cy)
            return png_bytes, coord_map

        for el in elements:
            cx = (el.x1 + el.x2) // 2
            cy = (el.y1 + el.y2) // 2
            coord_map[el.element_id] = (cx, cy)

            # Draw tag box
            draw.rectangle([el.x1, el.y1, el.x2, el.y2], outline=self.border_color, width=2)
            
            # Draw label background
            label_w = 20
            label_h = 16
            draw.rectangle(
                [el.x1, el.y1 - label_h, el.x1 + label_w, el.y1],
                fill=self.fill_color,
            )
            # Draw label text
            draw.text((el.x1 + 4, el.y1 - label_h + 2), el.element_id, fill=self.text_color)

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue(), coord_map
