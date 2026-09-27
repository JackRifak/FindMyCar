# AI AGENT PROJECT CONTEXT (FULL ORIGINAL BRIEF)

## Visual-Based Indoor Parking Navigation

### 1. Project Overview

We are developing a full-scale indoor parking navigation system that helps a driver find their parked vehicle inside a large indoor parking facility.

The parking facility already has an existing parking management system that provides:

* Vehicle identification
* ANPR / license plate recognition
* Parking slot occupancy detection
* Vehicle-to-parking-slot mapping
* Parking slot information through an API

Therefore, **we do NOT need to develop vehicle identification, occupancy detection, or vehicle-to-slot detection**.

Our system will consume the existing parking system API.

The new system is responsible primarily for:

1. Indoor user localisation
2. Visual positioning
3. Continuous user tracking
4. Indoor pedestrian navigation
5. AR-based navigation
6. Integration with the existing parking API
7. Mobile web application integration

### 2. Selected Positioning Technology

The selected approach is:

**Camera-Based Visual Positioning + Visual-Inertial Odometry (VIO)**

BLE-based positioning is NOT the primary approach for this project.

The system should combine:

* Smartphone camera
* Accelerometer
* Gyroscope
* Visual Place Recognition (VPR)
* Image feature matching
* Image embeddings
* Geometric verification
* Visual-Inertial Odometry
* Position estimation
* Heading/orientation estimation
* Indoor map matching

The objective is to determine:

> "Where is the user inside the parking facility, and which direction are they facing?"

This position will then be used by the navigation engine to guide the user to their parked vehicle.

---

# 3. High-Level System Architecture

The intended system flow is:

User opens mobile web application

↓

Existing Parking Management API

↓

Retrieve user's parked vehicle

↓

Retrieve vehicle's parking slot / location

↓

Camera + IMU from smartphone

↓

Visual localisation

↓

Visual-Inertial Odometry

↓

User position + orientation

↓

Indoor navigation map

↓

Route calculation

↓

AR navigation

↓

Guide user to parked vehicle

The Computer Vision subsystem is primarily responsible for:

Camera + IMU

↓

Visual Place Recognition

*

VIO

↓

User Position + Orientation

↓

Navigation System

---

# 4. Important Concept: Global Localisation vs Continuous Tracking

Do NOT treat image embeddings and VIO as the same problem.

They have different purposes.

### Visual Place Recognition

VPR answers:

> "Where am I?"

For example:

The user points the camera toward a particular corridor.

The system compares the camera image against previously captured reference images.

It identifies:

> "This looks like the north corridor near Column C17."

This provides a global position estimate.

### VIO

VIO answers:

> "How have I moved from my previous position?"

The camera and IMU continuously estimate movement.

For example:

Initial position:

X = 25 m
Y = 42 m

After the user walks:

X = 27 m
Y = 46 m

VIO continuously tracks this movement.

### Combined System

Use:

**VPR → Global localisation / re-localisation**

**VIO → Continuous local tracking**

Therefore:

VPR + VIO → Robust indoor positioning

---

# 5. Visual Map

The parking facility must be surveyed before the positioning system can be properly developed.

We need to create a visual reference map/database.

Capture images or video throughout the relevant parking areas.

Important visual features include:

* Columns
* Walls
* Doors
* Signs
* Parking-zone signs
* Floor markings
* Arrows
* Ramps
* Staircases
* Elevators
* Intersections
* Pedestrian corridors
* Unique structural features
* Other stable visual landmarks

Avoid depending heavily on temporary objects such as:

* Parked vehicles
* People
* Temporary advertisements
* Temporary barriers
* Movable objects

The visual map should primarily contain **stable environmental features**.

Each reference image should have metadata such as:

```text
image_id
floor
zone
x_coordinate
y_coordinate
orientation
timestamp
camera_information
embedding
feature_descriptors
```

Example:

```json
{
  "image_id": "F01_Z03_IMG0245",
  "floor": 1,
  "zone": "Zone_C",
  "x": 42.5,
  "y": 67.2,
  "orientation": 90,
  "embedding": "...",
  "features": "..."
}
```

---

# 6. Visual Place Recognition Pipeline

The CV system should investigate a robust VPR pipeline.

Basic pipeline:

```text
User Camera Frame
        ↓
Image Preprocessing
        ↓
Image Embedding Model
        ↓
Vector Similarity Search
        ↓
Top-K Candidate Reference Images
        ↓
Geometric Verification
        ↓
Best Matching Location
        ↓
Estimated Position
```

The system should NOT rely only on cosine similarity between embeddings.

