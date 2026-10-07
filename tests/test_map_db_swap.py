"""Map DB write-new-then-swap, orphan tag re-attach, and unaligned-walk exclusion.

No JVM: the H2 write itself is faked; everything runs in a temp dir.
"""
from types import SimpleNamespace

import numpy as np

from fmc.storage import h2gis_store as hs


def _site(tmp_path):
    return SimpleNamespace(index_dir=tmp_path)


def _fake_writer(calls):
    def write(stem, rows, floor_labels, aligned):
        calls.append((stem.name, len(rows)))
        (stem.parent / (stem.name + ".mv.db")).write_bytes(b"x")
    return write


def test_replace_writes_new_file_and_swaps_pointer(tmp_path, monkeypatch):
    site = _site(tmp_path)
    calls = []
    monkeypatch.setattr(hs, "_write_landmark_rows", _fake_writer(calls))
    (tmp_path / "map_h2gis.mv.db").write_bytes(b"legacy")  # old (bloated) DB
    (tmp_path / "map_h2gis.stale").write_text("x")
    assert hs.db_file_for_site(site).name == "map_h2gis"

    n = 5
    hs.replace_landmarks(site, np.zeros((n, 3)), np.zeros((n, 32), np.uint8), np.arange(n))
    first = hs.db_file_for_site(site).name
    assert first.startswith("map_h2gis_") and calls == [(first, n)]
    assert not (tmp_path / "map_h2gis.stale").exists()  # success clears the marker
    assert (tmp_path / "map_h2gis.mv.db").exists()  # legacy file left for the user

    import time
    time.sleep(0.01)
    hs.replace_landmarks(site, np.zeros((n, 3)), np.zeros((n, 32), np.uint8), np.arange(n))
    second = hs.db_file_for_site(site).name
    assert second != first
    assert not (tmp_path / (first + ".mv.db")).exists()  # superseded generated DB removed


def test_failed_write_keeps_readers_on_previous_db(tmp_path, monkeypatch):
    site = _site(tmp_path)
    (tmp_path / "map_h2gis.mv.db").write_bytes(b"old")

    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(hs, "_write_landmark_rows", boom)
    try:
        hs.replace_landmarks(site, np.zeros((2, 3)), np.zeros((2, 32), np.uint8), np.arange(2))
    except RuntimeError:
        pass
    assert hs.db_file_for_site(site).name == "map_h2gis"  # pointer never moved


def test_pointer_to_missing_file_falls_back(tmp_path):
    site = _site(tmp_path)
    (tmp_path / "map_h2gis.current").write_text("map_h2gis_123")
    assert hs.db_file_for_site(site).name == "map_h2gis"
    (tmp_path / "map_h2gis.current").write_text("../evil")
    assert hs.db_file_for_site(site).name == "map_h2gis"


def _mapper():
    from fmc.mapping.continuous_mapper import ContinuousMapper
    return ContinuousMapper()


def test_orphan_tag_joins_session_of_nearest_keyframe():
    from fmc.mapping.continuous_mapper import COMMITTED_SESSION
    m = _mapper()
    m._aligned_sessions = {COMMITTED_SESSION}
    # tag dropped in session 0 before the walk's first keyframe opened session 1
    m.user_tags = [SimpleNamespace(timestamp=100.0, session_id=0, facility_x=1, facility_y=2),
                   SimpleNamespace(timestamp=180.0, session_id=1, facility_x=3, facility_y=4)]
    m.keyframes = [SimpleNamespace(timestamp=t, session_id=1) for t in (110.0, 140.0, 175.0)]
    m._reattach_orphan_tags()
    assert [t.session_id for t in m.user_tags] == [1, 1]  # session 1 now has its 2 tags


def test_unaligned_walk_not_exported():
    from fmc.mapping.continuous_mapper import COMMITTED_SESSION
    m = _mapper()
    m._aligned_sessions = {COMMITTED_SESSION, 2}
    m.landmarks = {
        1: SimpleNamespace(session_id=COMMITTED_SESSION),
        2: SimpleNamespace(session_id=2),
        3: SimpleNamespace(session_id=3),  # walk never aligned (1 tag)
    }
    assert m._unaligned_landmark_ids() == {3}
    m._aligned_sessions = set()  # nothing aligned at all → legacy local-frame map, export all
    assert m._unaligned_landmark_ids() == set()
