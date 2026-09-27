# Find My Car — Visual Indoor Parking Navigation

See `PROJECT_CONTEXT.md` for the condensed project brief and `docs/` for the
Phase 1 planning deliverables. This README covers the code scaffold.

**Have real survey data (floor plan + location photo sweeps)?** Skip straight
to `docs/04_survey_ingestion_workflow.md`.

## Status
Phase 1 (site survey spec, coordinate system) + a runnable **mock-site**
end-to-end pipeline scaffold (dataset → embedding → VPR → geometric
verification → sensor fusion → map matching → position API). No real
facility has been surveyed yet — see "What's real vs. placeholder" below.

## Setup
```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

## Try the mock pipeline end-to-end
```bash
python scripts/generate_mock_images.py     # synthesize placeholder landmark images
python scripts/register_mock_dataset.py    # register them into dataset.jsonl
python -m fmc.dataset.build_index --site mock_site   # build embeddings.npz
python scripts/query_position.py data/mock_site/raw/L1_000.jpg --site mock_site   # localize a query image
pytest tests/                                        # all tests (pipeline, routing, georeferencing)
```

For a real site instead of the mock one, same last step:
```bash
python scripts/query_position.py /path/to/any/photo.jpg --site site_00
```

**Measure real accuracy** (Top-1 correctness, false-match/confusion pairs,
inlier-ratio distribution) with a leave-one-out evaluation over everything
already ingested for a site — no separate held-out capture needed:
```bash
python scripts/evaluate_vpr_accuracy.py --site site_00
```

Run the position + route API (FMC_SITE_ID is required, no default):
```bash
FMC_SITE_ID=site_00 uvicorn fmc.api.server:app --reload
# POST /localize?device_id=abc         with an image file -> position JSON
# GET  /position/abc                   -> last known position
# GET  /route/abc?slot_id=A014          -> waypoints from last position to that slot
# POST /admin/reload                    -> pick up re-ingested data / rebuilt index / edited config.yaml without restarting
```
This same server also hosts a **live capture test page** at `/` (open it in
a browser) — a hybrid VPR + client-side PDR tracker (VPR corrects drift and
calibrates heading, PDR carries continuous position between fixes) — see
`docs/06_live_capture_test.md` and `docs/07_pdr.md` (note: needs HTTPS to
access camera/motion sensors from a phone, documented there).

**Interactive web editor** for floor-plan georeferencing, walkable paths,
and parking slots (browser alternative to the CLI point-picker) — see
`docs/05_web_editor.md`:
```bash
uvicorn fmc.webtools.server:app --reload --port 8001
```

## Project layout
```
docs/                   Phase 1 planning deliverables (survey spec, coord system, dataset spec, real-data ingestion workflow, web editor, live capture test, hybrid VPR+PDR tracking)
data/mock_site/         Synthetic site config + generated mock dataset (throwaway)
data/<real_site>/       Real site data goes here once surveyed/ingested (same structure, plus floorplan/)
src/fmc/
  config.py             Paths, coordinate system constants, site config loader
  georeference.py        Floor-plan pixel -> real-world-meters affine transform fitting
  pdf_render.py          PDF-to-image rendering (shared by CLI script and web editor)
  dataset/              Metadata schema, capture registration, index building
  vpr/                  Embedder, vector search, geometric verification, VPR pipeline
  vio/                  VIO tracker interface (stub — see below)
  fusion/               Sensor fusion (VPR+VIO) and map matching
  navigation/           Walkable-path graph + Dijkstra routing (position -> vehicle slot)
  api/                  Position + route API (FastAPI) — the contract exposed to the rest of the app, plus a live capture test page at "/" (docs/06_live_capture_test.md)
  webtools/             Interactive browser editor for georeferencing/geometry (see docs/05_web_editor.md)
scripts/                Mock data generation + real survey ingestion (PDF render, point picking, transform fitting, photo ingestion)
tests/                  Automated tests over the mock pipeline, routing, georeferencing, and the web editor backend
```

## What's real vs. placeholder (read this before trusting any numbers)
- **Embedding model** (`fmc/vpr/embedder.py`): a cheap color-histogram baseline.
  Exists only to make the pipeline runnable. Deliverable 4 will benchmark
  real candidates (CLIP/DINOv2/NetVLAD/etc.) and replace it.
- **Geometric verification** (`fmc/vpr/geometric_verification.py`): ORB +
  RANSAC homography baseline. Deliverable 5 will benchmark against
  SuperPoint+SuperGlue/LightGlue/LoFTR.
- **VIO tracker**: the Python `VIOTracker` interface (`fmc/vio/tracker.py`,
  `DeadReckoningStub`) was the original placeholder, but the real tracking
  logic now lives client-side: a hybrid VPR + Pedestrian Dead Reckoning
  (PDR) tracker in `fmc/api/static/app.js`, documented in `docs/07_pdr.md`.
  Field-tested but not yet fully validated — see that doc's "known
  caveats" and `docs/06_live_capture_test.md`'s test history for current
  open questions (survey coverage density, heading calibration noise).
- **Mock site** (`data/mock_site/`): entirely synthetic coordinates/images,
  standing in for a real facility until the actual survey (`docs/02_site_survey_spec.md`)
  happens. Discard mock data once real survey data exists — don't let any
  mock-derived tuning carry over.
- **Sensor fusion** (`fmc/fusion/sensor_fusion.py`): hard-reset-on-VPR-fix,
  not a proper filter. Fine as a first version; revisit with a real
  EKF/particle filter once real VIO drift characteristics are known.

## Next steps (per docs/ development order)
1. Real site survey (physical facility) → replace `data/mock_site/` with `data/<real_site>/`.
2. Deliverable 4: benchmark embedding models against the real dataset once it exists.
3. Continue field-testing the hybrid VPR+PDR tracker (`docs/07_pdr.md`) —
   densify survey coverage, calibrate PDR's step-length constant against a
   real walk, and watch for the heading-calibration-noise pattern flagged
   in that doc.
4. Replace hard-reset fusion with a proper filter once real drift
   characteristics are well understood from field testing.
5. Navigation routing — basic Dijkstra over the walkable graph is implemented
   (`fmc/navigation/`, exposed via `/route/{device_id}`); still missing:
   multi-floor transitions (stairs/ramps/elevators) and turn-by-turn
   instruction generation from the raw waypoint list.
6. AR layer — explicitly last, per the brief.
