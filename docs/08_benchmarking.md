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
embedder. Registered embedders: `color_histogram` (current production
baseline), `clip_vit_b32`, `clip_vit_l14`, `dinov2_small`, `dinov2_base`,
`netvlad_resnet18`. `dinov2_*` uses DINOv2 patch-token GeM pooling
(rather than the weaker CLS-token embedding), which is the fairer
AnyLoc-style dense-feature baseline for this benchmark. See
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
    --verifiers orb_ransac superpoint_lightglue loftr \
    --out data/site_00/benchmarks/verifiers_report.json
```

Reports true/false-positive rate at each verifier's own threshold, a
21-point inlier-ratio threshold sweep (to find the best cutoff for this
site rather than trusting a default), accuracy split by texture richness
(low/medium/high, via ORB keypoint count as a proxy -- the split most
relevant to blank walls / repetitive garage flooring), and per-pair
latency. Registered verifiers: `orb_ransac` (current production baseline),
`superpoint_lightglue`, `superpoint_superglue` (needs a separate git clone,
see `src/fmc/vpr/verifiers/superglue_verifier.py`), `loftr`.

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
