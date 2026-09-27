# Real Survey Ingestion Workflow

For a site where you have (a) a PDF floor drawing with marked dimensions and
(b) location-grouped photo sweeps — one folder per capture spot, each with
~10 overlapping photos taken while rotating in place, spots roughly 5m apart.

This turns that raw material into the dataset format defined in
`03_dataset_spec.md`, ready for `fmc.dataset.build_index`.

## 0. Prerequisites
```bash
pip install -e ".[dev]"
```
The first script you run against a new `<site_id>` auto-creates a minimal
`data/<site_id>/config.yaml` (just a placeholder `floor: 1` entry) if one
doesn't exist yet, so you don't need to hand-write it before ingesting.
If your site has more than one floor, add an entry per floor to that file
afterward (see `data/mock_site/config.yaml` for the shape) — the `floor`
values there just need to match the floor numbers you use in `locations.csv`.

## 1. Render the floor plan to an image
```bash
python scripts/pdf_to_floorplan_image.py /path/to/floorplan.pdf --site <site_id> --page 0 --dpi 200
```
Produces `data/<site_id>/floorplan/floorplan.png`. Use `--page` if the PDF
has multiple floors (one page per floor) — repeat this whole workflow once
per floor.

## 2. Pick control points for georeferencing
```bash
python scripts/pick_floorplan_points.py data/<site_id>/floorplan/floorplan.png \
    --out data/<site_id>/floorplan/control_points.csv
```
Click **at least 3 non-collinear points** on features you can tie to a real
measurement from the drawing's marked dimensions — e.g. the two ends of a
dimensioned corridor, plus a third point off that line (a corner, a column).
The window prints each point's pixel coordinates as you click.

Then **manually edit** `control_points.csv` to add `real_x` and `real_y`
columns: assign one point as your coordinate-system origin `(0, 0)` (per
`01_coordinate_system.md` — usually a corner near the main entrance) and
derive the others from the marked dimensions relative to it.

**`real_x`/`real_y` must be in METERS.** Architectural/CAD-exported PDFs
commonly mark dimensions in millimeters — if yours does, divide by 1000
before entering values here. Getting this wrong doesn't cause an error
anywhere upstream (the fit still "succeeds" and residuals still look fine,
just in the wrong unit) — it only shows up later as absurd position values.
If you've already ingested data with the wrong unit, `scripts/fix_coordinate_units.py`
fixes it after the fact without re-surveying. Example (already in meters):
```csv
index,pixel_x,pixel_y,real_x,real_y
0,120.4,88.1,0.0,0.0
1,940.2,90.5,25.0,0.0
2,118.9,610.7,0.0,15.0
```

## 3. Fit and sanity-check the transform
```bash
python scripts/fit_floorplan_transform.py data/<site_id>/floorplan/control_points.csv \
    --out data/<site_id>/floorplan/transform.json
```
Check the printed **residuals** — each should be small (well under half a
meter). A large residual usually means a mis-clicked point or a mismatched
real_x/real_y value; fix `control_points.csv` and re-run before continuing.
Add more control points if residuals are inconsistently large across the
drawing (a single mis-measured dimension won't show up as an outlier with
only 3 points — more points make the least-squares fit self-checking).

## 4. Pick each capture location on the floor plan
```bash
python scripts/pick_floorplan_points.py data/<site_id>/floorplan/floorplan.png \
    --out data/<site_id>/floorplan/locations_pixels.csv
```
Click one point per capture location, roughly where you stood. Then
**manually edit** the CSV to add `location_id` (must exactly match your
photo folder names), `floor`, and `zone` columns:
```csv
index,pixel_x,pixel_y,location_id,floor,zone
0,205.0,140.2,loc_01,1,Zone_A
1,205.0,260.0,loc_02,1,Zone_A
```

## 5. Compute real-world coordinates for each location
```bash
python scripts/compute_location_coords.py \
    data/<site_id>/floorplan/transform.json \
    data/<site_id>/floorplan/locations_pixels.csv \
    --out data/<site_id>/index/locations.csv
```
Produces the `locations.csv` manifest (`location_id, floor, zone, x, y`)
that the ingestion step needs.

## 6. Check for existing compass headings, then ingest the photo sweeps
Before doing any manual work, check whether your photos already carry a
compass heading in EXIF (common if location services were on during capture):
```bash
python scripts/check_exif_compass.py /path/to/survey_photos
```
If it reports full coverage, skip straight to the ingest command below —
no headings CSV needed, EXIF is used automatically.

If you have compass readings from another source (field notes, a written
log) instead, or to fill in gaps EXIF didn't cover, you need a headings CSV
with columns `location_id, filename, heading_degrees`. Two ways to build it:

**If you wrote headings down in capture order** (most likely, if you noted
each direction while rotating through the sweep) — use the helper so you
don't have to manually match filenames yourself:
```csv
location_id,headings
loc_01,0;36;75;110;158;200;240;280;320;350
loc_02,10;50;95;140;185;230;275;320
```
```bash
python scripts/build_headings_csv.py ordered_headings.csv /path/to/survey_photos --out data/<site_id>/floorplan/headings.csv
```
It matches each heading to a photo using the same capture-order logic
ingestion itself uses, and **prints the pairing it made** — check that
against your notes before trusting it; a miscounted photo or heading
throws every subsequent pairing in that location off by one.

