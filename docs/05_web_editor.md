# Interactive Web Editor

A browser tool for the georeferencing/geometry/survey workflow in
`04_survey_ingestion_workflow.md`, as an alternative (or complement) to the
CLI scripts + matplotlib point-picker.

## Run it
```bash
uvicorn fmc.webtools.server:app --reload --port 8001
```
Open `http://127.0.0.1:8001/`. Runs on a different port from the position
API (`fmc.api.server`, port 8000 by default) so both can run at once.

## Integration with the existing system
This is **not** a separate tool with its own data format — it's a thin web
layer over the same `fmc.georeference`, `fmc.config`, `fmc.dataset`,
`fmc.navigation`, and `fmc.vpr` modules the CLI scripts already use.
Concretely:

- Reads/writes `control_points.csv` and `transform.json` in the exact
  format `fit_floorplan_transform.py` uses
- Reads/writes `config.yaml`'s `walkable_segments`/`vehicle_slots` the same
  way `build_site_geometry.py` does
- Reads/writes `locations.csv` (via the shared `fmc.dataset.locations`
  module) the same way `compute_location_coords.py` does
- Reads/writes `dataset.jsonl` (via `fmc.dataset.schema`, `fmc.dataset.capture`)
  the same way `ingest_survey_locations.py` does, including the same
  `location_id` field
- "Test" calls the same `VPRPipeline` the position API and
  `query_position.py` use, and the same `calculate_route` +
  `snap_to_walkable` the position API's `/route` endpoint uses

You can start georeferencing/surveying a site with the CLI, finish it in
the browser, edit it again with the CLI later, or any combination — every
file is interchangeable either way.

## What it does (steps in the mode bar)

**Overview** — read-only view of everything saved so far: ingested capture
locations, walkable paths, vehicle slots.

**1. Calibrate** — upload a floor plan PDF (choose page + DPI, rendered via
the same `fmc.pdf_render` module the CLI uses), click control points, enter
real-world (meters) coordinates, and get a **live fit preview** before
saving anything: residuals, implied scale (m/pixel), and explicit warnings
— including the exact mm-vs-meters mistake this project hit twice, which
residuals alone can't catch with only 3 points (see `fmc/georeference.py`).

**2. Paths** — click pairs of points along corridors to define walkable
segments; saved segments auto-merge endpoints within 0.3m (same tolerance
as CLI-built geometry).

**3. Slots** — click a location, assign `slot_id` + `zone` for each parking
slot.

**4. Locations** — click an existing marker to select a capture location,
or click empty space to create a new one (prompts for an ID and zone).
Selecting a location shows its reference photos as a grid: each photo has
a heading input (0-360°) with a "Save"-on-blur pattern and an explicit
**unsaved** badge while a typed value hasn't been committed yet, plus a
delete button. The last three digits of each photo ID and processed
filename track its saved heading; editing the heading updates both.
"+ Add photo(s)" accepts multiple JPEG/PNG files at once
(non-image files are rejected client- and server-side); newly added photos
default to heading 0 and can be adjusted afterward. The selected location's
photos are drawn as small directional arrows on the floor plan, computed
through the site's actual pixel↔world transform (not naive on-screen
angles, since the transform can rotate/reflect between pixel and world
axes) — see `fmc/webtools/server.py`'s `_heading_direction_px`. A "Delete
location" button removes the location and all its photos (files +
metadata) after a confirmation prompt. This step is what previously
required leaving the browser entirely for `pick_floorplan_points.py` +
`compute_location_coords.py` + manually organizing photo folders +
`ingest_survey_locations.py` — now it's all here, and both paths remain
fully interchangeable (a location created via CLI shows up in the browser
and vice versa). Use **Build embeddings** to rebuild the site's VPR index
from all ingested photos after adding or removing reference images; the
button reports the indexed image count and embedding dimension when done.

**5. Test** — upload a real survey photo, see where VPR thinks it is
(magenta star) directly on the map, and preview a route to a chosen slot.

## Roadmap — what else this needs
Roughly in priority order, not all built yet:

- **Drag-to-move** a placed point instead of delete-and-reclick to correct it
- **Draft state surviving a reload** — currently, control points/segments/
  slots not yet saved are lost if you navigate away; capture-location
  photos are the exception (each upload/heading-edit persists immediately,
  since that flow is inherently multi-step)
- **Accuracy-evaluation map overlay** — visualize `evaluate_vpr_accuracy.py`
  results (confusion pairs, correct/wrong/no-match per location) spatially
  instead of as a text report
- **Config versioning/backup** — `PUT /floors/{floor}/segments` and
  `/slots` still replace a whole floor's geometry outright; a confirmation
  step or automatic backup before overwrite would prevent an accidental
  full wipe
- **Site export/import** — bundle a site (floor plan + config + control
  points + locations) for sharing with a teammate without filesystem/SSH access
- **Cross-floor geometry copy** — multi-floor sites currently redo control
  points + geometry from scratch per floor
- **Large-image performance** — fine for a single-floor plan at a few
  hundred DPI; a very large or very high-resolution multi-megapixel plan
  would benefit from image tiling
- **Multi-editor coordination** — this tool, the CLI scripts, and any other
  session editing files directly are all last-write-wins with no
  coordination; worth a "who's editing this right now" indicator if this
  is ever used by more than one person on the same site concurrently
- **Touch/tablet support** — for verifying geometry on-site while
  physically walking the facility, rather than only at a desk