Embedding similarity should generate candidate locations.

Then use geometric verification to determine whether the candidate is actually correct.

Possible approaches include:

* Local feature detection
* Feature descriptors
* Feature matching
* Homography verification where appropriate
* Essential/fundamental matrix verification where appropriate
* RANSAC
* Camera pose estimation
* 3D landmarks where available

The AI agent should investigate and compare suitable modern approaches.

---

# 7. VIO Pipeline

The system should investigate existing robust VIO/SLAM solutions rather than unnecessarily implementing VIO from scratch.

Inputs:

```text
Camera frames
+
Accelerometer
+
Gyroscope
```

Output:

```text
Position
Orientation
Velocity
Tracking state
```

The system should investigate suitable technologies/frameworks such as:

* OpenVINS
* VINS-Fusion
* ORB-SLAM / ORB-SLAM3
* ARCore
* ARKit
* Web-compatible alternatives
* Other suitable production-grade VIO/SLAM frameworks

The AI agent must evaluate these based on:

* Smartphone compatibility
* Android support
* iOS support
* Mobile browser limitations
* Accuracy
* Real-time performance
* Integration complexity
* Licensing
* Backend vs device-side processing

Do not select a framework simply because it is popular. Evaluate it against the project's actual requirements.

---

# 8. Visual Relocalisation

VIO will accumulate drift and can lose tracking.

Therefore, the system needs a relocalisation mechanism.

Example:

```text
VIO tracking
     ↓
User walks
     ↓
Tracking drift/loss
     ↓
Camera frame
     ↓
VPR
     ↓
Recognise known location
     ↓
Reset / correct VIO position
     ↓
Continue tracking
```

This is an important part of the project.

The target architecture should therefore support:

**VPR → VIO → VPR → VIO → VPR**

rather than performing VPR only once.

---

# 9. Indoor Coordinate System

The CV system must work with a defined indoor coordinate system.

For example:

```text
             Y
             ↑
             │
             │
             │
             └────────────→ X
```

Every visual reference location should correspond to this coordinate system.

The CV localisation engine should ultimately return something similar to:

```json
{
  "floor": 1,
  "x": 42.5,
  "y": 67.2,
  "z": 0.0,
  "heading": 87.5,
  "confidence": 0.93,
  "tracking_status": "tracking"
}
```

The navigation system can then use this position.

---

# 10. Navigation Integration

The Computer Vision system does NOT need to calculate the entire navigation route itself.

The navigation subsystem will use:

```text
Current User Position
+
Destination Vehicle Slot
+
Indoor Walkable Map
```

to calculate the route.

For example:

```text
Current Position
      ↓
     P23
      ↓
     P31
      ↓
     P45
      ↓
   Zone C
      ↓
 Parking Slot C034
```

A graph-based navigation algorithm such as A* or Dijkstra can be used.

The CV system's responsibility is to provide:

**Accurate current position + heading + confidence.**

---

# 11. AR Navigation

Once localisation is sufficiently reliable, the system can provide AR navigation.

Example:

User opens camera.

↓

System identifies current position.

↓

Route is calculated.

↓

AR overlay shows:

* Direction arrow
* Turn instruction
* Distance
* Destination marker
* Parking slot location

Example:

```text
          ↑
       WALK 18 m
          ↑
          ↑
     TURN RIGHT →
```

The AR layer must be correctly aligned with the real-world environment.

Therefore, AR should not be considered the first development task.

First prove:

**Can we reliably localise the user?**

Then:

**Can we continuously track the user?**

Then:

**Can we align the navigation route with the physical environment?**

---

# 12. Computer Vision Engineer Responsibilities

My role in this project is:

## A. Visual Survey and Dataset

Responsible for designing the visual data collection methodology.

Tasks:

* Define visual survey requirements
* Determine image/video capture locations
* Determine capture spacing
* Determine camera orientations
* Identify stable visual landmarks
* Define environmental conditions to capture
* Collect reference imagery
* Organise the dataset
* Define metadata format
* Identify difficult/repetitive areas

Deliverables:

* Visual reference dataset
* Dataset structure
* Image metadata
* Visual landmark database
* Data collection guidelines

---

# 13. Visual Map Construction

Tasks:

* Process captured images/video
* Extract frames
* Remove poor-quality frames
* Remove redundant images
* Assign coordinates
* Assign floor/zone information
* Generate image embeddings
* Generate local feature descriptors
* Build visual reference database
* Build vector index

Potential technologies:

* Python
* OpenCV
* PyTorch
* Hugging Face
* FAISS
* Qdrant
* PostgreSQL + pgvector

