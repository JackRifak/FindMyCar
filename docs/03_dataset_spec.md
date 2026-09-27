# Visual Dataset Specification

## Image format
- Storage format: JPEG, quality ~90 (balance of fidelity vs. storage; raw video frames get re-encoded on extraction).
- Resolution: capture at the device's native resolution; store a normalized working copy at **1280x720** (or 720 on the short side, aspect preserved) — enough detail for feature matching without bloating the index.

## Directory / storage structure
```
data/<site_id>/
  raw/            # untouched captures (video or stills) per capture session
  processed/
    F<floor>/Z<zone>/<image_id>.jpg   # normalized reference images
  index/
    dataset.jsonl        # one JSON record per reference image (metadata)
    embeddings.npz        # embedding matrix + id list, built from dataset.jsonl
```

## Metadata record (one JSON object per line in `dataset.jsonl`)
Based on the brief's Section 5 schema, plus a `location_id` field added
later to explicitly link each photo to the capture location it belongs to
(needed for the web editor's per-location photo management, see
`05_web_editor.md`):
```json
{
  "image_id": "F01_ZC_00245_090",
  "floor": 1,
  "zone": "Zone_C",
  "x": 42.5,
  "y": 67.2,
  "orientation": 90,
  "timestamp": "2026-09-07T10:15:00Z",
  "camera_information": {"device": "string", "focal_length_mm": null, "resolution": "1280x720"},
  "processed_path": "F1/ZC/F01_ZC_00245_090.jpg",
  "location_id": "loc_05",
  "embedding": "<computed at index-build time, not stored in raw record>",
  "features": "<local feature descriptors, computed on demand for geometric verification, not stored in dataset.jsonl>"
}
```
`location_id` defaults to `""` for records written before this field
existed (older CLI-ingested datasets) — those remain fully usable, just not
directly queryable by location_id (fall back to matching floor/zone/x/y).

Design choice: `dataset.jsonl` stores **capture metadata only** (coordinates, orientation, timestamp, camera info, path to processed image). Embeddings are computed in a separate build step and stored in `embeddings.npz` (id-aligned array) so the embedding model can be swapped (Deliverable 4 benchmarking) by re-running the build step, without touching ground-truth metadata. Local feature descriptors (ORB/SIFT/etc.) are recomputed on demand during geometric verification rather than stored, since they're cheap to compute and storing them for every reference image is unnecessary disk overhead at this stage.

## Naming convention
See `02_site_survey_spec.md` — `F{floor:02d}_Z{zone}_{sequence:05d}_{orientation_deg:03d}`.

## Coordinate format
Floats in meters, per `01_coordinate_system.md`. `orientation` in integer degrees, 0-360, clockwise from +Y.

## Versioning
`dataset.jsonl` was originally append-only during survey; the web editor's
capture-location/photo management (heading edits, photo deletion) now also
performs full-file read-modify-write updates when needed (see
`fmc.dataset.schema.save_records`) — safe at the dataset sizes involved
here (tens to low hundreds of images). A `dataset_version` field
(git-style short hash of the file's content, computed at build time) gets
baked into `embeddings.npz` so a served position result can be traced back
to exactly which dataset snapshot produced it — useful once the dataset is
re-surveyed or corrected over time.
