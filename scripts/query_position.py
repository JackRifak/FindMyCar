"""Quick end-to-end test: localize a query image against a site's built index.

Run after building the index:
    python scripts/query_position.py path/to/query.jpg --site site_00
Should print the matched floor/zone/x/y, the embedding similarity, and the
geometric-verification inlier ratio that confirmed it.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import cv2

from fmc.config import load_site_config
from fmc.vpr.pipeline import VPRPipeline

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("query_image", type=Path, help="Path to any image to localize (doesn't need to already be in the dataset)")
    parser.add_argument("--site", default="mock_site")
    args = parser.parse_args()

    site = load_site_config(args.site)
    pipeline = VPRPipeline(site)

    query_img = cv2.imread(str(args.query_image))
    if query_img is None:
        print(f"Could not read {args.query_image}")
        return

    result = pipeline.localize(query_img)
    if result.matched:
        r = result.record
        print(
            f"MATCH: {r.image_id}  floor={r.floor} zone={r.zone} "
            f"x={r.x} y={r.y}  similarity={result.similarity:.3f} "
            f"inlier_ratio={result.inlier_ratio:.3f}"
        )
    else:
        print("NO MATCH — no candidate survived geometric verification.")


if __name__ == "__main__":
    main()
