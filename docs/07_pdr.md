# Hybrid VPR + PDR Tracking

Implements the VIO-direction decision from the project's architecture
discussion: continuous position comes from client-side Pedestrian Dead
Reckoning (PDR), corrected periodically by server-side VPR fixes. Lives in
`fmc/api/static/app.js`, integrated directly into the live capture test
client (`docs/06_live_capture_test.md`).

## Why PDR, not double-integrated acceleration

Double-integrating raw accelerometer readings to get position drifts to
nonsense within 1-2 seconds — tiny sensor noise and bias compound
quadratically over time. PDR instead counts discrete steps and multiplies
by a stride estimate, which only accumulates error roughly linearly with
distance walked — slow enough that periodic VPR correction can keep up
with it. This is standard practice for phone-based pedestrian positioning;
it is not the same technique as inertial VIO/SLAM.

## How it works

**Step detection** — accelerometer magnitude (`|acceleration|`, orientation-
independent) is high-pass filtered against a slowly-adapting gravity
estimate (EMA, α=0.08) to isolate dynamic (footfall) acceleration. A
peak/valley state machine detects each step with a 280ms refractory period
(~3.5 steps/sec ceiling).

**Stride length** — either a fixed value (default 0.70m) or the Weinberg
formula `L = K · (a_peak − a_valley)^0.25` (K=0.43, clamped to [0.42,
1.05]m), selectable via the "Adaptive Stride" toggle. The Weinberg constant
is a rough starting point, not calibrated to any particular person's gait —
see "Tuning" below.

**Heading** — from the compass (`webkitCompassHeading` on iOS, `alpha` on
other browsers), continuously recalibrated by an offset computed at every
VPR fix: `offset = vpr_heading − raw_compass_heading`. This keeps the
compass aligned to the facility's coordinate frame (per
`01_coordinate_system.md`: 0°=+Y, clockwise) and self-corrects for slow
magnetometer drift, at the cost of a caveat below.

**Fusion** — on each VPR fix: hard-reset `(x, y)` to the match, recompute
the heading offset, log the drift (distance between where PDR had wandered
to and where VPR corrected it, in meters). On a VPR miss: PDR keeps
carrying the position forward unchanged — no snap to `(0,0)`, no freeze.
Confidence decays 1.5% per PDR step and resets on each VPR fix, mirroring
`fmc/fusion/sensor_fusion.py`'s Python decay model.

## Known caveats

- **Heading calibration inherits VPR's heading noise.** `vpr_heading` is
  the *matched reference photo's stored orientation*, not a live reading —
  and earlier field testing showed this value is inconsistent even at a
  fixed physical position (see `06_live_capture_test.md`'s test history).
  Since PDR's compass offset gets nudged by every VPR fix, a noisy VPR
  heading can introduce a visible sudden turn in the PDR trail right after
  a correction, even though the person didn't actually turn. Worth
  specifically checking for this pattern when reviewing a session's
  trajectory.
- **Weinberg's K constant is uncalibrated.** It needs tuning against a real
  person's actual gait (known-distance walk, compare PDR's reported
  distance to ground truth) before its stride estimates should be trusted
  quantitatively.
- **Step-confirmation may fire before the true acceleration valley**,
  slightly reducing Weinberg stride accuracy (the bounce term
  `peak − valley` can be measured before `valley` reaches its true
  minimum). Doesn't affect step *counting*, only stride *estimation*
  quality when adaptive stride is enabled.
- **Indoor magnetometer distortion** (rebar, structural steel, parked
  vehicles) remains a real risk in this specific environment, as flagged
  when PDR was first proposed — VPR-based recalibration mitigates but
  doesn't eliminate this.

## Reading a session afterward

`scripts/analyze_live_capture_log.py` handles the hybrid CSV format
(distinguishes VPR capture events from PDR step updates via the `source`
column) and reports **drift at VPR correction** — the single most useful
number for judging PDR quality: small, consistent drift means PDR is
tracking real movement well between fixes; large or erratic drift means
its step/heading estimate isn't trustworthy enough to carry the gaps
unassisted.

## Sensor calibration wizard (pre-session, "Sensor Calibration" card)

A 3-step wizard run once before Start Session, aimed at reducing PDR/heading
error at the source rather than only correcting it reactively at each VPR
fix. Click "Run Calibration"; each step runs automatically except the walk
step, which needs a tap to end it.

