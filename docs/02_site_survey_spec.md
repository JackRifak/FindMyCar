# Site Survey & Data Collection Specification

## Goal
Produce a dense, well-distributed set of reference images covering all pedestrian-navigable areas of the facility, tagged with accurate coordinates, sufficient for VPR to distinguish visually similar locations (a major risk in parking garages, where many bays/columns look alike).

## Capture spacing
- **Along corridors:** one capture position every **2-3 meters**, alternating so consecutive positions overlap enough for geometric verification to find shared features (~60-70% visual overlap between adjacent captures).
- **At decision points** (intersections, ramps, stairs, elevator lobbies, zone boundaries): capture from every approach direction, not just one — a user can arrive from any direction.
- **Along a single row of parking bays:** every 3-4 meters is usually enough since bay markings/columns repeat; rely on zone-level signage more than bay-level texture here.

## Camera orientations per capture position
At each capture position, take **4 orientations** (roughly 90° apart: facing each direction a pedestrian could be walking) rather than a single forward shot. This matches the reality that VPR must work regardless of which way the user is holding their phone.

## Capture format
- Prefer short video sweeps (slow pan, ~1-2 seconds per orientation) over single stills — frames can be extracted and filtered for quality, giving more usable reference images per site visit than deliberately-composed stills.
- Phone held at approximate eye/chest height (1.2-1.5 m), matching how users will actually hold their phone while navigating.

## Landmark selection criteria (what to make sure gets captured)
Prioritize, per Section 5 of the brief:
- Structural: columns, walls, stairwells, elevator doors, ramps
- Signage: zone signs, floor signs, directional arrows, parking-zone markers
- Fixed floor markings (lane paint, arrows, numbers)
- Distinctive intersections

Explicitly avoid relying on: parked vehicles, people, temporary ads/barriers, anything movable. If a candidate capture position has nothing but parked cars in frame, walk to where structural/signage elements are visible instead.

## Environmental conditions to capture (where feasible)
- Different times of day (natural light changes near entrances/ramps)
- Both artificial-lighting-only areas and mixed-lighting areas
- At least one revisit of a subset of positions on a different day/lighting condition, to explicitly test VPR robustness to lighting drift (used later in Deliverable 4 benchmarking)

## Ground-truth coordinate assignment
- Each capture position gets X/Y/Z/floor/zone from the coordinate system in `01_coordinate_system.md`, either via physical floor markers measured with a laser distance meter / measuring wheel from the floor origin, or (temporarily, for early testing) estimated from the facility's architectural floor plan if available.
- Record heading per orientation shot from a compass/phone sensor at capture time — approximate is fine; VPR matching does not require perfect heading, geometric verification refines it.

## Naming convention
`F{floor:02d}_Z{zone}_{sequence:05d}_{orientation_deg:03d}.jpg`
Example: `F01_ZC_00245_090.jpg` = floor 1, zone C, capture #245, facing 90°.

## Test/ground-truth routes (for later evaluation, Deliverable 7)
Define 3-5 fixed walking routes through the facility (varying length/complexity) with known start/end coordinates, to be walked repeatedly during testing to measure localisation error, tracking stability, and relocalisation success against the acceptance targets in the project brief.

## Mock-site substitute (current phase)
Real capture isn't available yet. `scripts/generate_mock_images.py` synthesizes a small set of distinguishable placeholder "landmark" images (different shapes/colors standing in for distinct visual landmarks) with coordinates matching `data/mock_site/config.yaml`, so the dataset/VPR/geometric-verification code path can be built and tested now. This substitutes for real capture only — it does not validate real-world VPR accuracy, which requires the actual survey above.
