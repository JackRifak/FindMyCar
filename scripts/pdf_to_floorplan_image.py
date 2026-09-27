"""Render a floor-plan PDF page to a PNG image, for georeferencing and
location picking.

Run:
    python scripts/pdf_to_floorplan_image.py path/to/floorplan.pdf --site mock_site --page 0 --dpi 200

Higher DPI = more precision when clicking points later, at the cost of a
bigger image file. 200 is a reasonable starting point for a single-floor
parking plan; go higher (300+) if the drawing is large or fine-grained.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from fmc.config import DATA_ROOT
from fmc.pdf_render import render_pdf_page


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("pdf_path", type=Path)
    parser.add_argument("--site", default="mock_site")
    parser.add_argument("--page", type=int, default=0, help="0-indexed page number")
    parser.add_argument("--dpi", type=int, default=200)
    args = parser.parse_args()

    out_path = DATA_ROOT / args.site / "floorplan" / "floorplan.png"
    width, height = render_pdf_page(args.pdf_path, args.page, args.dpi, out_path)
    print(f"Rendered page {args.page} at {args.dpi} DPI -> {out_path} ({width}x{height}px)")


if __name__ == "__main__":
    main()
