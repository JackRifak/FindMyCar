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
import re
import time
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


# Each finalize writes a NEW database file (map_h2gis_<ms>) that nobody is reading — no
# lock fight with the viewer/PnP, and a compact file instead of an ever-growing one — then
# flips this pointer to it. Readers resolve the current file through db_file_for_site().
_CURRENT_PTR = "map_h2gis.current"
_DB_NAME_RE = re.compile(r"^map_h2gis(_\d+)?$")


def db_file_for_site(site: SiteConfig) -> Path:
    """Current H2 file path without extension (H2 appends .mv.db)."""
    try:
        name = (site.index_dir / _CURRENT_PTR).read_text(encoding="utf-8").strip()
        if _DB_NAME_RE.match(name) and Path(str(site.index_dir / name) + ".mv.db").exists():
            return site.index_dir / name
    except OSError:
        pass
    return site.index_dir / "map_h2gis"


def _set_current_db(site: SiteConfig, stem: Path) -> None:
    ptr = site.index_dir / _CURRENT_PTR
    tmp = site.index_dir / (_CURRENT_PTR + ".tmp")
    tmp.write_text(stem.name, encoding="utf-8")
    os.replace(tmp, ptr)  # atomic: readers see the old or the new name, never half


def _cleanup_old_dbs(site: SiteConfig, keep: str) -> None:
    """Delete superseded generated DBs (map_h2gis_<ms>.*). Files still open are retried on
    the next finalize. The legacy map_h2gis.* file is never touched here."""
    for p in site.index_dir.glob("map_h2gis_*"):
        stem = p.name.split(".")[0]
        if stem == keep or not _DB_NAME_RE.match(stem):
            continue
        if not p.name.endswith((".mv.db", ".trace.db", ".lock.db", ".db")):
            continue
        try:
            p.unlink()
        except OSError as e:
            logger.info("[H2GIS] old DB %s still in use — will retry next finalize (%s)", p.name, e)


def jdbc_url(db_file: Path) -> str:
    # forward slashes required on Windows JDBC paths
    p = db_file.resolve().as_posix()
    return (
        f"jdbc:h2:file:{p};"
        "AUTO_SERVER=TRUE;DB_CLOSE_DELAY=0;DB_CLOSE_ON_EXIT=TRUE;MODE=PostgreSQL;"
        # finalize rewrites the whole table while readers (viewer, PnP reload) may be mid-
        # SELECT; H2's default 1 s made that DELETE time out. 10 s covers a read without
        # letting requests stall for a minute if something does hold a lock.
        "LOCK_TIMEOUT=10000"
    )


# A failed/partial rewrite must not leave readers on the old map while map_landmarks.npz
# already holds the new one. The marker exists from the start of a rewrite until it commits;
# while it exists load_landmarks() returns None so every caller falls back to the NPZ.
def _stale_marker(site: SiteConfig) -> Path:
    return site.index_dir / "map_h2gis.stale"


def is_stale(site: SiteConfig) -> bool:
    return _stale_marker(site).exists()


def connect(db_file: Path):
    import jaydebeapi

    url = jdbc_url(db_file)
    user = os.environ.get("FMC_H2_USER", "sa")
    password = os.environ.get("FMC_H2_PASSWORD", "")
    conn = jaydebeapi.connect(DRIVER, url, [user, password], _jars())
    _ensure_spatial(conn)
    return conn


_SPATIAL_READY: set[str] = set()  # JDBC URLs whose DB already has the H2GIS functions


def _ensure_spatial(conn) -> None:
    """Register H2GIS spatial functions — once, only if missing.

    H2GIS_SPATIAL() DROPs and re-CREATEs hundreds of function aliases. Calling it on every
    connection took an exclusive lock on the SYS table each time; with concurrent requests
    they queued behind each other (floors / point cloud requests hung) and the churn grew
    the .mv.db file from ~25 MB to many GB.
    """
    try:
        url = str(conn.jconn.getMetaData().getURL())
    except Exception:
        url = ""
    if url and url in _SPATIAL_READY:
        return
    cur = conn.cursor()
    try:
        try:
            cur.execute(
                "SELECT COUNT(*) FROM INFORMATION_SCHEMA.ROUTINES "
                "WHERE UPPER(ROUTINE_NAME) = 'ST_GEOMFROMTEXT'"
            )
            row = cur.fetchone()
            have = bool(row and int(row[0]) > 0)
        except Exception:
            have = False  # can't tell → register (old behaviour)
        if not have:
            cur.execute(
                'CREATE ALIAS IF NOT EXISTS H2GIS_SPATIAL FOR '
                '"org.h2gis.functions.factory.H2GISFunctions.load"'
            )
            cur.execute("CALL H2GIS_SPATIAL()")
        if url:
            _SPATIAL_READY.add(url)
    finally:
        cur.close()


