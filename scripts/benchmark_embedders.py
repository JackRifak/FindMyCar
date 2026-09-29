"""Benchmark harness for VPR embedding models (Deliverable 4).

Leave-one-out RETRIEVAL evaluation across candidate embedders (color
histogram baseline, CLIP variants, DINOv2, NetVLAD, ...) against a site's
already-ingested real dataset. This mirrors evaluate_vpr_accuracy.py's
leave-one-out philosophy but scores Stage 1 (embedding/retrieval) directly
and in isolation from Stage 2 (geometric verification), so the two stages
can be evaluated and swapped independently.

Metrics reported per embedder:
  - Recall@1, Recall@5, mAP over leave-one-out retrieval
  - genuine vs. impostor similarity-score separation (mean/std of each,
    and the gap between them -- tells you whether a similarity threshold
    could ever cleanly separate true from false matches on this site's data)
  - heading-bucketed Recall@1 (0-45 / 45-90 / 90-180 degrees between the
    query's and the top-1 candidate's stored heading) -- the viewpoint-
    robustness signal that matters most for walking-in-an-arbitrary-
    direction VPR
  - embedding latency (mean, p95)

Ground truth for "same location" prefers matching location_id when set
(tighter), falling back to within LOCATION_MATCH_TOLERANCE_M of (x, y) for
older records ingested before location_id existed.

Run:
    python scripts/benchmark_embedders.py --site site_00 \\
        --embedders color_histogram clip_vit_b32 dinov2_base netvlad_resnet18 \\
        --out data/site_00/benchmarks/embedders_report.json

List available embedders:
    python scripts/benchmark_embedders.py --list
"""
from __future__ import annotations

import argparse
import json
import math
import time
from collections import defaultdict
from pathlib import Path

import cv2
import numpy as np

from fmc.config import load_site_config
from fmc.dataset.schema import ReferenceImage, load_records
from fmc.vpr.embedders.registry import available_embedders, get_benchmark_embedder

LOCATION_MATCH_TOLERANCE_M = 0.5
NETVLAD_CLUSTER_SAMPLE_SIZE = 40


def heading_diff(a: int, b: int) -> float:
    """Smallest angular difference between two 0-360 headings, in degrees."""
    d = abs(a - b) % 360
    return min(d, 360 - d)


def bucket_heading(diff: float) -> str:
    if diff <= 45:
        return "0-45"
    if diff <= 90:
        return "45-90"
    return "90-180"


def is_same_location(a: ReferenceImage, b: ReferenceImage) -> bool:
    if a.location_id and b.location_id:
        return a.location_id == b.location_id
    return math.hypot(a.x - b.x, a.y - b.y) <= LOCATION_MATCH_TOLERANCE_M


def load_images(site, records: list[ReferenceImage]) -> dict[str, np.ndarray]:
    images = {}
    for r in records:
        img_path = site.processed_dir / r.processed_path
        img = cv2.imread(str(img_path))
        if img is not None:
            images[r.image_id] = img
        else:
            print(f"WARNING: cannot read {img_path}, skipping {r.image_id}")
    return images


