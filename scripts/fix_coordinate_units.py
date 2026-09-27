"""One-off fix: rescale x/y coordinates in a site's dataset.jsonl,
locations.csv, and floorplan/transform.json by a given factor.

Use this if coordinates were captured in the wrong unit -- e.g. control
points were given in millimeters instead of meters
(docs/01_coordinate_system.md specifies meters). Rescaling the saved
transform (not just the already-computed data) keeps FUTURE
compute_location_coords.py runs correct too, in case you add more
locations to this site later.

Does NOT touch embeddings.npz or re-process any images -- VPR search
re-reads dataset.jsonl fresh on every pipeline load, so fixing the metadata
alone is enough; no need to rebuild the index.

Run (millimeters -> meters, the common case):
    python scripts/fix_coordinate_units.py --site site_00 --factor 0.001
"""
from __future__ import annotations

import argparse

from fmc.config import load_site_config
from fmc.dataset.rescale import rescale_dataset_jsonl, rescale_locations_csv, rescale_transform


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", required=True)
    parser.add_argument(
        "--factor", type=float, required=True,
        help="Multiply existing x/y values by this (e.g. 0.001 for mm -> m)",
    )
    args = parser.parse_args()

    site = load_site_config(args.site)

    n_records = rescale_dataset_jsonl(site.dataset_jsonl_path, args.factor)
    print(
        f"Rescaled {n_records} records in {site.dataset_jsonl_path}"
        if n_records else f"No dataset.jsonl found at {site.dataset_jsonl_path} (skipped)"
    )

    locations_path = site.index_dir / "locations.csv"
    n_locations = rescale_locations_csv(locations_path, args.factor)
    print(
        f"Rescaled {n_locations} rows in {locations_path}"
        if n_locations else f"No locations.csv found at {locations_path} (skipped)"
    )

    transform_path = site.data_dir / "floorplan" / "transform.json"
    if rescale_transform(transform_path, args.factor):
        print(f"Rescaled transform -> {transform_path} (future compute_location_coords.py runs will output the corrected unit too)")
    else:
        print(f"No transform.json found at {transform_path} (skipped)")

    print("\nDone. embeddings.npz was not touched -- no need to rebuild the index.")


if __name__ == "__main__":
    main()
