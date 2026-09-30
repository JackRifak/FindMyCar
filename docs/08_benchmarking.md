# Deliverable 4/5 benchmarking: embedders and geometric verifiers

Two independent, pluggable benchmark harnesses, both run against a site's
own real ingested data rather than a public benchmark -- your genuine
confusion pairs (e.g. the P5/P6/P7 area in site_00) are more diagnostic for
this project than any generic VPR dataset.

## Stage 1 -- embedding models (`scripts/benchmark_embedders.py`)

Leave-one-out retrieval evaluation. For each embedder: embeds every ingested
photo once, then for each photo (as query) ranks every other photo by
cosine similarity and checks whether photos of the same capture location
come back on top.

```
python scripts/benchmark_embedders.py --list
python scripts/benchmark_embedders.py --site site_00 \
    --embedders color_histogram clip_vit_b32 dinov2_base netvlad_resnet18 \
    --out data/site_00/benchmarks/embedders_report.json
```

Reports Recall@1/@5, mAP, genuine/impostor score separation, heading-
bucketed Recall@1 (viewpoint robustness), and embedding latency, per
embedder. Registered embedders: `color_histogram` (baseline),
`clip_vit_b32`, `clip_vit_l14`, `dinov2_small`, `dinov2_base`,
`netvlad_resnet18`, `fusion_netvlad_dinov2_base`, and
`late_fusion_zscore_netvlad_dinov2_base`. The feature-level fusion embedder
concatenates separately L2-normalized NetVLAD and DINOv2-VLAD vectors with
equal total weight. The late-fusion benchmark instead computes each model's
cosine scores independently, z-scores each query's candidate scores, then
averages the two score sets. `dinov2_*` extracts DINOv2 patch descriptors and
aggregates them with site-fitted, hard-assignment VLAD, following the
AnyLoc-style DINOv2 + VLAD approach (not CLS pooling or GeM). See
`src/fmc/vpr/embedders/` for implementations and
`src/fmc/vpr/embedders/netvlad_embedder.py` for an important caveat: this
NetVLAD is cluster-fit on the site's own data, not the original paper's
pretrained weights.

## Stage 2 -- geometric verifiers (`scripts/benchmark_verifiers.py`)

Builds true-match / hard-negative pairs from the site's CURRENTLY indexed
embeddings (leave-one-out retrieval, top-K candidates per query, every
candidate labeled), then scores each verifier's ability to accept true
matches and reject confusions.

```
python scripts/benchmark_verifiers.py --list
python scripts/benchmark_verifiers.py --site site_00 \
    --verifiers orb_ransac superpoint_lightglue superpoint_superglue loftr \
    --out data/site_00/benchmarks/verifiers_report.json
```

Reports true/false-positive rate at each verifier's own threshold, a
21-point inlier-ratio threshold sweep (to find the best cutoff for this
site rather than trusting a default), accuracy split by texture richness
(low/medium/high, via ORB keypoint count as a proxy -- the split most
relevant to blank walls / repetitive garage flooring), and per-pair
latency. Registered verifiers: `orb_ransac` (current production baseline),
`superpoint_lightglue`, `superpoint_superglue` (original SuperGlue; requires
the official repository cloned to `third_party/SuperGluePretrainedNetwork`),
`loftr`. To enable SuperGlue, from the project root run:

```
git clone https://github.com/magicleap/SuperGluePretrainedNetwork third_party/SuperGluePretrainedNetwork
```

Then include `superpoint_superglue` in `--verifiers`. If the clone or Torch
dependency is missing, the benchmark reports it as skipped and continues.

## Installing dependencies

Nothing above is required by the mock-site baseline. Install only what
you're benchmarking -- each embedder/verifier raises `ImportError` with the
exact `pip install` command if its deps are missing, and both scripts catch
that and skip it rather than aborting the whole run. See the bottom of
`requirements.txt` for the full optional list.

## Reading the results together

Per-stage numbers are useful for picking a winner in isolation, but the
number that actually matters is end-to-end: once you've picked a leading
embedder + verifier pair, wire them into `fmc/vpr/embedder.get_embedder()`
and `fmc/vpr/geometric_verification.verify()` and re-run
`scripts/evaluate_vpr_accuracy.py` for real localisation accuracy in
meters and end-to-end latency -- that's what determines whether the change
actually fixes the field-test failures (P6's zero-match, the ~48% no-match
rate) documented in the project's live-capture test notes.

To check whether the geometric verifier's default inlier-ratio threshold is
appropriate for a particular retrieval embedder, run the end-to-end threshold
sweep. It reuses each query's ordered top-K candidates and their ORB/RANSAC
evidence while reporting correct, false-match, and no-match counts per cutoff:

```
python scripts/evaluate_vpr_accuracy.py --site site_00 --embedder dinov2_base \
    --device cuda --threshold-sweep --ransac-seed 0 \
    --out data/site_00/benchmarks/e2e_dinov2_threshold_sweep.json
```

This sweep is a same-site exploratory retuning, not a held-out accuracy
estimate. Keep a separate capture session for selecting thresholds before
using them in production.

The primary end-to-end acceptance threshold is `--inlier-ratio-threshold`
(default `0.40`). The evaluator also accepts `--orb-ratio-test` (default `0.75`),
`--min-match-count` (default `15`), and `--min-inlier-spread-fraction`
(default `0`, disabled). The spread fraction is the smallest normalized
inlier bounding-box span across x/y in both images. These options are for
evaluation only; they do not change the production verifier defaults.
For example, compare a stricter Lowe test and match-count floor with:

```
python scripts/evaluate_vpr_accuracy.py --site site_00 --embedder production \
    --inlier-ratio-threshold 0.50 --min-match-count 15 --threshold-sweep \
    --ransac-seed 0 --out data/site_00/benchmarks/e2e_orb_strict.json
```

To inspect the actual ORB correspondences for selected queries, add
`--visualize-dir` and optionally restrict export with `--visualize-ids`:

```
python scripts/evaluate_vpr_accuracy.py --site site_00 --embedder production \
    --visualize-dir data/site_00/benchmarks/orb_match_visuals \
    --visualize-ids F01_ZB_00008_202 F01_ZB_00028_284
```

Each image shows the query beside its selected match (or top-ranked rejected
candidate when no candidate passes), with ORB correspondences colored green
for RANSAC inliers and red for outliers. The header includes retrieval rank,
score, match count, inlier ratio, and localization outcome.
