"""Render a floor-plan PDF page to a raster image.

Shared by scripts/pdf_to_floorplan_image.py (CLI) and fmc.webtools (browser
tool) so both paths produce identical output -- this is the pixel space
every downstream coordinate (control points, capture locations, walkable
segments, vehicle slots) is measured in.
"""
from __future__ import annotations

from pathlib import Path

import fitz  # PyMuPDF


def render_pdf_page(pdf_path: Path, page_index: int, dpi: int, out_path: Path) -> tuple[int, int]:
    """Render one page of a PDF to a PNG at the given DPI. Returns (width, height) in pixels."""
    doc = fitz.open(pdf_path)
    if page_index >= len(doc):
        raise ValueError(f"PDF only has {len(doc)} page(s), asked for page {page_index}")
    page = doc[page_index]
    zoom = dpi / 72.0
    matrix = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=matrix)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pix.save(str(out_path))
    return pix.width, pix.height


def pdf_page_count(pdf_path: Path) -> int:
    return len(fitz.open(pdf_path))