1. **Hold still (2.5s)** — averages accelerometer magnitude and gyroscope
   `rotationRate` over the window. The gyro average becomes a per-session
   **bias** subtracted from every subsequent gyro sample used for relative-
   yaw integration (MEMS gyros report a small nonzero rate even at rest,
   which otherwise integrates into real heading drift over a session). The
   accelerometer average seeds `gravityEMA` (previously hardcoded to 9.81),
   giving the step detector a correct baseline from sample zero instead of
   converging to it over the first several steps.
2. **Figure-8 rotation (6s), Android Chrome only** — if the Generic Sensor
   API's `Magnetometer` interface is available, collects raw magnetic-field
   samples while the phone is rotated through many orientations and fits a
   lightweight hard-iron (offset) + soft-iron (per-axis scale) correction
   from the min/max envelope traced out. **iOS Safari does not expose raw
   magnetometer data at all** — this step auto-skips there (and anywhere
   else `Magnetometer` isn't available or permission is denied), which is a
   real platform asymmetry, not a bug to keep chasing. On Android Chrome,
   if the constructor throws, the sensor may need "Generic Sensor Extra
   Classes" enabled in `chrome://flags` on that specific device. On success,
   a second `Magnetometer` instance keeps running for the rest of the
   session, feeding the new **Device-Calibrated** compass mode (below).
3. **Walk 5 steps straight** — point the phone the way you're about to
   walk, take 5 steps, tap "Done Walking". This reports how many steps the
   detector actually counted against the 5 expected (a direct, ground-
   truth-free diagnostic for whether Step Sensitivity is tuned too high or
   too low), and captures the *circular mean* of the raw compass heading
   sampled throughout the walk. That averaged value seeds the rolling
   heading buffer (see below) so the first real VPR fix computes its
   compass offset from a smoothed reading instead of one noisy instantaneous
   sample.

Calibration results (gyro bias, gravity, magnetometer status, walk step
count, current stride scale factor) are shown live in the card, and
appended as trailing `#`-comment lines when exporting CSV (ignored by
`analyze_live_capture_log.py`'s parsing, useful when comparing runs by eye).

### Device-Calibrated compass mode (experimental)

A new fourth `compassMode` option. Instead of trusting the OS's own fused
compass heading, it computes heading directly from the calibrated
magnetometer plus the live accelerometer vector (tilt compensation), giving
full control over the correction instead of relying on however the OS
happens to have calibrated its own compass. **Caveat: the exact sign/axis
convention in `computeTiltCompensatedHeading` is device-dependent and not
fully standardized across accelerometer/magnetometer chip vendors** — this
is exactly why it ships as an opt-in mode rather than the default. Validate
it against a couple of known bearings on the actual test device before
trusting it for a real walk; if it reads 180° off or mirrored, the sign
flips in that function need adjusting for this device.

### In-session stride-length refinement (not just at calibration time)

Separately from the calibration wizard, `refineStrideScaleFactor()` keeps
adjusting a session-wide `strideScaleFactor` (starts at 1.0) every time two
*consecutive genuine* VPR fixes bracket a stretch of PDR steps: it compares
the real distance between the two fixes to what the Weinberg formula (or
fixed step length) predicted for that many steps, and blends the ratio in
via an EMA (`×0.7 old + ×0.3 new`, clamped to `[0.5, 1.8]`) rather than a
hard override. **Guarded against confusable/wrong-location fixes**: if the
correction (`drift_m`) is larger than the distance PDR itself walked since
the last fix — the exact pattern seen in a real field-test bounce between
two visually similar locations — refinement is skipped for that pair,
since a wrong-location fix isn't a real ground-truth confirmation of the
path just walked and would corrupt the estimate rather than improve it.

## Tuning (all live in the page's Settings panel, no code changes needed)
- **Step Sensitivity** — accelerometer threshold (m/s²) for a step to
  register; raise if picking up false steps from phone jostling, lower if
  missing genuine steps
- **Base Step Length** — used directly when Adaptive Stride is off, and as
  a sanity bound when it's on
- **Compass Mode** — VPR-calibrated (default), raw device compass, or
  relative gyro yaw (for comparing against a magnetometer-free heading
  source)
- **Manual Heading Offset** — a constant correction on top of whichever
  compass mode is selected, for quick manual adjustment during testing
