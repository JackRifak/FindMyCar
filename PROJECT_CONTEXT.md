# Find My Car — Visual Indoor Parking Navigation

## What this is
Indoor navigation system that guides a driver from their phone to their parked car inside a large indoor parking facility, using **camera-based visual positioning + Visual-Inertial Odometry (VIO)** — not BLE. AR overlay guides the final walk.

## Explicitly out of scope (already exists / not our job)
ANPR, license plate recognition, occupancy detection, vehicle-to-slot detection, BLE positioning — all provided by an **existing parking management API** we just consume (vehicle → slot lookup).

## Core technical approach
Two complementary subsystems, not one:
- **VPR (Visual Place Recognition)** — "Where am I?" Global/re-localisation: query camera frame → image embedding → vector similarity search → top-K candidate reference images → **geometric verification** (feature matching + RANSAC + homography/essential-matrix, NOT cosine similarity alone) → best match → position.
- **VIO** — "How have I moved since then?" Continuous local tracking via camera + accelerometer + gyroscope. Drifts over time/loses tracking.
- **Combined loop:** `VPR → VIO → VPR → VIO → VPR ...` (VPR re-localises whenever VIO drifts/loses tracking, not just once at start).

Candidate VIO/SLAM frameworks to evaluate (not pre-selected): OpenVINS, VINS-Fusion, ORB-SLAM3, ARCore, ARKit, web-compatible alternatives — judged on mobile browser compatibility, accuracy, real-time perf, licensing, on-device vs backend.

Candidate feature-matching methods for geometric verification: SIFT, ORB, SuperPoint, SuperGlue, LightGlue, LoFTR.

Candidate visual-DB/vector-search stack: OpenCV, PyTorch/HF for embeddings, FAISS / Qdrant / PostgreSQL+pgvector for the index.

## System pipeline (end to end)
```
Mobile web app → Existing Parking API (get vehicle's slot)
Phone camera + IMU → VPR (global pos) → Geometric verification
                   → VIO (continuous tracking)
                   → Sensor fusion → Map matching (snap to walkable path)
                   → Position {floor, x, y, heading, confidence, tracking_status}
                   → Navigation engine (A*/Dijkstra over indoor walkable map)
                   → AR overlay guiding user to the slot
```

## CV subsystem's contract to the rest of the app
Exposes only a clean position interface — callers don't need to know how it's computed:
```json
{"floor":1,"x":42.5,"y":67.2,"z":0.0,"heading":87.5,"confidence":0.93,"tracking_status":"tracking","timestamp":1750000000}
```

## Visual reference map (needed before any model work)
Site must be surveyed first — capture images/video of **stable** landmarks only (columns, walls, doors, signs, floor markings, ramps, stairs, elevators, intersections) and explicitly avoid transient objects (parked cars, people, temp ads/barriers). Each reference image needs metadata: `image_id, floor, zone, x, y, orientation, timestamp, camera_information, embedding, feature_descriptors`.

## Development order (do not skip ahead)
1. Site survey → 2. Define coordinate system (floor/X/Y/Z/heading) → 3. Capture visual dataset → 4. Build visual reference DB → 5. Benchmark embedding models → 6. Implement VPR → 7. Add geometric verification → 8. Implement/test VIO → 9. Combine VPR+VIO → 10. Map matching → 11. Navigation integration → 12. AR layer (last, not first) → 13. Full field testing.

**Do not start with AR.** First prove localisation works, then continuous tracking, then route alignment.

## Acceptance targets (POC targets, not guarantees)
| Metric | Target |
|---|---|
| Global visual localisation error | ≤ 2–5 m |
| Relocalisation success | ≥ 90% |
| Navigation success | ≥ 90% |
| Position update latency | ≤ 2–3 s |
| VPR Top-5 retrieval | ≥ 90% |
| Tracking recovery | ≥ 90% |
| Floor identification | ≥ 95% |

## My role (Computer Vision Engineer) — deliverables owed
1. CV system architecture (camera→VPR→geometric verification→VIO→sensor fusion→map matching→position)
2. Site survey & data collection spec
3. Visual dataset spec (format, resolution, capture interval, metadata, naming, storage)
4. VPR model benchmark plan
5. VIO technology evaluation (vs. target smartphones)
6. POC development plan (phases, milestones, acceptance criteria)
7. Ground-truth & testing plan (accuracy vs. known physical coordinates)

## Testing dimensions to cover
Lighting (bright/dark/artificial/time-of-day), environment (empty/full lot, obstruction), user movement (slow/fast/turning/stopping), camera angle/distance, multiple target smartphones.

---
*Source: full original project brief archived alongside this file — see `PROJECT_BRIEF_FULL.md` if present. This file is the condensed version for fast AI context loading.*
