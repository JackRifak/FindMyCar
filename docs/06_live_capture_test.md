# Live Capture Test — Server-Side VIO Trial

Tests the "server-side VIO via periodic VPR" direction discussed for
Deliverable 5, before falling back to client-side PDR if it doesn't hold
up. This is a real client hitting your actual `/localize` endpoint with
adaptive-rate frame capture (not continuous video streaming) — same
session/fusion logic (`PositionFuser`) the production app will use.

## Required: HTTPS (or localhost)

Browsers only allow camera access (`getUserMedia`) and motion-sensor access
(`DeviceMotionEvent`) in a **secure context** — HTTPS, or the literal
hostname `localhost`. Testing on your phone against your laptop's LAN IP
over plain HTTP will silently fail (camera permission prompt won't even
appear, or `DeviceMotionEvent.requestPermission` will be missing). Two ways
to get HTTPS for local testing:

**Option A — ngrok tunnel (recommended)** — no certificate warnings, works
even if your phone isn't on the same WiFi network:

1. Install ngrok (Debian/Ubuntu):
   ```bash
   curl -sSL https://ngrok-agent.s3.amazonaws.com/ngrok.asc | sudo tee /etc/apt/trusted.gpg.d/ngrok.asc >/dev/null
   echo "deb https://ngrok-agent.s3.amazonaws.com buster main" | sudo tee /etc/apt/sources.list.d/ngrok.list
   sudo apt update && sudo apt install ngrok
   ```
2. Sign up free at https://dashboard.ngrok.com/signup, copy your authtoken
   from the dashboard, then run once:
   ```bash
   ngrok config add-authtoken <YOUR_TOKEN>
   ```
3. Run the server plainly (no SSL flags — ngrok terminates HTTPS for you):
   ```bash
   FMC_SITE_ID=site_00 uvicorn fmc.api.server:app --port 8000
   ```
4. In a second terminal:
   ```bash
   ngrok http 8000
   ```
   It prints a forwarding URL like `https://a1b2-xx-xx-xx-xx.ngrok-free.app`.
5. Open that URL on your phone. First visit shows a one-time ngrok
   interstitial ("You are about to visit...") — click **Visit Site**.

Two things to know: the free tier gives you a **new random URL every time
you restart the tunnel** (reopen it on your phone each session), and since
the frontend and API are served from the same origin regardless of which
domain reaches them, no CORS or server code changes are needed for this to
work.

**Option B — self-signed certificate** (no external service needed, but
the browser shows an "unsafe" warning to click through, and your phone must
be on the same WiFi network as your laptop):
```bash
openssl req -x509 -newkey rsa:2048 -keyout key.pem -out cert.pem -days 365 -nodes -subj "/CN=localhost"
FMC_SITE_ID=site_00 uvicorn fmc.api.server:app --host 0.0.0.0 --port 8000 \
    --ssl-keyfile key.pem --ssl-certfile cert.pem
```
Then on your phone: `https://<laptop-lan-ip>:8000/` — accept the
certificate warning ("Advanced" → "Proceed").

