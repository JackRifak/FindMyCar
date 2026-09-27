"""Leave-one-out accuracy evaluation for the VPR pipeline, against a site's
already-ingested dataset (project brief Section 20/21: Top-1 accuracy,
localisation error, false-match/confusion pairs).

For each already-ingested reference image, temporarily hides it from the
candidate pool and asks: "would VPR correctly relocate this image to its
own capture location, using every OTHER image in the dataset?" This is a
real accuracy signal without needing a separate held-out capture session --
it directly tests the brief's core concern (Section 6): can the current
embedder + geometric verification actually tell locations apart, including
ones that look visually similar?

Ground truth for a query is its own (x, y) -- its capture location's
coordinates. A "correct" result is a match whose (x, y) is within
LOCATION_MATCH_TOLERANCE_M of that -- i.e. it matched some OTHER photo from
the same capture spot, not a different, wrong location.

Run:
    python scripts/evaluate_vpr_accuracy.py --site site_00
"""
from __future__ import annotations

import argparse
import math
from collections import defaultdict

import cv2
import numpy as np

from fmc.config import TOP_K_CANDIDATES, load_site_config
from fmc.dataset.schema import load_records
from fmc.vpr.geometric_verification import verify

LOCATION_MATCH_TOLERANCE_M = 0.5


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", required=True)
    parser.add_argument(
        "--top-k", type=int, default=max(TOP_K_CANDIDATES, 8),
        help="Candidates considered per query (higher than the live default, since "
             "leave-one-out removes the exact self-match)",
    )
    args = parser.parse_args()

    site = load_site_config(args.site)
    records = load_records(site.dataset_jsonl_path)
    if len(records) < 2:
        print("Need at least 2 ingested images to evaluate.")
        return

    data = np.load(site.embeddings_path, allow_pickle=True)
    ids = list(data["ids"])
    embeddings = data["embeddings"].astype(np.float32)
    norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
    norms[norms == 0] = 1e-8
    normalized = embeddings / norms

    records_by_id = {r.image_id: r for r in records}

    # (query_id, status, error_m, inlier_ratio, matched_id)
    results: list[tuple[str, str, float | None, float | None, str | None]] = []
    confusions: list[tuple[str, str, float]] = []

    for query_idx, query_id in enumerate(ids):
        query_record = records_by_id.get(query_id)
        if query_record is None:
            continue
        query_img_path = site.processed_dir / query_record.processed_path
        query_img = cv2.imread(str(query_img_path))
        if query_img is None:
            print(f"WARNING: can't read {query_img_path}, skipping")
            continue

        query_embedding = normalized[query_idx]
        similarities = normalized @ query_embedding
        similarities[query_idx] = -1.0  # exclude self -- this is the whole point of leave-one-out
        top_k_idx = np.argsort(-similarities)[: args.top_k]

        matched = False
        for cand_idx in top_k_idx:
            cand_id = ids[cand_idx]
            cand_record = records_by_id.get(cand_id)
            if cand_record is None:
                continue
            cand_img_path = site.processed_dir / cand_record.processed_path
            cand_img = cv2.imread(str(cand_img_path))
            if cand_img is None:
                continue
            verification = verify(query_img, cand_img)
            if verification.is_match:
                error = math.hypot(cand_record.x - query_record.x, cand_record.y - query_record.y)
                status = "correct" if error <= LOCATION_MATCH_TOLERANCE_M else "wrong_location"
                results.append((query_id, status, error, verification.inlier_ratio, cand_id))
                if status == "wrong_location":
                    confusions.append((query_id, cand_id, error))
                matched = True
                break

        if not matched:
            results.append((query_id, "no_match", None, None, None))

    total = len(results)
    correct = sum(1 for r in results if r[1] == "correct")
    wrong = sum(1 for r in results if r[1] == "wrong_location")
    no_match = sum(1 for r in results if r[1] == "no_match")

    print(f"Evaluated {total} images (leave-one-out) against site '{args.site}'\n")
    print(f"Correct (matched own location):         {correct}/{total}  ({100 * correct / total:.1f}%)")
    print(f"Wrong location (false match):            {wrong}/{total}  ({100 * wrong / total:.1f}%)")
    print(f"No match (nothing passed verification):  {no_match}/{total}  ({100 * no_match / total:.1f}%)")

    correct_inliers = [r[3] for r in results if r[1] == "correct"]
    if correct_inliers:
        print(
            f"\nInlier ratio for correct matches: mean={np.mean(correct_inliers):.3f}, "
            f"min={min(correct_inliers):.3f}, max={max(correct_inliers):.3f}"
        )

    if confusions:
        print("\nFalse-match pairs (locations the baseline confused with each other):")
        for query_id, matched_id, error in confusions:
            print(f"  {query_id}  ->  {matched_id}   (~{error:.1f}m apart)")

    print("\nPer-location breakdown:")
    by_location: dict[tuple, list[str]] = defaultdict(list)
    for image_id, status, _error, _inlier_ratio, _matched_id in results:
        r = records_by_id[image_id]
        key = (r.floor, r.zone, round(r.x, 2), round(r.y, 2))
        by_location[key].append(status)
    for (floor, zone, x, y), statuses in sorted(by_location.items()):
        n = len(statuses)
        c = statuses.count("correct")
        print(f"  floor={floor} zone={zone} ({x},{y}): {c}/{n} correct")


if __name__ == "__main__":
    main()