**If you already have specific filename-to-heading pairs**, write the CSV
directly instead:
```csv
location_id,filename,heading_degrees
loc_01,IMG_0001.jpg,0
loc_01,IMG_0002.jpg,37
```
`filename` must match exactly. Either way, you don't need an entry for
every photo — anything missing falls back to EXIF, then to an evenly-spaced
approximation.

Then ingest:
```bash
python scripts/ingest_survey_locations.py /path/to/survey_photos data/<site_id>/index/locations.csv --site <site_id> [--headings headings.csv]
```
`/path/to/survey_photos` must contain one subfolder per `location_id`, each
holding that location's overlapping rotation-sweep photos. Heading is
resolved per photo in priority order: your `--headings` CSV, then EXIF
`GPSImgDirection`, then an evenly-spaced approximation across the sweep —
see the docstring in `scripts/ingest_survey_locations.py` for details. The
script prints a summary of how many photos used each source, so you can see
at a glance how much of your dataset has a real measured heading.

## 7. Build the index and test it
```bash
python -m fmc.dataset.build_index --site <site_id>
```
Then query it exactly like the mock-site walkthrough in the main README,
substituting `--site <site_id>` and a real photo as the query image.

## 8. If coordinates come out in the wrong unit
If positions from `query_position.py` (or anywhere else) look like they're
in millimeters instead of meters (four/five-digit values instead of
tens/hundreds), your control points were likely entered in mm. Fix it
without re-surveying or re-embedding:
```bash
python scripts/fix_coordinate_units.py --site <site_id> --factor 0.001
```
This rescales `dataset.jsonl`, `locations.csv`, and the saved
`transform.json` (so future locations you add to this site come out
correct too) — it does not touch `embeddings.npz`, since VPR re-reads
`dataset.jsonl` fresh on every query and doesn't need re-embedding.

## 9. Add real walkable paths and vehicle slots (for routing)
The VPR pipeline (steps 1-7) doesn't need this, but routing
(`fmc/navigation`) and map matching (`fmc/fusion/map_matching.py`) do —
without it, `data/<site_id>/config.yaml` only has the empty placeholder
from step 0.

**Walkable segments** — click pairs of points along every pedestrian
corridor/aisle centerline, in continuous pairs so segments share corners
(click one segment's end near where the next one starts). Endpoints within
0.3m of each other are automatically treated as the same junction (see
`NODE_SNAP_TOLERANCE_M` in `fmc/navigation/graph.py`), so ordinary
clicking imprecision won't silently break the graph — but a real gap
larger than that (e.g. clicking two genuinely separate corridor segments
without connecting them) won't route between them:
```bash
python scripts/pick_floorplan_points.py data/<site_id>/floorplan/floorplan.png \
    --out data/<site_id>/floorplan/segments_pixels.csv --mode segments
```

**Vehicle slots** — click one point per slot you want routable to, then
manually add `slot_id` and `zone` columns:
```bash
python scripts/pick_floorplan_points.py data/<site_id>/floorplan/floorplan.png \
    --out data/<site_id>/floorplan/slots_pixels.csv
```
```csv
index,pixel_x,pixel_y,slot_id,zone
0,340.2,410.5,A014,Zone_A
1,690.0,205.0,B027,Zone_B
```

Then convert both into real-world coordinates and write them into
`config.yaml`:
```bash
python scripts/build_site_geometry.py --site <site_id> --floor <floor> \
    --segments data/<site_id>/floorplan/segments_pixels.csv \
    --slots data/<site_id>/floorplan/slots_pixels.csv
```
Either `--segments` or `--slots` can be omitted if you're only updating
one. **Re-running replaces that floor's segments/slots entirely** — it's
not additive, so re-click everything for that floor each time, or hand-edit
`config.yaml` for small tweaks. Repeat per floor for multi-floor sites.

## Notes / limitations of this first pass
- **One floor at a time.** Multi-floor sites repeat steps 1-6 per floor
  (each floor's PDF page gets its own `floorplan.png`/`transform.json`, but
  all floors share the same `dataset.jsonl`/`locations.csv` distinguished by
  the `floor` column).
- **Heading is resolved per photo** from your `--headings` CSV, then EXIF,
  then an evenly-spaced approximation as a last resort — see step 6 above.
- **Zone assignment is manual** (you decide which zone each location falls
  in when editing `locations_pixels.csv`) — there's no automatic zone
  detection from the floor plan yet.
- **Walkable segments need to form a connected graph** for routing to work
  across the whole floor. Endpoints within 0.3m automatically merge into
  one junction (absorbing normal clicking imprecision), but genuinely
  disconnected corridor clusters still won't route between each other —
  `fmc/navigation` will route within each cluster but return no route
  across them.
- **Cross-floor routing (stairs/ramps/elevators) isn't implemented** —
  `/route/{device_id}` in the API currently rejects a slot on a different
  floor than the current position.