## Run it
With ngrok (Option A), the server command has no SSL flags — see above.
With the self-signed cert (Option B):
```bash
FMC_SITE_ID=site_00 uvicorn fmc.api.server:app --host 0.0.0.0 --port 8000 --ssl-keyfile key.pem --ssl-certfile cert.pem
```
Either way, open the HTTPS URL on your phone, tap **Start**, grant camera +
motion permissions (iOS shows an explicit motion-sensor prompt; this only
appears because the request happens inside the Start button's click
handler — don't remove that constraint if editing the JS).

## What it does
- Captures a frame only when the phone has moved enough (accelerometer
  magnitude change past a threshold) **or** a max interval has elapsed,
  whichever comes first — this is the "adaptive rate" from the
  optimization discussion, not fixed-rate video streaming
- Downscales each frame before upload (configurable max dimension)
- Posts each frame to `/localize?device_id=<random-per-session>` — the
  same endpoint and `PositionFuser` session logic the real app will use
- Shows live position, confidence, and round-trip latency
- Keeps a rolling log (last 30 shown, up to 500 kept in memory) and can
  export the full session as CSV for later analysis

## Tunable settings (in the page itself, no code changes needed)
- **Min interval** — floor on capture rate, even if moving continuously
  (prevents overwhelming the server/network)
- **Max interval** — ceiling — send at least this often even standing still,
  so confidence doesn't decay to nothing between fixes
- **Motion threshold** — how much accelerometer change triggers an
  early capture
- **Max image dimension** — bandwidth/accuracy tradeoff for each frame

## What to actually look for while walking a real route
- **Does position update smoothly enough to be usable**, or does it feel
  laggy/jumpy between fixes? (there's no interpolation in this test client
  on purpose — you're seeing raw fix-to-fix behavior, which is the honest
  signal for whether this approach needs a smoothing layer on top)
- **Latency** — the `latency=`/`avg=` numbers in the readout. If these
  regularly exceed a second or two, that alone may be disqualifying against
  the brief's own position-update-latency target (Section 21)
- **Confidence and tracking dropouts** — does `tracking=false` show up
  often, or only in genuinely hard spots (poor lighting, featureless walls)?
- **Compare the CSV log's x/y trail against the actual path you walked** —
  systematic errors (wrong turns, backtracking that didn't happen) point at
  VPR/geometry issues; noisy jitter around a roughly correct path points at
  needing the smoothing/interpolation layer PDR would naturally provide

## Analyzing a session log
After downloading a session's CSV, get an automated read instead of
eyeballing it:
```bash
python scripts/analyze_live_capture_log.py live_capture_XXXX.csv
```
Reports match rate, latency stats, average confidence, and how many
*distinct* positions actually appeared — a low distinct-position count
relative to matched frames is a strong sign that matches are snapping onto
a small handful of known survey locations rather than tracking real
movement, which points at sparse survey coverage rather than a flaw in the
approach itself. See "First real-world run" below for a worked example.

## First real-world run (2026-09-17) — coverage, not the approach, was the limiter
Initial field test on site_00 (4 of 10-30 planned locations ingested, only
2 walkable segments):
- **Latency: 61-491ms** across every request — comfortably inside the
  brief's ≤2-3s target. This was the main theoretical risk going in, and it
  wasn't the bottleneck.
- **~48% no-match rate**, and matched positions collapsed onto only 3
  distinct points — which turned out to be exactly the coordinates of
  3 of the 4 ingested capture locations (P5/P6/P8), snapped onto the
  nearest of the 2 walkable segments. Walking anywhere without a nearby
  reference photo simply had nothing to match against.
- **Heading was inconsistent at a fixed matched position** (e.g. 268°, then
  5°, then 257°, then 202° while "at" the same spot) — expected, since VPR
  heading is the matched reference photo's stored heading, not a live
  reading. This is a known limitation of VPR-only heading regardless of
  which tracking approach is used going forward.

**Conclusion so far: inconclusive on the core approach, pending denser
coverage.** The right next step before judging server-side periodic-VPR
pass/fail is to ingest more locations along the actual walked route and
expand the walkable geometry to match, then re-run this same test. If match
rate and position smoothness improve with coverage, that's real signal for
the approach; if it's still choppy with dense coverage, that's real signal
for moving to PDR.

## Decision point
If this holds up well enough on a real walkthrough — good match rate,
tolerable latency, position trail roughly matches the real path — this can
stay the primary approach, with client-side PDR added later purely as a
smoothing/interpolation layer between fixes rather than a fallback.

If it doesn't hold up (too laggy, too many dropouts, position trail doesn't
track the real path), that's the signal to move to PDR as the primary
tracking layer, per the earlier discussion.
