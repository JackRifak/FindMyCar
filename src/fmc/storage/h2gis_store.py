"""H2GIS spatial store for mapped 3D landmarks (PnP localization).

Uses the H2GIS JDBC driver (Java) via JayDeBeApi. Landmarks are stored as
POINT Z geometries with ORB descriptors so we can:
  - load the full map for 2D-to-3D matching
  - spatially filter landmarks near a coarse VPR fix (ST_DWithin)
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

import numpy as np

from fmc.config import PROJECT_ROOT, SiteConfig

logger = logging.getLogger("fmc.storage.h2gis")

H2GIS_BIN = PROJECT_ROOT / "lib" / "h2gis" / "h2gis-standalone" / "bin"
DRIVER = "org.h2.Driver"


def _jars() -> list[str]:
    if not H2GIS_BIN.is_dir():
        raise FileNotFoundError(
            f"H2GIS jars missing at {H2GIS_BIN}. "
            "Download h2gis-standalone-bin.zip (v2.2.5) into lib/ and extract."
        )
    jars = sorted(str(p) for p in H2GIS_BIN.glob("*.jar"))
    if not jars:
        raise FileNotFoundError(f"No .jar files under {H2GIS_BIN}")
    return jars


def db_file_for_site(site: SiteConfig) -> Path:
    """H2 file path without extension (H2 appends .mv.db)."""
    return site.index_dir / "map_h2gis"


def jdbc_url(db_file: Path) -> str:
    # forward slashes required on Windows JDBC paths
    p = db_file.resolve().as_posix()
    return (
        f"jdbc:h2:file:{p};"
        "AUTO_SERVER=TRUE;DB_CLOSE_DELAY=0;DB_CLOSE_ON_EXIT=TRUE;MODE=PostgreSQL"
    )


def connect(db_file: Path):
    import jaydebeapi

    url = jdbc_url(db_file)
    user = os.environ.get("FMC_H2_USER", "sa")
    password = os.environ.get("FMC_H2_PASSWORD", "")
    conn = jaydebeapi.connect(DRIVER, url, [user, password], _jars())
    _ensure_spatial(conn)
    return conn


def _ensure_spatial(conn) -> None:
    cur = conn.cursor()
    try:
        cur.execute(
            'CREATE ALIAS IF NOT EXISTS H2GIS_SPATIAL FOR '
            '"org.h2gis.functions.factory.H2GISFunctions.load"'
        )
        cur.execute("CALL H2GIS_SPATIAL()")
    finally:
        cur.close()


def _ensure_schema(conn) -> None:
    cur = conn.cursor()
    try:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS map_meta (
                k VARCHAR(64) PRIMARY KEY,
                v VARCHAR(512)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS map_landmarks (
                landmark_id INT PRIMARY KEY,
                x DOUBLE NOT NULL,
                y DOUBLE NOT NULL,
                z DOUBLE NOT NULL,
                the_geom GEOMETRY,
                descriptor VARBINARY(256) NOT NULL,
                color_r INT DEFAULT 52,
                color_g INT DEFAULT 199,
                color_b INT DEFAULT 89
            )
            """
        )
        # spatial index (ignore if already exists)
        try:
            cur.execute(
                "CREATE SPATIAL INDEX IF NOT EXISTS map_landmarks_spidx "
                "ON map_landmarks(the_geom)"
            )
        except Exception:
            # older H2GIS may not support IF NOT EXISTS on spatial index
            try:
                cur.execute(
                    "CREATE SPATIAL INDEX map_landmarks_spidx ON map_landmarks(the_geom)"
                )
            except Exception:
                pass
    finally:
        cur.close()


def _as_jbytes(arr: np.ndarray):
    """Pack uint8 descriptor into a Java byte[] for JDBC VARBINARY."""
    import jpype

    u8 = np.asarray(arr, dtype=np.uint8).ravel()
    # JByte is signed; bit pattern preserved via int8 view
    return jpype.JArray(jpype.JByte)(u8.view(np.int8).tolist())


def _from_jbytes(blob) -> np.ndarray:
    """Unpack JDBC VARBINARY / Java byte[] to uint8 numpy."""
    if blob is None:
        return np.zeros(0, dtype=np.uint8)
    if isinstance(blob, (bytes, bytearray, memoryview)):
        return np.frombuffer(bytes(blob), dtype=np.uint8).copy()
    # java byte[]
    return np.array([int(b) & 0xFF for b in blob], dtype=np.uint8)