def evaluate_embedder(name: str, records: list[ReferenceImage], images: dict[str, np.ndarray], device: str = "cpu") -> dict:
    embedder = get_benchmark_embedder(name, device=device)

    # NetVLAD needs cluster centers fit on this site's own data before it can
    # embed anything -- do this ONCE on a fixed sample, not per query. This
    # sample overlapping the eval set is a mild, standard form of leakage for
    # an unsupervised clustering step -- reported here rather than hidden.
    if hasattr(embedder, "fit_clusters"):
        all_ids = list(images.keys())
        step = max(1, len(all_ids) // NETVLAD_CLUSTER_SAMPLE_SIZE)
        sample_ids = all_ids[::step][:NETVLAD_CLUSTER_SAMPLE_SIZE]
        embedder.fit_clusters([images[i] for i in sample_ids])

    ids = [r.image_id for r in records if r.image_id in images]
    records_by_id = {r.image_id: r for r in records}

    if hasattr(embedder, "score_matrix"):
        similarities, embed_latencies = embedder.score_matrix([images[image_id] for image_id in ids])
    else:
        embeddings = []
        embed_latencies = []
        for image_id in ids:
            t0 = time.perf_counter()
            emb = embedder.embed(images[image_id])
            embed_latencies.append((time.perf_counter() - t0) * 1000)
            embeddings.append(emb)
        embeddings = np.stack(embeddings).astype(np.float32)
        norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
        norms[norms == 0] = 1e-8
        normalized = embeddings / norms
        similarities = normalized @ normalized.T

    n = len(ids)
    ranks = []
    ap_scores = []
    genuine_scores = []
    impostor_scores = []
    heading_bucket_hits: dict[str, list[int]] = defaultdict(lambda: [0, 0])  # bucket -> [hits@1, total]

    for i, query_id in enumerate(ids):
        query_record = records_by_id[query_id]
        sims = similarities[i].copy()
        sims[i] = -np.inf  # exclude self

        relevant = np.zeros(n, dtype=bool)
        for j, cand_id in enumerate(ids):
            if j != i and is_same_location(query_record, records_by_id[cand_id]):
                relevant[j] = True

        if not relevant.any():
            continue  # no other photo of this location exists -- can't score retrieval for it

        order = np.argsort(-sims)
        ranked_relevant = relevant[order]
        rank_of_first_hit = int(np.argmax(ranked_relevant)) + 1 if ranked_relevant.any() else n
        ranks.append(rank_of_first_hit)

        hits, precisions = 0, []
        for rank, is_rel in enumerate(ranked_relevant, start=1):
            if is_rel:
                hits += 1
                precisions.append(hits / rank)
        ap_scores.append(float(np.mean(precisions)) if precisions else 0.0)

        genuine_scores.extend(sims[relevant].tolist())
        impostor_scores.extend(sims[~relevant & (np.arange(n) != i)].tolist())

        top1_record = records_by_id[ids[order[0]]]
        bucket = bucket_heading(heading_diff(query_record.orientation, top1_record.orientation))
        heading_bucket_hits[bucket][1] += 1
        if relevant[order[0]]:
            heading_bucket_hits[bucket][0] += 1

    ranks_arr = np.array(ranks)
    return {
        "embedder": name,
        "dim": embedder.dim,
        **({"fusion": "per-query z-score; mean of NetVLAD and DINOv2-VLAD scores"} if hasattr(embedder, "score_matrix") else {}),
        "num_queries_scored": int(len(ranks)),
        "recall_at_1": float(np.mean(ranks_arr <= 1)) if len(ranks_arr) else 0.0,
        "recall_at_5": float(np.mean(ranks_arr <= 5)) if len(ranks_arr) else 0.0,
        "mAP": float(np.mean(ap_scores)) if ap_scores else 0.0,
        "genuine_score_mean": float(np.mean(genuine_scores)) if genuine_scores else None,
        "genuine_score_std": float(np.std(genuine_scores)) if genuine_scores else None,
        "impostor_score_mean": float(np.mean(impostor_scores)) if impostor_scores else None,
        "impostor_score_std": float(np.std(impostor_scores)) if impostor_scores else None,
        "score_separation": (
            float(np.mean(genuine_scores) - np.mean(impostor_scores))
            if genuine_scores and impostor_scores
            else None
        ),
        "heading_robustness": {
            bucket: {"recall_at_1": (hits / total if total else None), "n": total}
            for bucket, (hits, total) in sorted(heading_bucket_hits.items())
        },
        "embed_latency_ms_mean": float(np.mean(embed_latencies)),
        "embed_latency_ms_p95": float(np.percentile(embed_latencies, 95)),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--site")
    parser.add_argument(
        "--embedders", nargs="+", default=None,
        help="Embedder names to benchmark (default: all registered)",
    )
    parser.add_argument("--out", default=None, help="Optional path to write the full JSON report")
    parser.add_argument(
        "--device",
        default="cpu",
        help="Torch device for loaded models (e.g. cpu, cuda, cuda:0)",
    )
    parser.add_argument("--list", action="store_true", help="List available embedder names and exit")
    args = parser.parse_args()

    if args.list:
        print("Available embedders:")
        for name in available_embedders():
            print(f"  {name}")
        return

    if not args.site:
        parser.error("--site is required (unless using --list)")

    site = load_site_config(args.site)
    records = load_records(site.dataset_jsonl_path)
    if len(records) < 3:
        print("Need at least 3 ingested images to benchmark retrieval meaningfully.")
        return

    images = load_images(site, records)
    embedder_names = args.embedders or available_embedders()

    reports = []
    for name in embedder_names:
        print(f"\n=== Benchmarking embedder: {name} ===")
        try:
            report = evaluate_embedder(name, records, images, device=args.device)
        except ImportError as e:
            print(f"  SKIPPED ({e})")
            continue
        reports.append(report)
        print(
            f"  dim={report['dim']}  Recall@1={report['recall_at_1']:.3f}  "
            f"Recall@5={report['recall_at_5']:.3f}  mAP={report['mAP']:.3f}  "
            f"score_separation={report['score_separation']}"
        )
        print(
            f"  embed latency: mean={report['embed_latency_ms_mean']:.1f}ms "
            f"p95={report['embed_latency_ms_p95']:.1f}ms"
        )
        print("  heading-bucketed Recall@1:")
        for bucket, stats in report["heading_robustness"].items():
            r = stats["recall_at_1"]
            print(f"    {bucket} deg (n={stats['n']}): {'n/a' if r is None else f'{r:.3f}'}")

    print("\n=== Summary (sorted by Recall@1) ===")
    for report in sorted(reports, key=lambda r: -r["recall_at_1"]):
        print(
            f"  {report['embedder']:24s} Recall@1={report['recall_at_1']:.3f} "
            f"Recall@5={report['recall_at_5']:.3f} mAP={report['mAP']:.3f} "
            f"latency={report['embed_latency_ms_mean']:.1f}ms"
        )

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w") as f:
            json.dump(reports, f, indent=2)
        print(f"\nWrote full report to {out_path}")


if __name__ == "__main__":
    main()
