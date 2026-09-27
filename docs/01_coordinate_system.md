# Coordinate System Definition

## Convention
- **Units:** meters, floating point.
- **Origin (0, 0):** fixed at one physical reference corner per floor — typically the entrance/ramp corner closest to the facility's main pedestrian entrance. Marked physically (or virtually, for the mock site) so it can be re-surveyed if lost.
- **Axes:** standard right-handed 2D plan, `X` increasing east/right along the entrance wall, `Y` increasing north/away from the entrance, as viewed on the facility floor plan.
- **Z:** floor height offset in meters from the ground floor slab (0.0 = ground floor reference plane). Used mainly to disambiguate floors in multi-floor sensor fusion, not for fine positioning.
- **Floor:** integer floor index (0 = ground, 1, 2, ... for upper levels, -1, -2 for basements).
- **Heading:** degrees, 0-360, clockwise from `+Y` (i.e. compass-style: 0 = facing "north"/+Y, 90 = facing "east"/+X).
- **Zone:** human-readable label (`Zone_A`, `Zone_B`, ...) — a coarse partition of a floor used for indexing/filtering VPR candidates before geometric verification, not a positioning unit itself.

## Per-floor origin table
One entry per floor, established during site survey and never changed afterward (a changed origin invalidates the whole reference dataset for that floor):

| Floor | Origin physical description | Notes |
|---|---|---|
| (mock) 0 | Corner of "Zone_A" nearest mock entrance marker | Placeholder until real site surveyed |

## Mock site definition (current)
Since no physical facility is available yet, `data/mock_site/config.yaml` defines a small synthetic single-floor facility (one floor, two zones, a handful of waypoints and a walkable-path graph) purely so the pipeline (dataset → embedding → VPR → geometric verification → fusion → map matching → position API) can be exercised end-to-end before real survey data exists. Every coordinate in the mock config is arbitrary and must be discarded once real survey data replaces it — nothing about the mock geometry should leak into production assumptions.

## Ground truth marking (for real survey, later)
When the real survey happens, ground-truth points must be physically marked (floor tape/markers at known surveyed X/Y) at a sparse grid so that:
1. Reference images can be tagged with accurate coordinates.
2. Evaluation walks (Deliverable 7) can compare estimated vs. true position.
