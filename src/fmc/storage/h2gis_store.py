"""H2GIS spatial store for mapped 3D landmarks (PnP localization).

Uses the H2GIS JDBC driver (Java) via JayDeBeApi. Landmarks are stored as
POINT Z geometries with ORB descriptors so we can:
  - load the full map for 2D-to-3D matching
  - filter by floor_id (multi-floor parking, no cross-floor false positives)
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


def _column_exists(conn, table: str, column: str) -> bool:
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
            WHERE UPPER(TABLE_NAME) = UPPER(?) AND UPPER(COLUMN_NAME) = UPPER(?)
            """,
            (table, column),
        )
        row = cur.fetchone()
        return bool(row and int(row[0]) > 0)
    except Exception:
        return False
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
                color_b INT DEFAULT 89,
                floor_id VARCHAR(32) DEFAULT '1' NOT NULL
            )
            """
        )
        # migrate older DBs that lack floor_id / still use INT
        if not _column_exists(conn, "map_landmarks", "floor_id"):
            try:
                cur.execute(
                    "ALTER TABLE map_landmarks ADD COLUMN floor_id VARCHAR(32) DEFAULT '1' NOT NULL"
                )
                logger.info("[H2GIS] Migrated map_landmarks: added floor_id (VARCHAR)")
            except Exception as e:
                logger.warning("[H2GIS] floor_id migrate skipped: %s", e)
        else:
            # best-effort widen INT → VARCHAR for label floors (G, B1, …)
            try:
                cur.execute(
                    "ALTER TABLE map_landmarks ALTER COLUMN floor_id VARCHAR(32)"
                )
            except Exception:
                pass

        try:
            cur.execute(
                "CREATE SPATIAL INDEX IF NOT EXISTS map_landmarks_spidx "
                "ON map_landmarks(the_geom)"
            )
        except Exception:
            try:
                cur.execute(
                    "CREATE SPATIAL INDEX map_landmarks_spidx ON map_landmarks(the_geom)"
                )
            except Exception:
                pass

        try:
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_floor_geom "
                "ON map_landmarks(floor_id)"
            )
        except Exception:
            try:
                cur.execute("CREATE INDEX idx_floor_geom ON map_landmarks(floor_id)")
            except Exception:
                pass
    finally:
        cur.close()


def _as_jbytes(arr: np.ndarray):
    """Pack uint8 descriptor into a Java byte[] for JDBC VARBINARY."""
    import jpype

    u8 = np.asarray(arr, dtype=np.uint8).ravel()
    return jpype.JArray(jpype.JByte)(u8.view(np.int8).tolist())


def _from_jbytes(blob) -> np.ndarray:
    """Unpack JDBC VARBINARY / Java byte[] to uint8 numpy."""
    if blob is None:
        return np.zeros(0, dtype=np.uint8)
    if isinstance(blob, (bytes, bytearray, memoryview)):
        return np.frombuffer(bytes(blob), dtype=np.uint8).copy()
    return np.array([int(b) & 0xFF for b in blob], dtype=np.uint8)


def replace_landmarks(
    site: SiteConfig,
    positions: np.ndarray,
    descriptors: np.ndarray,
    ids: np.ndarray,
    colors: Optional[np.ndarray] = None,
    floors: Optional[np.ndarray] = None,
    aligned: bool = False,
) -> Path:
    """Wipe and rewrite the landmark table for a site. Returns DB file stem path."""
    if len(positions) == 0:
        raise ValueError("no landmarks to store")
    if len(positions) != len(descriptors) or len(positions) != len(ids):
        raise ValueError("positions/descriptors/ids length mismatch")

    db_file = db_file_for_site(site)
    db_file.parent.mkdir(parents=True, exist_ok=True)

    from fmc.floors import coerce_floor_array, normalize_floor_id

    if floors is None:
        floor_labels = ["1"] * len(positions)
    else:
        floor_labels = coerce_floor_array(floors, n=len(positions))

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
            floor_set = sorted(set(floor_labels), key=str)
            cur.execute(
                "INSERT INTO map_meta(k, v) VALUES (?, ?)",
                ("floors", ",".join(floor_set)),
            )

            if colors is None:
                colors = np.tile(np.array([52, 199, 89], dtype=np.uint8), (len(positions), 1))

            for i in range(len(positions)):
                x, y, z = (float(positions[i, 0]), float(positions[i, 1]), float(positions[i, 2]))
                wkt = f"POINT Z ({x} {y} {z})"
                desc = _as_jbytes(descriptors[i])
                cr, cg, cb = int(colors[i, 0]), int(colors[i, 1]), int(colors[i, 2])
                fid = normalize_floor_id(floor_labels[i])
                cur.execute(
                    """
                    INSERT INTO map_landmarks
                    (landmark_id, x, y, z, the_geom, descriptor, color_r, color_g, color_b, floor_id)
                    VALUES (?, ?, ?, ?, ST_GeomFromText(?, 0), ?, ?, ?, ?, ?)
                    """,
                    (int(ids[i]), x, y, z, wkt, desc, cr, cg, cb, fid),
                )
            conn.commit()
        finally:
            cur.close()
    finally:
        conn.close()

    logger.info(
        "[H2GIS] Stored %s landmarks in %s (aligned=%s floors=%s)",
        len(positions), db_file, aligned, sorted(set(floor_labels)),
    )
    return db_file


def list_floors(site: SiteConfig) -> list[str]:
    """Distinct floor_id labels present in the landmark DB."""
    from fmc.floors import normalize_floor_id

    db_file = db_file_for_site(site)
    mv = Path(str(db_file) + ".mv.db")
    if not mv.exists() and not Path(str(db_file) + ".db").exists() and not db_file.exists():
        return []

    conn = connect(db_file)
    try:
        _ensure_schema(conn)
        cur = conn.cursor()
        try:
            cur.execute("SELECT DISTINCT floor_id FROM map_landmarks ORDER BY floor_id")
            rows = cur.fetchall()
            return [normalize_floor_id(r[0]) for r in rows]
        finally:
            cur.close()
    finally:
        conn.close()


def retag_floor(site: SiteConfig, old_floor, new_floor) -> int:
    """Rewrite floor_id labels in the landmark DB. Returns rows updated."""
    from fmc.floors import normalize_floor_id

    old_id = normalize_floor_id(old_floor)
    new_id = normalize_floor_id(new_floor)
    if old_id == new_id:
        return 0

    db_file = db_file_for_site(site)
    mv = Path(str(db_file) + ".mv.db")
    if not mv.exists() and not Path(str(db_file) + ".db").exists() and not db_file.exists():
        return 0

    conn = connect(db_file)
    try:
        _ensure_schema(conn)
        cur = conn.cursor()
        try:
            cur.execute(
                "UPDATE map_landmarks SET floor_id = ? WHERE CAST(floor_id AS VARCHAR) = ?",
                (new_id, old_id),
            )
            n = cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else 0
            conn.commit()
            return int(n)
        finally:
            cur.close()
    finally:
        conn.close()


def load_landmarks(
    site: SiteConfig,
    floor: Optional[str] = None,
    center_xy: Optional[tuple[float, float]] = None,
    radius_m: float = 25.0,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray] | None:
    """Load landmarks as (positions Nx3, descriptors NxD, ids N, floors N).

    If floor is set, only that floor's landmarks are returned (prevents
    cross-floor PnP false positives). If center_xy is set, also apply
    spatial radius filter.
    """
    from fmc.floors import normalize_floor_id

    db_file = db_file_for_site(site)
    mv = Path(str(db_file) + ".mv.db")
    if not mv.exists() and not Path(str(db_file) + ".db").exists():
        if not db_file.exists():
            return None

    conn = connect(db_file)
    try:
        _ensure_schema(conn)
        cur = conn.cursor()
        try:
            clauses = []
            params: list = []
            if floor is not None:
                clauses.append("CAST(floor_id AS VARCHAR) = ?")
                params.append(normalize_floor_id(floor))
            if center_xy is not None:
                cx, cy = float(center_xy[0]), float(center_xy[1])
                clauses.append(
                    "ST_DWithin(ST_Force2D(the_geom), ST_GeomFromText(?, 0), ?)"
                )
                params.extend([f"POINT({cx} {cy})", float(radius_m)])

            where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
            sql = (
                "SELECT landmark_id, x, y, z, descriptor, floor_id "
                f"FROM map_landmarks{where}"
            )
            if params:
                cur.execute(sql, tuple(params))
            else:
                cur.execute(sql)
            rows = cur.fetchall()
        finally:
            cur.close()
    finally:
        conn.close()

    if not rows:
        return None

    ids = np.array([int(r[0]) for r in rows], dtype=np.int32)
    positions = np.array([[float(r[1]), float(r[2]), float(r[3])] for r in rows], dtype=np.float32)
    floors = np.array(
        [normalize_floor_id(r[5] if r[5] is not None else "1") for r in rows],
        dtype=object,
    )
    descs = [_from_jbytes(r[4]) for r in rows]
    dlen = max((len(d) for d in descs), default=0)
    descriptors = np.zeros((len(descs), dlen), dtype=np.uint8)
    for i, d in enumerate(descs):
        descriptors[i, : len(d)] = d

    logger.info(
        "[H2GIS] Loaded %s landmarks from %s (floor=%s)",
        len(ids), db_file, floor if floor is not None else "all",
    )
    return positions, descriptors, ids, floors


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


def delete_landmarks_where(
    site: SiteConfig,
    *,
    floor: Optional[str] = None,
    ids: Optional[list[int]] = None,
    x_min: Optional[float] = None,
    x_max: Optional[float] = None,
    y_min: Optional[float] = None,
    y_max: Optional[float] = None,
    z_min: Optional[float] = None,
    z_max: Optional[float] = None,
) -> int:
    """Delete matching landmarks from H2GIS. Returns number of deleted rows."""
    from fmc.floors import normalize_floor_id

    if (
        floor is None
        and not ids
        and x_min is None and x_max is None
        and y_min is None and y_max is None
        and z_min is None and z_max is None
    ):
        raise ValueError("refuse full wipe — pass floor, ids, and/or bbox filters")

    db_file = db_file_for_site(site)
    mv = Path(str(db_file) + ".mv.db")
    if not mv.exists() and not Path(str(db_file) + ".db").exists() and not db_file.exists():
        return 0

    clauses: list[str] = []
    params: list = []
    if floor is not None:
        clauses.append("CAST(floor_id AS VARCHAR) = ?")
        params.append(normalize_floor_id(floor))
    if ids:
        placeholders = ",".join("?" for _ in ids)
        clauses.append(f"landmark_id IN ({placeholders})")
        params.extend(int(i) for i in ids)
    if x_min is not None:
        clauses.append("x >= ?")
        params.append(float(x_min))
    if x_max is not None:
        clauses.append("x <= ?")
        params.append(float(x_max))
    if y_min is not None:
        clauses.append("y >= ?")
        params.append(float(y_min))
    if y_max is not None:
        clauses.append("y <= ?")
        params.append(float(y_max))
    if z_min is not None:
        clauses.append("z >= ?")
        params.append(float(z_min))
    if z_max is not None:
        clauses.append("z <= ?")
        params.append(float(z_max))

    where = " AND ".join(clauses)
    conn = connect(db_file)
    try:
        _ensure_schema(conn)
        cur = conn.cursor()
        try:
            cur.execute(f"SELECT COUNT(*) FROM map_landmarks WHERE {where}", tuple(params))
            n = int(cur.fetchone()[0] or 0)
            if n:
                cur.execute(f"DELETE FROM map_landmarks WHERE {where}", tuple(params))
                conn.commit()
            logger.info("[H2GIS] Deleted %s landmarks (%s)", n, where)
            return n
        finally:
            cur.close()
    finally:
        conn.close()
