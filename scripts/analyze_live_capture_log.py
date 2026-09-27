"""Analyze a live-capture-test session log (CSV exported from the live
capture test page, fmc/api/static/) for quick signal on the current
"server-side VIO via periodic VPR" / hybrid VPR+PDR trial
(docs/06_live_capture_test.md).

Handles two CSV shapes:
  - Original (VPR-only client): one row per capture attempt.
  - Hybrid (VPR+PDR client): rows are interleaved per-step PDR updates and
    occasional VPR fixes/misses, distinguished by a `source` column
    (VPR / VPR_MISS / PDR). PDR rows always show tracking=true (since PDR
    reports a carried position, not a match), so match-rate/latency/
    confidence stats are computed from VPR/VPR_MISS rows ONLY when a
    `source` column is present -- counting PDR rows as capture attempts
    would badly inflate the apparent match rate.

Run:
    python scripts/analyze_live_capture_log.py live_capture_XXXX.csv
"""
from __future__ import annotations

import argparse
import csv
from collections import Counter
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("csv_path", type=Path)
    args = parser.parse_args()

    with open(args.csv_path, "r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))

    total = len(rows)
    if total == 0:
        print("Empty log.")
        return

    has_source = "source" in rows[0]
    if has_source:
        vpr_rows = [r for r in rows if r["source"] in ("VPR", "VPR_MISS")]
        pdr_rows = [r for r in rows if r["source"] == "PDR"]
        print(f"Hybrid log detected: {len(vpr_rows)} VPR capture events, {len(pdr_rows)} PDR step updates")
    else:
        vpr_rows = rows
        pdr_rows = []

    if not vpr_rows:
        print("No VPR capture events found to analyze.")
        return

    matched_rows = [r for r in vpr_rows if r["tracking"].strip().upper() == "TRUE"]
    match_rate = len(matched_rows) / len(vpr_rows)

    latencies = [int(r["latency_ms"]) for r in vpr_rows]
    avg_latency = sum(latencies) / len(latencies)
    max_latency = max(latencies)

    # Round positions to spot repeated/quantized matches -- a proxy for how
    # many distinct real-world spots were actually recognized, versus
    # matches all collapsing onto a small handful of known survey locations.
    distinct_positions = Counter(
        (round(float(r["x"]), 1), round(float(r["y"]), 1)) for r in matched_rows
    )

    confidences = [float(r["confidence"]) for r in matched_rows]
    avg_confidence = sum(confidences) / len(confidences) if confidences else 0.0

    print(f"\nTotal VPR capture attempts: {len(vpr_rows)}")
    print(f"Matched: {len(matched_rows)} ({match_rate * 100:.1f}%)")
    print(f"Latency: avg={avg_latency:.0f}ms  max={max_latency}ms")
    print(f"Avg confidence (matched only): {avg_confidence:.3f}")
    print(f"Distinct positions seen: {len(distinct_positions)}")
    if distinct_positions:
        print("Most frequent positions:")
        for (x, y), count in distinct_positions.most_common(10):
            print(f"  ({x}, {y}): {count} times")

    # PDR-specific analysis -- only meaningful for hybrid-format logs.
    if pdr_rows:
        step_counts = [int(r["steps_since_fix"]) for r in pdr_rows if r.get("steps_since_fix")]
        max_steps_between_fixes = max(step_counts) if step_counts else 0
        drifts = [float(r["drift_m"]) for r in matched_rows if r.get("drift_m")]
        print(f"\nPDR: {len(pdr_rows)} step updates logged")
        print(f"Longest gap carried by PDR alone: {max_steps_between_fixes} steps")
        if drifts:
            print(
                f"Drift at VPR correction (how far PDR had wandered since the last "
                f"fix): avg={sum(drifts) / len(drifts):.2f}m  max={max(drifts):.2f}m"
            )
            print(
                "This is the key number for judging PDR quality: small, consistent "
                "drift means PDR is tracking real movement well between fixes; large "
                "or wildly varying drift means PDR's step/heading estimate isn't "
                "trustworthy enough to carry the gaps on its own."
            )

    print()
    if match_rate < 0.7:
        print(
            "WARNING: match rate below 70%. Before concluding anything about the\n"
            "tracking approach itself, check whether this reflects sparse survey\n"
            "coverage (few ingested locations relative to the area walked) rather\n"
            "than a fundamental limitation -- compare against how many locations\n"
            "are actually ingested for this site (fmc.dataset.build_index count)."
        )
    if len(distinct_positions) <= 5 and len(matched_rows) > 10:
        print(
            "WARNING: very few distinct positions relative to matched frames --\n"
            "this usually means matches are 'snapping' to a small handful of known\n"
            "survey locations rather than tracking smooth real movement. Again\n"
            "points at coverage density, not necessarily the approach itself."
        )
    if avg_latency > 1500:
        print("WARNING: average latency is high relative to the brief's ~2-3s target.")
    if match_rate >= 0.7 and len(distinct_positions) > 5 and avg_latency <= 1500:
        print("No red flags on these three checks -- worth a closer look at whether")
        print("the position trail actually matches the path you walked.")


if __name__ == "__main__":
    main()