def replace_landmarks(
    site: SiteConfig,
    positions: np.ndarray,
    descriptors: np.ndarray,
    ids: np.ndarray,
    colors: Optional[np.ndarray] = None,
    aligned: bool = False,
) -> Path:
    """Wipe and rewrite the landmark table for a site. Returns DB file stem path."""
    if len(positions) == 0:
        raise ValueError("no landmarks to store")
    if len(positions) != len(descriptors) or len(positions) != len(ids):
        raise ValueError("positions/descriptors/ids length mismatch")

    db_file = db_file_for_site(site)
    db_file.parent.mkdir(parents=True, exist_ok=True)

    conn = connect(db_file)
    try:
        _ensure_schema(conn)
        cur = conn.cursor()
        try:
            cur.execute("DELETE FROM map_landmarks")
            cur.execute("DELETE FROM map_meta")
            cur.execute(
                "INSERT INTO map_meta(k, v) VALUES (?, ?)",
                ("aligned", "1" if aligned else "0"),
            )
            cur.execute(
                "INSERT INTO map_meta(k, v) VALUES (?, ?)",
                ("frame", "facility_xy_height"),
            )
            cur.execute(
                "INSERT INTO map_meta(k, v) VALUES (?, ?)",
                ("count", str(len(positions))),
            )

            if colors is None:
                colors = np.tile(np.array([52, 199, 89], dtype=np.uint8), (len(positions), 1))

            for i in range(len(positions)):
                x, y, z = (float(positions[i, 0]), float(positions[i, 1]), float(positions[i, 2]))
                wkt = f"POINT Z ({x} {y} {z})"
                desc = _as_jbytes(descriptors[i])
                cr, cg, cb = int(colors[i, 0]), int(colors[i, 1]), int(colors[i, 2])
                cur.execute(
                    """
                    INSERT INTO map_landmarks
                    (landmark_id, x, y, z, the_geom, descriptor, color_r, color_g, color_b)
                    VALUES (?, ?, ?, ?, ST_GeomFromText(?, 0), ?, ?, ?, ?)
                    """,
                    (int(ids[i]), x, y, z, wkt, desc, cr, cg, cb),
                )
            conn.commit()
        finally:
            cur.close()
    finally:
        conn.close()

    logger.info(
        "[H2GIS] Stored %s landmarks in %s (aligned=%s)",
        len(positions), db_file, aligned,
    )
    return db_file


def load_landmarks(
    site: SiteConfig,
    center_xy: Optional[tuple[float, float]] = None,
    radius_m: float = 25.0,
) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
    """Load landmarks as (positions Nx3, descriptors NxD, ids N).

    If center_xy is set, only return landmarks within radius_m (spatial filter).
    """
    db_file = db_file_for_site(site)
    mv = Path(str(db_file) + ".mv.db")
    if not mv.exists() and not Path(str(db_file) + ".db").exists():
        # also accept bare file created by some H2 versions
        if not db_file.exists():
            return None

    conn = connect(db_file)
    try:
        _ensure_schema(conn)
        cur = conn.cursor()
        try:
            if center_xy is not None:
                cx, cy = float(center_xy[0]), float(center_xy[1])
                # ST_DWithin on 2D projection of POINT Z
                cur.execute(
                    """
                    SELECT landmark_id, x, y, z, descriptor
                    FROM map_landmarks
                    WHERE ST_DWithin(
                        ST_Force2D(the_geom),
                        ST_GeomFromText(?, 0),
                        ?
                    )
                    """,
                    (f"POINT({cx} {cy})", float(radius_m)),
                )
            else:
                cur.execute(
                    "SELECT landmark_id, x, y, z, descriptor FROM map_landmarks"
                )
            rows = cur.fetchall()
        finally:
            cur.close()
    finally:
        conn.close()

    if not rows:
        return None

    ids = np.array([int(r[0]) for r in rows], dtype=np.int32)
    positions = np.array([[float(r[1]), float(r[2]), float(r[3])] for r in rows], dtype=np.float32)
    descs = [_from_jbytes(r[4]) for r in rows]
    dlen = max((len(d) for d in descs), default=0)
    descriptors = np.zeros((len(descs), dlen), dtype=np.uint8)
    for i, d in enumerate(descs):
        descriptors[i, : len(d)] = d

    logger.info("[H2GIS] Loaded %s landmarks from %s", len(ids), db_file)
    return positions, descriptors, ids


def delete_site_db(site: SiteConfig) -> None:
    """Remove H2 database files for a site."""
    stem = db_file_for_site(site)
    for suffix in (".mv.db", ".db", ".trace.db", ".lock.db"):
        p = Path(str(stem) + suffix)
        if p.exists():
            try:
                p.unlink()
            except OSError as e:
                logger.warning("[H2GIS] Could not remove %s: %s", p, e)