The final technology should be selected based on the actual project requirements.

---

# 14. Image Embedding / VPR Development

Tasks:

* Research suitable image embedding models
* Benchmark multiple candidate models
* Generate embeddings
* Build similarity-search pipeline
* Test Top-1, Top-5 and Top-K retrieval
* Evaluate visually similar areas
* Evaluate different lighting conditions
* Evaluate different camera orientations
* Evaluate partial occlusion
* Evaluate parked-vehicle changes
* Evaluate different smartphones
* Develop confidence scoring

Important evaluation:

```text
Query Image
     ↓
Embedding
     ↓
Vector Search
     ↓
Top 5 candidates
     ↓
Geometric Verification
     ↓
Correct / Incorrect
```

Measure:

* Top-1 accuracy
* Top-5 accuracy
* Localization error
* False matches
* Confidence
* Processing time

---

# 15. Feature Matching and Geometric Verification

Develop a second-stage verification mechanism.

Potential technologies:

* SIFT
* ORB
* SuperPoint
* SuperGlue
* LightGlue
* LoFTR
* Other modern feature matching methods

The agent should benchmark appropriate methods rather than assuming one is best.

Example:

```text
Embedding Search
      ↓
Top 5 candidates
      ↓
Feature Matching
      ↓
RANSAC / Geometric Verification
      ↓
Best Candidate
      ↓
Position
```

This is important because two different parking areas can look very similar.

---

# 16. VIO / SLAM Investigation

Tasks:

* Research suitable VIO frameworks
* Build small test environments
* Test camera + IMU tracking
* Measure drift
* Measure tracking stability
* Test walking scenarios
* Test turns
* Test stopping/starting
* Test poor visual environments
* Test tracking loss
* Test relocalisation

The goal is to determine:

> How accurately can the system track a pedestrian between known visual landmarks?

---

# 17. Sensor Fusion

Investigate combining:

```text
VPR
+
VIO
+
IMU
+
Map Constraints
```

Potential architecture:

```text
             Camera
                ↓
          VPR / Features
                ↓
          Global Position
                ↓
IMU → VIO → Local Tracking
                ↓
          Sensor Fusion
                ↓
           Map Matching
                ↓
        Final User Position
```

The final position should contain:

```text
X
Y
Floor
Heading
Confidence
Tracking State
Timestamp
```

---

# 18. Map Matching

The CV position should be constrained against the indoor pedestrian map.

For example, the raw CV estimate may be:

```text
X = 43.2
Y = 67.8
```

but that position could be inside a wall.

Map matching should determine the nearest valid pedestrian position.

The system should investigate:

* Walkable-path constraints
* Corridor constraints
* Floor constraints
* Zone constraints
* Heading constraints
* Position smoothing

---

# 19. Testing Responsibilities

Create a formal testing methodology.

Test conditions should include:

### Lighting

* Bright
* Dark
* Artificial lighting
* Different times

### Environment

* Empty parking area
* Full parking area
* Different parked vehicles
* People walking
* Partial obstruction

### User movement

* Slow walking
* Normal walking
* Fast walking
* Stopping
* Turning
* Changing direction

### Camera

* Camera facing forward
* Slightly downward
* Different orientations
* Different distances from landmarks

### Devices

Test multiple target smartphones.

The system should document device-specific limitations.

---

# 20. Performance Metrics

The CV subsystem should be evaluated using measurable metrics.

Important metrics:

### Global localisation accuracy

Distance between estimated and ground-truth position.

Example:

```text
Ground truth: (42.0, 67.0)
Estimated:     (43.1, 68.2)

Error = 1.63 m
```

### VIO tracking error

Measure drift over walking distance.

### Relocalisation success rate

Percentage of tracking-loss events successfully recovered.

### VPR retrieval accuracy

* Top-1
* Top-5
* Top-K

### Processing latency

Time required to generate a position.

### Update frequency

Positions per second.

### Tracking availability

Percentage of walking time during which the system maintains valid localisation.

### Navigation success

Percentage of users successfully reaching their vehicle.

---

# 21. Proposed Initial Acceptance Targets

These are initial engineering targets and must be validated during the POC.

Target values to investigate:

| Metric                     |               Initial Target |
| -------------------------- | ---------------------------: |
| Global visual localisation |                      ≤ 2–5 m |
| Continuous tracking        | Stable during normal walking |
| Relocalisation success     |                        ≥ 90% |
| Navigation success         |                        ≥ 90% |
| Position update latency    |                ≤ 2–3 seconds |
| VPR Top-5 retrieval        |                        ≥ 90% |
| Tracking recovery          |                        ≥ 90% |
| Floor identification       |                        ≥ 95% |

