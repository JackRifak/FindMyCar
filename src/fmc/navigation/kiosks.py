"""Parking payment kiosks as known start points.

After paying, the kiosk shows a QR code with the user's bay and the kiosk's ID:

    https://<server>/parking/?slot=86-03C&kiosk=K2

Scanning it in the parking app gives the destination AND the start position: the user is
standing in front of that kiosk's screen, facing it. No localization needed (an ArUco marker
on the kiosk, when registered, refines it to a few cm).

Registry: config.yaml, per floor (written by the floor plan editor, Kiosks mode):

    floors:
    - floor: B1
      kiosks:
      - kiosk_id: K2
        x: 12.4          # facility metres, the kiosk screen
        y: -18.0
        facing_deg: 180  # direction the SCREEN faces (towards the user), 0 = +y, 90 = +x
        stand_m: 0.6     # where the user stands, this far in front of the screen
        marker_id: 5     # optional: ArUco marker on the kiosk body (same floor)

Kiosk IDs are unique site-wide and matched case-insensitively.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass

import cv2
import numpy as np

logger = logging.getLogger("fmc.kiosks")

DEFAULT_STAND_M = 0.6
KIOSK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


@dataclass
class Kiosk:
    kiosk_id: str
    floor: str
    x: float
    y: float
    facing_deg: float
    stand_m: float
    marker_id: int | None

    def start_pose(self) -> dict:
        """Where the user stands after scanning: in front of the screen, facing it."""
        f = math.radians(self.facing_deg)
        return {
            "floor": self.floor,
            "x": self.x + self.stand_m * math.sin(f),
            "y": self.y + self.stand_m * math.cos(f),
            "heading": (self.facing_deg + 180.0) % 360.0,
        }


def norm_id(kiosk_id) -> str:
    return str(kiosk_id or "").strip().upper()


def load_kiosks(site) -> list[Kiosk]:
    from fmc.floors import normalize_floor_id

    out: list[Kiosk] = []
    for f in site.raw.get("floors") or []:
        fid = normalize_floor_id(f.get("floor"))
        for k in f.get("kiosks") or []:
            try:
                mid = k.get("marker_id")
                out.append(Kiosk(
                    kiosk_id=norm_id(k["kiosk_id"]),
                    floor=fid,
                    x=float(k["x"]),
                    y=float(k["y"]),
                    facing_deg=float(k.get("facing_deg", 0.0)) % 360.0,
                    stand_m=float(k.get("stand_m", DEFAULT_STAND_M)),
                    marker_id=int(mid) if mid not in (None, "") else None,
                ))
            except (KeyError, TypeError, ValueError) as e:
                logger.warning("[Kiosk] bad kiosk entry on floor %s: %s (%s)", fid, k, e)
    return out


_disk_cache: dict[str, tuple[float, list[Kiosk]]] = {}


def current_kiosks(site) -> list[Kiosk]:
    """Kiosks from config.yaml ON DISK (cached on its mtime): kiosks saved in the editor
    count without restarting the API server, which holds the config it loaded at startup."""
    try:
        from fmc.config import load_site_config

        path = site.data_dir / "config.yaml"
        mtime = path.stat().st_mtime
        hit = _disk_cache.get(str(path))
        if hit is None or hit[0] != mtime:
            hit = (mtime, load_kiosks(load_site_config(site.site_id)))
            _disk_cache[str(path)] = hit
            logger.info("[Kiosk] registry: %s", [(k.kiosk_id, k.floor) for k in hit[1]] or "none")
        return hit[1]
    except Exception as e:  # unreadable mid-save etc.
        logger.debug("[Kiosk] disk registry read failed: %s", e)
        return load_kiosks(site)


def find_kiosk(site, kiosk_id) -> Kiosk | None:
    want = norm_id(kiosk_id)
    return next((k for k in current_kiosks(site) if k.kiosk_id == want), None)


# ------------------------------------------------------------------ QR codes

def decode_qr(image_bgr: np.ndarray) -> str | None:
    """Text of a QR code in the frame (server-side reader for browsers without
    BarcodeDetector). Tries the frame as is, then a 2x upscale for small codes."""
    if image_bgr is None or image_bgr.size == 0:
        return None
    det = cv2.QRCodeDetector()
    gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY) if image_bgr.ndim == 3 else image_bgr
    for img in (gray, cv2.resize(gray, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)):
        try:
            text, pts, _ = det.detectAndDecode(img)
        except cv2.error:
            text = ""
        if text:
            return text
    return None


def qr_svg(text: str, caption: str = "", sub: str = "", module_mm: float = 2.0) -> str:
    """Printable / on-screen QR as SVG (OpenCV's encoder, 4-module quiet zone)."""
    img = cv2.QRCodeEncoder.create().encode(text)  # 255 = white, 0 = black, 1 px / module
    n = img.shape[0]
    quiet = 4
    size = (n + 2 * quiet) * module_mm
    text_h = 16.0 if caption or sub else 0.0
    rects = "".join(
        f'<rect x="{(quiet + c) * module_mm:.2f}" y="{(quiet + r) * module_mm:.2f}" '
        f'width="{module_mm + 0.02:.2f}" height="{module_mm + 0.02:.2f}"/>'
        for r in range(n) for c in range(n) if img[r, c] < 128
    )

    def esc(s: str) -> str:
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{size:.1f}mm" height="{size + text_h:.1f}mm" '
        f'viewBox="0 0 {size:.2f} {size + text_h:.2f}">',
        '<rect width="100%" height="100%" fill="#fff"/>',
        f'<g fill="#000">{rects}</g>',
    ]
    if caption:
        parts.append(
            f'<text x="{size / 2:.2f}" y="{size + 5:.2f}" font-family="sans-serif" font-size="5" '
            f'text-anchor="middle">{esc(caption)}</text>'
        )
    if sub:
        parts.append(
            f'<text x="{size / 2:.2f}" y="{size + 11:.2f}" font-family="sans-serif" font-size="3" '
            f'fill="#555" text-anchor="middle">{esc(sub)}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)