def _column_type(conn, table: str, column: str) -> str:
    """Upper-case DATA_TYPE of a column ('' if unknown)."""
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS
            WHERE UPPER(TABLE_NAME) = UPPER(?) AND UPPER(COLUMN_NAME) = UPPER(?)
            """,
            (table, column),
        )
        row = cur.fetchone()
        return str(row[0]).upper() if row and row[0] is not None else ""
    except Exception:
        return ""
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
        elif "CHAR" not in _column_type(conn, "map_landmarks", "floor_id"):
            # one-time widen of old INT floor ids → VARCHAR for label floors (G, B1, …).
            # Only when needed: this DDL takes an exclusive lock (and can rebuild the
            # table) — run on every read it blocked finalize's rewrite → lock timeout.
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

    if colors is None:
        colors = np.tile(np.array([52, 199, 89], dtype=np.uint8), (len(positions), 1))
    rows = []
    for i in range(len(positions)):
        x, y, z = (float(positions[i, 0]), float(positions[i, 1]), float(positions[i, 2]))
        rows.append((
            # descriptor stays numpy here — Java byte[] needs the JVM, started by connect()
            int(ids[i]), x, y, z, f"POINT Z ({x} {y} {z})", descriptors[i],
            int(colors[i, 0]), int(colors[i, 1]), int(colors[i, 2]),
            normalize_floor_id(floor_labels[i]),
        ))

    # fresh file → nobody else has it open → no lock contention; readers switch over only
    # once it's complete (a failure leaves them on the previous, intact DB)
    new_stem = site.index_dir / f"map_h2gis_{int(time.time() * 1000)}"
    _write_landmark_rows(new_stem, rows, floor_labels, aligned)
    _set_current_db(site, new_stem)
    _stale_marker(site).unlink(missing_ok=True)
    _cleanup_old_dbs(site, keep=new_stem.name)
    db_file = new_stem

    logger.info(
        "[H2GIS] Stored %s landmarks in %s (aligned=%s floors=%s)",
        len(positions), db_file, aligned, sorted(set(floor_labels)),
    )
    return db_file


_INSERT_SQL = """
    INSERT INTO map_landmarks
    (landmark_id, x, y, z, the_geom, descriptor, color_r, color_g, color_b, floor_id)
    VALUES (?, ?, ?, ?, ST_GeomFromText(?, 0), ?, ?, ?, ?, ?)
"""
_INSERT_CHUNK = 2000


def _write_landmark_rows(db_file: Path, rows: list, floor_labels, aligned: bool) -> None:
    """DELETE + batched INSERT in one transaction (batches keep the table lock short)."""
    conn = connect(db_file)
    try:
        conn.jconn.setAutoCommit(False)
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
                ("count", str(len(rows))),
            )
            floor_set = sorted(set(floor_labels), key=str)
            cur.execute(
                "INSERT INTO map_meta(k, v) VALUES (?, ?)",
                ("floors", ",".join(floor_set)),
            )

            for k in range(0, len(rows), _INSERT_CHUNK):
                batch = [r[:5] + (_as_jbytes(r[5]),) + r[6:] for r in rows[k:k + _INSERT_CHUNK]]
                cur.executemany(_INSERT_SQL, batch)
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            cur.close()
    finally:
        conn.close()


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
    ignore_stale: bool = False,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray] | None:
    """Load landmarks as (positions Nx3, descriptors NxD, ids N, floors N).

    If floor is set, only that floor's landmarks are returned (prevents
    cross-floor PnP false positives). If center_xy is set, also apply
    spatial radius filter.

    ignore_stale: read the DB even if the last rewrite didn't complete (the 3D viewer
    shows what H2GIS actually holds; PnP/mapper fall back to the NPZ instead).
    """
    from fmc.floors import normalize_floor_id

    db_file = db_file_for_site(site)
    if is_stale(site) and not ignore_stale:
        logger.warning("[H2GIS] last rewrite did not complete — using map_landmarks.npz instead")
        return None
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
    """Remove H2 database files for a site (current, generated and legacy) + pointer."""
    stems = {db_file_for_site(site).name, "map_h2gis"}
    stems |= {p.name.split(".")[0] for p in site.index_dir.glob("map_h2gis_*")
              if _DB_NAME_RE.match(p.name.split(".")[0])}
    for name in stems:
        for suffix in (".mv.db", ".db", ".trace.db", ".lock.db"):
            p = site.index_dir / (name + suffix)
            if p.exists():
                try:
                    p.unlink()
                except OSError as e:
                    logger.warning("[H2GIS] Could not remove %s: %s", p, e)
    for extra in (_CURRENT_PTR, _CURRENT_PTR + ".tmp", "map_h2gis.stale"):
        (site.index_dir / extra).unlink(missing_ok=True)


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