These values are **targets for evaluation, not guaranteed results**.

---

# 22. What the AI Agent Must NOT Do

Do not spend development effort on:

* ANPR
* License plate recognition
* Parking occupancy detection
* Vehicle detection
* Vehicle-to-slot detection
* BLE positioning
* Developing the existing parking management system
* Building a complete Digital Twin before the visual localisation concept is proven

Those systems already exist or are outside the primary CV responsibility.

The CV system should consume existing APIs and provide localisation services to the rest of the system.

---

# 23. Most Important First Task

## DO NOT START WITH AR.

The first task should be:

### PHASE 1 — SITE SURVEY + VISUAL DATA COLLECTION PLAN

Before implementing sophisticated AI models, understand the physical environment.

The AI agent should first help produce:

1. Required parking-area information
2. Coordinate-system definition
3. Visual survey methodology
4. Camera/video capture strategy
5. Reference-image spacing
6. Landmark selection criteria
7. Dataset structure
8. Ground-truth collection methodology
9. Testing routes
10. Test scenarios
11. Target smartphone/device list
12. Environmental conditions

Then perform the actual visual survey and dataset collection.

---

# 24. Development Order

The project should proceed in this order:

### Step 1 — Site Survey

Understand the physical parking facility.

↓

### Step 2 — Define Coordinate System

Establish:

```text
Floor
X
Y
Z
Heading
```

↓

### Step 3 — Capture Visual Dataset

Create reference images/video throughout the facility.

↓

### Step 4 — Build Visual Database

Images + coordinates + metadata.

↓

### Step 5 — Benchmark Image Embeddings

Determine whether visual similarity can reliably identify locations.

↓

### Step 6 — Implement VPR

Query image → candidate location.

↓

### Step 7 — Add Geometric Verification

Reduce false visual matches.

↓

### Step 8 — Implement/Test VIO

Continuous movement tracking.

↓

### Step 9 — Combine VPR + VIO

Global localisation + continuous tracking.

↓

### Step 10 — Map Matching

Constrain position to walkable areas.

↓

### Step 11 — Navigation Integration

Current position → vehicle slot → route.

↓

### Step 12 — AR Layer

Display navigation instructions through the camera.

↓

### Step 13 — Full Field Testing

Validate the complete system under realistic parking conditions.

---

# 25. Immediate Deliverables Required From the AI Agent

The AI agent should begin by producing the following:

### Deliverable 1

**Computer Vision System Architecture**

Detailed architecture for:

```text
Camera
↓
VPR
↓
Geometric Verification
↓
VIO
↓
Sensor Fusion
↓
Map Matching
↓
Position
```

### Deliverable 2

**Site Survey and Data Collection Specification**

Define exactly how the parking facility should be surveyed.

### Deliverable 3

**Visual Dataset Specification**

Define:

* Image format
* Resolution
* Capture interval
* Metadata
* Coordinate format
* Naming convention
* Storage structure

### Deliverable 4

**VPR Model Benchmark Plan**

Compare suitable embedding models.

### Deliverable 5

**VIO Technology Evaluation**

Compare suitable VIO/SLAM technologies for the target smartphones.

### Deliverable 6

**POC Development Plan**

Define implementation phases, dependencies, milestones and measurable acceptance criteria.

### Deliverable 7

**Ground-Truth and Testing Plan**

Define how localisation accuracy will be measured against known physical coordinates.

---

# 26. Final Objective

The final Computer Vision subsystem should expose a simple positioning interface to the rest of the application.

For example:

```json
{
  "floor": 1,
  "x": 42.5,
  "y": 67.2,
  "heading": 87.5,
  "confidence": 0.93,
  "tracking": true,
  "timestamp": 1750000000
}
```

The rest of the application should not need to know how the position was calculated.

The CV subsystem should abstract away:

* Image processing
* Embeddings
* VPR
* Feature matching
* Geometric verification
* VIO
* IMU processing
* Sensor fusion
* Relocalisation
* Position filtering

and provide a reliable:

**USER POSITION + HEADING + CONFIDENCE**

to the navigation system.

## Primary engineering goal

The primary goal is **not to demonstrate that a camera can recognise a parking area**.

The real goal is:

> **Determine whether smartphone-based visual localisation combined with VIO can provide sufficiently accurate, stable, repeatable, and scalable indoor positioning for real-world pedestrian navigation inside the target parking facility.**
