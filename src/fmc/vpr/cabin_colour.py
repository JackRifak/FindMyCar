"""Floor confirmation from lift-lobby ("cabin") wall colour.

Each floor's lobby outside the lift is painted a different colour but has almost no
texture, so PnP can't lock there. Colour can't give a pose, but it can say *which floor*
you stepped out on, which is enough to (a) restrict PnP to that floor and (b) seed AR at
the known lift door.

Signature = hue histogram of strongly coloured pixels only (grey concrete, white light,
dark shadows are masked out), so it is fairly robust to exposure. One reference per floor
is captured from the mapping app under the real lighting and averaged over samples.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

logger = logging.getLogger(__name__)

HUE_BINS = 36  # 10° per bin
SAT_MIN = 70  # OpenCV S, 0..255 — below this counts as grey/white
VAL_MIN = 40  # too dark to trust the hue
# no upper V cut: a brightly lit wall clips to V=255 but keeps its hue; blown white fails SAT_MIN
# frame must be at least this colourful to say anything
MIN_COLOUR_FRAC = 0.06
# Bhattacharyya coefficient (0..1) needed for a match, and lead over the runner-up
MATCH_MIN = 0.6
MATCH_MARGIN = 0.12

REF_FILE = "cabin_colours.json"


@dataclass
class ColourSignature:
    hist: np.ndarray  # (HUE_BINS,) sums to 1
    colour_frac: float  # share of pixels that passed the colour mask


@dataclass
class CabinMatch:
    floor: str | None  # None = no confident match
    score: float  # best similarity
    margin: float  # best - second best
    colour_frac: float


def signature(image_bgr: np.ndarray) -> ColourSignature | None:
    """Hue histogram of saturated, well-exposed pixels; None if the frame is basically grey."""
    if image_bgr is None or image_bgr.size == 0:
        return None
    h, w = image_bgr.shape[:2]
    scale = 320.0 / max(h, w)
    if scale < 1.0:
        image_bgr = cv2.resize(image_bgr, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    hsv = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2HSV)
    hue, sat, val = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    mask = (sat >= SAT_MIN) & (val >= VAL_MIN)
    frac = float(mask.mean())
    if frac < MIN_COLOUR_FRAC:
        return ColourSignature(hist=np.zeros(HUE_BINS), colour_frac=frac)
    # weight by saturation so vivid wall paint beats a faint tint
    hist, _ = np.histogram(hue[mask], bins=HUE_BINS, range=(0, 180), weights=sat[mask].astype(np.float64))
    # circular smoothing — small white-balance shifts land in the neighbouring bin
    hist = 0.5 * hist + 0.25 * np.roll(hist, 1) + 0.25 * np.roll(hist, -1)
    total = hist.sum()
    if total <= 0:
        return ColourSignature(hist=np.zeros(HUE_BINS), colour_frac=frac)
    return ColourSignature(hist=hist / total, colour_frac=frac)


def similarity(a, b) -> float:
    """Bhattacharyya coefficient of two hue histograms (1 = identical)."""
    return float(np.sum(np.sqrt(np.asarray(a, dtype=np.float64) * np.asarray(b, dtype=np.float64))))


_similarity = similarity


def _ref_path(index_dir: Path) -> Path:
    return Path(index_dir) / REF_FILE


def load_references(index_dir: Path) -> dict[str, dict]:
    p = _ref_path(index_dir)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("[Cabin] could not read %s: %s", p, e)
        return {}


def save_references(index_dir: Path, refs: dict[str, dict]) -> None:
    _ref_path(index_dir).write_text(json.dumps(refs, indent=2), encoding="utf-8")


def add_reference(index_dir: Path, floor: str, image_bgr: np.ndarray) -> dict:
    """Average this frame into the floor's reference. Raises ValueError if it's too grey."""
    sig = signature(image_bgr)
    if sig is None or sig.colour_frac < MIN_COLOUR_FRAC:
        frac = 0.0 if sig is None else sig.colour_frac
        raise ValueError(
            f"only {frac:.0%} of the frame is coloured — point at the painted cabin walls"
        )
    refs = load_references(index_dir)
    cur = refs.get(floor)
    if cur:
        n = int(cur.get("samples", 1))
        hist = (np.asarray(cur["hist"]) * n + sig.hist) / (n + 1)
        frac = (float(cur.get("colour_frac", sig.colour_frac)) * n + sig.colour_frac) / (n + 1)
    else:
        n, hist, frac = 0, sig.hist, sig.colour_frac
    refs[floor] = {
        "hist": [round(float(v), 5) for v in hist / max(hist.sum(), 1e-9)],
        "colour_frac": round(frac, 4),
        "dominant_hue_deg": int(np.argmax(hist)) * (360 // HUE_BINS) + 360 // HUE_BINS // 2,
        "samples": n + 1,
        "updated": time.time(),
    }
    save_references(index_dir, refs)
    # warn when two floors look alike — the classifier won't separate them
    for other, ref in refs.items():
        if other == floor:
            continue
        sim = _similarity(np.asarray(refs[floor]["hist"]), np.asarray(ref["hist"]))
        if sim >= 1.0 - MATCH_MARGIN:
            logger.warning("[Cabin] floor %s colour is very close to floor %s (sim=%.2f)", floor, other, sim)
    return refs[floor]


def classify(image_bgr: np.ndarray, refs: dict[str, dict]) -> CabinMatch:
    """Best-matching floor by cabin colour, or floor=None when not confident."""
    sig = signature(image_bgr)
    if sig is None or not refs:
        return CabinMatch(None, 0.0, 0.0, 0.0 if sig is None else sig.colour_frac)
    if sig.colour_frac < MIN_COLOUR_FRAC:
        return CabinMatch(None, 0.0, 0.0, sig.colour_frac)
    scored = sorted(
        ((_similarity(sig.hist, np.asarray(ref["hist"])), floor) for floor, ref in refs.items()),
        reverse=True,
    )
    best, floor = scored[0]
    second = scored[1][0] if len(scored) > 1 else 0.0
    margin = best - second
    ok = best >= MATCH_MIN and margin >= MATCH_MARGIN
    return CabinMatch(floor if ok else None, best, margin, sig.colour_frac)
