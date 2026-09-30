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
import hashlib
import json
import math
from collections import defaultdict
from pathlib import Path

import cv2
import numpy as np

from fmc.config import (
    GEOMETRIC_INLIER_RATIO_THRESHOLD,
    ORB_MIN_MATCH_COUNT,
    TOP_K_CANDIDATES,
    load_site_config,
)
from fmc.dataset.schema import load_records
from fmc.vpr.geometric_verification import verify
from fmc.vpr.embedders.registry import available_embedders, get_benchmark_embedder

LOCATION_MATCH_TOLERANCE_M = 0.5


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", required=True)
    parser.add_argument(
        "--embedder",
        choices=["production", *available_embedders()],
        default="production",
        help="Use existing production index or recompute using a benchmark embedder",
    )
    parser.add_argument(
        "--device",
        default="cpu",
        help="Torch device for a selected benchmark embedder (e.g. cpu, cuda, cuda:0)",
    )
    parser.add_argument("--out", type=Path, default=None, help="Optional JSON path for per-image results")
    parser.add_argument("--ransac-seed", type=int, default=0, help="Base seed for repeatable per-pair RANSAC")
    parser.add_argument(
        "--threshold-sweep",
        action="store_true",
        help="Report end-to-end outcomes over ORB inlier-ratio thresholds 0.10-0.70",
    )
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

    records_by_id = {r.image_id: r for r in records}
    if args.embedder == "production":
        with np.load(site.embeddings_path, allow_pickle=True) as data:
            ids = [str(image_id) for image_id in data["ids"]]
            embeddings = data["embeddings"].astype(np.float32)
        norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
        norms[norms == 0] = 1e-8
        normalized = embeddings / norms
        similarities = normalized @ normalized.T
        embedder_dim = int(embeddings.shape[1])
    else:
        embedder = get_benchmark_embedder(args.embedder, device=args.device)
        images = {}
        for record in records:
            image_path = site.processed_dir / record.processed_path
            image = cv2.imread(str(image_path))
            if image is not None:
                images[record.image_id] = image
            else:
                print(f"WARNING: can't read {image_path}, skipping {record.image_id}")
        ids = [record.image_id for record in records if record.image_id in images]
        if hasattr(embedder, "fit_clusters"):
            step = max(1, len(ids) // 40)
            sample_ids = ids[::step][:40]
            embedder.fit_clusters([images[image_id] for image_id in sample_ids])
        if hasattr(embedder, "score_matrix"):
            similarities, _latencies = embedder.score_matrix([images[image_id] for image_id in ids])
        else:
            embeddings = np.stack([embedder.embed(images[image_id]) for image_id in ids]).astype(np.float32)
            norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
            norms[norms == 0] = 1e-8
            normalized = embeddings / norms
            similarities = normalized @ normalized.T
        embedder_dim = embedder.dim
        print(f"Using {args.embedder} embeddings: dimension={embedder_dim}, device={args.device}")

    # (query_id, [(candidate_id, error_m, verification)])
    candidate_evidence = []

    for query_idx, query_id in enumerate(ids):
        query_record = records_by_id.get(query_id)
        if query_record is None:
            continue
        query_img_path = site.processed_dir / query_record.processed_path
        query_img = cv2.imread(str(query_img_path))
        if query_img is None:
            print(f"WARNING: can't read {query_img_path}, skipping")
            continue

        query_similarities = similarities[query_idx].copy()
        query_similarities[query_idx] = -np.inf  # exclude self -- leave-one-out
        top_k_idx = np.argsort(-query_similarities)[: args.top_k]

        query_evidence = []
        for cand_idx in top_k_idx:
            cand_id = ids[cand_idx]
            cand_record = records_by_id.get(cand_id)
            if cand_record is None:
                continue
            cand_img_path = site.processed_dir / cand_record.processed_path
            cand_img = cv2.imread(str(cand_img_path))
            if cand_img is None:
                continue
            seed_material = f"{args.ransac_seed}:{query_id}:{cand_id}".encode("utf-8")
            pair_seed = int.from_bytes(hashlib.blake2s(seed_material, digest_size=4).digest(), "little")
            cv2.setRNGSeed(pair_seed & 0x7FFFFFFF)
            verification = verify(query_img, cand_img)
            error = math.hypot(cand_record.x - query_record.x, cand_record.y - query_record.y)
            query_evidence.append((cand_id, error, verification))
        candidate_evidence.append((query_id, query_evidence))

    def resolve_at_threshold(threshold: float):
        resolved = []
        for query_id, candidates in candidate_evidence:
            result = (query_id, "no_match", None, None, None)
            for candidate_id, error, verification in candidates:
                if (
                    verification.num_matches >= ORB_MIN_MATCH_COUNT
                    and verification.inlier_ratio >= threshold
                ):
                    status = "correct" if error <= LOCATION_MATCH_TOLERANCE_M else "wrong_location"
                    result = (query_id, status, error, verification.inlier_ratio, candidate_id)
                    break
            resolved.append(result)
        return resolved

    results = resolve_at_threshold(GEOMETRIC_INLIER_RATIO_THRESHOLD)
    confusions = [(r[0], r[4], r[2]) for r in results if r[1] == "wrong_location"]

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

    focus_ids = {"F01_ZB_00008_202", "F01_ZB_00028_284"}
    threshold_sweep = []
    if args.threshold_sweep:
        print("\nEnd-to-end threshold sweep (same retrieval candidates; geometry scored once):")
        thresholds = sorted(set(round(float(t), 2) for t in np.arange(0.10, 0.701, 0.05)) | {GEOMETRIC_INLIER_RATIO_THRESHOLD})
        for threshold in thresholds:
            threshold_results = resolve_at_threshold(threshold)
            threshold_correct = sum(r[1] == "correct" for r in threshold_results)
            threshold_wrong = sum(r[1] == "wrong_location" for r in threshold_results)
            threshold_no_match = sum(r[1] == "no_match" for r in threshold_results)
            target_results = {
                r[0]: {
                    "status": r[1],
                    "matched_image_id": r[4],
                    "location_error_m": r[2],
                    "inlier_ratio": r[3],
                }
                for r in threshold_results
                if r[0] in focus_ids
            }
            weak_location_results = [
                r for r in threshold_results
                if (round(records_by_id[r[0]].x, 2), round(records_by_id[r[0]].y, 2)) == (-12.56, -4.09)
            ]
            weak_location = {
                "correct": sum(r[1] == "correct" for r in weak_location_results),
                "wrong_location": sum(r[1] == "wrong_location" for r in weak_location_results),
                "no_match": sum(r[1] == "no_match" for r in weak_location_results),
                "total": len(weak_location_results),
            }
            row = {
                "threshold": threshold,
                "correct": threshold_correct,
                "wrong_location": threshold_wrong,
                "no_match": threshold_no_match,
                "correct_rate": threshold_correct / total if total else 0.0,
                "target_results": target_results,
                "weak_location": weak_location,
            }
            threshold_sweep.append(row)
            print(
                f"  {threshold:.2f}: correct={threshold_correct}/{total} "
                f"wrong={threshold_wrong} no_match={threshold_no_match}; "
                f"weak_location={weak_location['correct']}/{weak_location['total']} correct"
            )
            for image_id, outcome in target_results.items():
                print(
                    f"    {image_id}: {outcome['status']} "
                    f"(matched={outcome['matched_image_id']})"
                )

    if any(result[0] in focus_ids for result in results):
        print("\nRequested attractor photos:")
        for image_id, status, error, inlier_ratio, matched_id in results:
            if image_id in focus_ids:
                error_text = "n/a" if error is None else f"{error:.2f}m"
                inlier_text = "n/a" if inlier_ratio is None else f"{inlier_ratio:.3f}"
                print(
                    f"  {image_id}: {status}; matched={matched_id}; "
                    f"location_error={error_text}; inlier_ratio={inlier_text}"
                )

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "site": args.site,
                    "embedder": args.embedder,
                    "device": args.device,
                    "ransac_seed": args.ransac_seed,
                    "embedding_dim": embedder_dim,
                    "top_k": args.top_k,
                    "total": total,
                    "correct": correct,
                    "wrong_location": wrong,
                    "no_match": no_match,
                    "default_inlier_ratio_threshold": GEOMETRIC_INLIER_RATIO_THRESHOLD,
                    "threshold_sweep": threshold_sweep,
                    "results": [
                        {
                            "image_id": image_id,
                            "status": status,
                            "location_error_m": error,
                            "inlier_ratio": inlier_ratio,
                            "matched_image_id": matched_id,
                        }
                        for image_id, status, error, inlier_ratio, matched_id in results
                    ],
                },
                f,
                indent=2,
            )
        print(f"\nWrote per-image results to {args.out}")


if __name__ == "__main__":
    main()
