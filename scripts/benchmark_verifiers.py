"""Benchmark harness for second-stage geometric verification methods
(Deliverable 5): ORB+RANSAC baseline vs. SuperPoint+SuperGlue, SuperPoint+
LightGlue, LoFTR.

Builds a labeled pair set directly from the site's own real confusions --
reuses Stage 1 (whatever embedder is CURRENTLY indexed in embeddings.npz)
via leave-one-out retrieval, the same way evaluate_vpr_accuracy.py does, but
instead of stopping at the first candidate that passes verification, it
labels EVERY retrieved candidate as a true-match or hard-negative pair and
scores each verifier's ability to tell them apart. This isolates Stage 2 so
it can be benchmarked independently of whichever embedder produced the
candidates.

Reports, per verifier:
  - true-positive verification rate / false-positive rate at the verifier's
    own configured inlier-ratio threshold
  - a threshold sweep (precision/recall at 21 inlier-ratio cutoffs) to find
    the best achievable threshold for THIS site's data, not just report a
    single number tuned elsewhere
  - genuine vs. impostor inlier-ratio distributions
  - accuracy broken out by texture richness (ORB keypoint count in the
    query image as a cheap, verifier-independent proxy) -- the split most
    relevant to a parking garage's blank walls / repetitive flooring
  - runtime per pair (mean, p95) -- checked against this project's tight
    end-to-end latency budget

Run:
    python scripts/benchmark_verifiers.py --site site_00 \\
        --verifiers orb_ransac superpoint_lightglue loftr \\
        --out data/site_00/benchmarks/verifiers_report.json

List available verifiers:
    python scripts/benchmark_verifiers.py --list
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from fmc.config import TOP_K_CANDIDATES, load_site_config
from fmc.dataset.schema import ReferenceImage, load_records
from fmc.vpr.verifiers.registry import available_verifiers, get_benchmark_verifier

LOCATION_MATCH_TOLERANCE_M = 0.5
_orb_for_texture = cv2.ORB_create(nfeatures=1000)


@dataclass
class Pair:
    query_id: str
    candidate_id: str
    is_true_match: bool
    error_m: float


def is_same_location(a: ReferenceImage, b: ReferenceImage) -> bool:
    if a.location_id and b.location_id:
        return a.location_id == b.location_id
    return math.hypot(a.x - b.x, a.y - b.y) <= LOCATION_MATCH_TOLERANCE_M


def texture_bucket(image: np.ndarray) -> str:
    """Cheap texture-richness proxy: ORB keypoint count in the query image,
    independent of whichever verifier is under test."""
    kp, _ = _orb_for_texture.detectAndCompute(image, None)
    n = len(kp) if kp else 0
    if n < 50:
        return "low_texture"
    if n < 200:
        return "medium_texture"
    return "high_texture"


def build_pairs(site, records: list[ReferenceImage], top_k: int) -> list[Pair]:
    data = np.load(site.embeddings_path, allow_pickle=True)
    ids = list(data["ids"])
    embeddings = data["embeddings"].astype(np.float32)
    norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
    norms[norms == 0] = 1e-8
    normalized = embeddings / norms
    records_by_id = {r.image_id: r for r in records}

    pairs = []
    for i, query_id in enumerate(ids):
        query_record = records_by_id.get(str(query_id))
        if query_record is None:
            continue
        sims = normalized @ normalized[i]
        sims[i] = -1.0
        top_k_idx = np.argsort(-sims)[:top_k]
        for j in top_k_idx:
            cand_id = str(ids[j])
            cand_record = records_by_id.get(cand_id)
            if cand_record is None:
                continue
            error = math.hypot(cand_record.x - query_record.x, cand_record.y - query_record.y)
            is_true = is_same_location(query_record, cand_record)
            pairs.append(Pair(query_id=str(query_id), candidate_id=cand_id, is_true_match=is_true, error_m=error))
    return pairs


def evaluate_verifier(name: str, image_cache: dict[str, np.ndarray], pairs: list[Pair]) -> dict:
    verifier = get_benchmark_verifier(name)

    rows = []  # (is_true, inlier_ratio, latency_ms, texture_bucket, accepted)
    for pair in pairs:
        query_img = image_cache.get(pair.query_id)
        cand_img = image_cache.get(pair.candidate_id)
        if query_img is None or cand_img is None:
            continue
        result = verifier.verify(query_img, cand_img)
        bucket = texture_bucket(query_img)
        rows.append((pair.is_true_match, result.inlier_ratio, result.latency_ms, bucket, result.is_match))

    if not rows:
        return {"verifier": name, "error": "no pairs could be scored (missing images?)"}

    is_true = np.array([r[0] for r in rows])
    inlier_ratio = np.array([r[1] for r in rows])
    latency = np.array([r[2] for r in rows])
    buckets = np.array([r[3] for r in rows])
    accepted = np.array([r[4] for r in rows])

    tp = int(np.sum(is_true & accepted))
    fn = int(np.sum(is_true & ~accepted))
    fp = int(np.sum(~is_true & accepted))
    tn = int(np.sum(~is_true & ~accepted))
    tpr = tp / (tp + fn) if (tp + fn) else None
    fpr = fp / (fp + tn) if (fp + tn) else None
    precision = tp / (tp + fp) if (tp + fp) else None

    thresholds = np.linspace(0.0, 1.0, 21)
    sweep = []
    for t in thresholds:
        pred = inlier_ratio >= t
        tp_s = int(np.sum(is_true & pred))
        fp_s = int(np.sum(~is_true & pred))
        fn_s = int(np.sum(is_true & ~pred))
        prec = tp_s / (tp_s + fp_s) if (tp_s + fp_s) else None
        rec = tp_s / (tp_s + fn_s) if (tp_s + fn_s) else None
        sweep.append({"threshold": round(float(t), 3), "precision": prec, "recall": rec})

    def f1(entry):
        p, r = entry["precision"], entry["recall"]
        if not p or not r or (p + r) == 0:
            return -1.0
        return 2 * p * r / (p + r)

    scored = [s for s in sweep if s["precision"] is not None and s["recall"] is not None]
    best = max(scored, key=f1, default=None)

    by_bucket = {}
    for bucket_name in ("low_texture", "medium_texture", "high_texture"):
        mask = buckets == bucket_name
        if mask.sum() == 0:
            continue
        true_in_bucket = is_true[mask]
        false_in_bucket = ~is_true[mask]
        by_bucket[bucket_name] = {
            "n": int(mask.sum()),
            "true_positive_rate": float(np.sum(true_in_bucket & accepted[mask]) / max(int(true_in_bucket.sum()), 1)),
            "false_positive_rate": float(
                np.sum(false_in_bucket & accepted[mask]) / max(int(false_in_bucket.sum()), 1)
            ),
        }

    return {
        "verifier": name,
        "num_pairs": len(rows),
        "num_true_match_pairs": int(is_true.sum()),
        "num_hard_negative_pairs": int((~is_true).sum()),
        "at_configured_threshold": {
            "true_positive_rate": tpr,
            "false_positive_rate": fpr,
            "precision": precision,
        },
        "genuine_inlier_ratio_mean": float(np.mean(inlier_ratio[is_true])) if is_true.any() else None,
        "impostor_inlier_ratio_mean": float(np.mean(inlier_ratio[~is_true])) if (~is_true).any() else None,
        "best_threshold_by_f1": best,
        "threshold_sweep": sweep,
        "by_texture_bucket": by_bucket,
        "latency_ms_mean": float(np.mean(latency)),
        "latency_ms_p95": float(np.percentile(latency, 95)),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--site")
    parser.add_argument(
        "--verifiers", nargs="+", default=None,
        help="Verifier names to benchmark (default: all registered)",
    )
    parser.add_argument("--top-k", type=int, default=max(TOP_K_CANDIDATES, 8))
    parser.add_argument("--out", default=None)
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()

    if args.list:
        print("Available verifiers:")
        for name in available_verifiers():
            print(f"  {name}")
        return

    if not args.site:
        parser.error("--site is required (unless using --list)")

    site = load_site_config(args.site)
    records = load_records(site.dataset_jsonl_path)
    if len(records) < 3:
        print("Need at least 3 ingested images to benchmark verification meaningfully.")
        return

    pairs = build_pairs(site, records, args.top_k)
    print(
        f"Built {len(pairs)} labeled pairs "
        f"({sum(p.is_true_match for p in pairs)} true-match, "
        f"{sum(not p.is_true_match for p in pairs)} hard-negative) "
        f"from the currently indexed embeddings."
    )

    records_by_id = {r.image_id: r for r in records}
    needed_ids = {p.query_id for p in pairs} | {p.candidate_id for p in pairs}
    image_cache = {}
    for image_id in needed_ids:
        r = records_by_id.get(image_id)
        if r is None:
            continue
        img = cv2.imread(str(site.processed_dir / r.processed_path))
        if img is not None:
            image_cache[image_id] = img

    verifier_names = args.verifiers or available_verifiers()
    reports = []
    for name in verifier_names:
        print(f"\n=== Benchmarking verifier: {name} ===")
        try:
            report = evaluate_verifier(name, image_cache, pairs)
        except ImportError as e:
            print(f"  SKIPPED ({e})")
            continue
        reports.append(report)
        if "error" in report:
            print(f"  ERROR: {report['error']}")
            continue
        tpr = report["at_configured_threshold"]["true_positive_rate"]
        fpr = report["at_configured_threshold"]["false_positive_rate"]
        print(
            f"  TP rate={tpr:.3f}  FP rate={fpr:.3f}  "
            f"latency mean={report['latency_ms_mean']:.1f}ms p95={report['latency_ms_p95']:.1f}ms"
        )
        if report["best_threshold_by_f1"]:
            b = report["best_threshold_by_f1"]
            print(
                f"  best inlier-ratio threshold for this site: {b['threshold']} "
                f"(precision={b['precision']:.3f}, recall={b['recall']:.3f})"
            )
        print("  by texture bucket (true-positive rate / false-positive rate):")
        for bucket, stats in report["by_texture_bucket"].items():
            print(f"    {bucket} (n={stats['n']}): TP={stats['true_positive_rate']:.3f} FP={stats['false_positive_rate']:.3f}")

    print("\n=== Summary ===")
    for report in reports:
        if "error" in report:
            continue
        tpr = report["at_configured_threshold"]["true_positive_rate"]
        fpr = report["at_configured_threshold"]["false_positive_rate"]
        print(f"  {report['verifier']:28s} TP={tpr:.3f} FP={fpr:.3f} latency={report['latency_ms_mean']:.1f}ms")

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w") as f:
            json.dump(reports, f, indent=2)
        print(f"\nWrote full report to {out_path}")


if __name__ == "__main__":
    main()
