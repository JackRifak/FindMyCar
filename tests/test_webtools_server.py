"""Integration tests for fmc.webtools.server's core golden path: create a
site, upload a floor plan, fit a transform, save walkable geometry, and
read it back correctly converted to pixel coordinates. Query/route
endpoints aren't covered here since they need a fully ingested dataset --
they're thin wrappers over fmc.vpr.pipeline / fmc.navigation.routing, which
have their own tests.
"""
from __future__ import annotations

import io
from pathlib import Path

import fitz
import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

import fmc.webtools.server as server_module
from fmc.webtools.server import app


@pytest.fixture()
def client(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(server_module, "DATA_ROOT", tmp_path)
    import fmc.config as config_module
    monkeypatch.setattr(config_module, "DATA_ROOT", tmp_path)
    return TestClient(app)


def _tiny_pdf_bytes() -> bytes:
    doc = fitz.open()
    page = doc.new_page(width=200, height=150)
    page.draw_line((10, 10), (190, 10))  # something to render, not that it matters here
    return doc.tobytes()


def _tiny_jpeg_bytes(color=(200, 50, 50)) -> bytes:
    img = Image.new("RGB", (64, 48), color=color)
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


def _calibrated_site(client: TestClient, site_id: str = "site_00"):
    """Create a site with a saved transform: 1 pixel = 0.05m, no rotation."""
    client.post(f"/api/sites/{site_id}")
    points = [
        {"px": 100, "py": 100, "x": 0.0, "y": 0.0},
        {"px": 300, "py": 100, "x": 10.0, "y": 0.0},
        {"px": 100, "py": 300, "x": 0.0, "y": 10.0},
    ]
    client.put(f"/api/sites/{site_id}/control-points", json=points)
    client.post(f"/api/sites/{site_id}/control-points/fit/save")


def test_create_site_and_list(client: TestClient):
    assert client.get("/api/sites").json() == []

    res = client.post("/api/sites/site_00")
    assert res.status_code == 200
    assert res.json()["floors"] == [1]

    assert client.get("/api/sites").json() == ["site_00"]

    # duplicate create is rejected
    assert client.post("/api/sites/site_00").status_code == 409


def test_unknown_site_is_404(client: TestClient):
    assert client.get("/api/sites/nope/transform").status_code == 404
    assert client.get("/api/sites/nope/control-points").status_code == 404


def test_floorplan_upload_and_serve(client: TestClient):
    client.post("/api/sites/site_00")
    res = client.post(
        "/api/sites/site_00/floorplan",
        params={"page": 0, "dpi": 72},
        files={"file": ("plan.pdf", _tiny_pdf_bytes(), "application/pdf")},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["width"] == 200
    assert body["height"] == 150

    img_res = client.get("/api/sites/site_00/floorplan/image")
    assert img_res.status_code == 200
    assert img_res.headers["content-type"] == "image/png"


def test_control_points_fit_and_save_flow(client: TestClient):
    client.post("/api/sites/site_00")

    points = [
        {"px": 100, "py": 100, "x": 0.0, "y": 0.0},
        {"px": 300, "py": 100, "x": 10.0, "y": 0.0},
        {"px": 100, "py": 300, "x": 0.0, "y": 10.0},
    ]
    put_res = client.put("/api/sites/site_00/control-points", json=points)
    assert put_res.status_code == 200
    assert put_res.json()["saved"] == 3

    # Round-trips correctly
    assert client.get("/api/sites/site_00/control-points").json() == points

    # Fit preview does not persist a transform yet
    fit_res = client.post("/api/sites/site_00/control-points/fit")
    assert fit_res.status_code == 200
    fit_body = fit_res.json()
    assert fit_body["max_residual"] < 1e-6
    assert any("3 control points" in w for w in fit_body["warnings"])
    assert client.get("/api/sites/site_00/transform").status_code == 404

    # Saving persists it
    save_res = client.post("/api/sites/site_00/control-points/fit/save")
    assert save_res.status_code == 200
    assert client.get("/api/sites/site_00/transform").status_code == 200


def test_fit_requires_three_points(client: TestClient):
    client.post("/api/sites/site_00")
    client.put("/api/sites/site_00/control-points", json=[{"px": 0, "py": 0, "x": 0, "y": 0}])
    res = client.post("/api/sites/site_00/control-points/fit")
    assert res.status_code == 400


def test_segments_and_slots_round_trip_through_pixel_and_world(client: TestClient):
    client.post("/api/sites/site_00")
    points = [
        {"px": 100, "py": 100, "x": 0.0, "y": 0.0},
        {"px": 300, "py": 100, "x": 10.0, "y": 0.0},
        {"px": 100, "py": 300, "x": 0.0, "y": 10.0},
    ]
    client.put("/api/sites/site_00/control-points", json=points)
    client.post("/api/sites/site_00/control-points/fit/save")

    # 1 pixel = 0.05m per the fitted transform above; segment from (100,100)->(300,100)
    # should come out to world (0,0)->(10,0).
    segments = [{"px1": 100, "py1": 100, "px2": 300, "py2": 100}]
    res = client.put("/api/sites/site_00/floors/1/segments", json=segments)
    assert res.status_code == 200
    assert res.json()["saved"] == 1

    slots = [{"px": 100, "py": 300, "slot_id": "A01", "zone": "Zone_A"}]
    res = client.put("/api/sites/site_00/floors/1/slots", json=slots)
    assert res.status_code == 200

    geo = client.get("/api/sites/site_00/floors/1/geometry").json()
    seg = geo["segments"][0]
    assert seg["x1"] == pytest.approx(0.0, abs=1e-6)
    assert seg["y1"] == pytest.approx(0.0, abs=1e-6)
    assert seg["x2"] == pytest.approx(10.0, abs=1e-6)
    assert seg["y2"] == pytest.approx(0.0, abs=1e-6)
    # And pixel coords should round-trip back to what we saved
    assert seg["px1"] == pytest.approx(100.0, abs=1e-3)
    assert seg["px2"] == pytest.approx(300.0, abs=1e-3)

    slot = geo["slots"][0]
    assert slot["slot_id"] == "A01"
    assert slot["x"] == pytest.approx(0.0, abs=1e-6)
    assert slot["y"] == pytest.approx(10.0, abs=1e-6)
    assert slot["px"] == pytest.approx(100.0, abs=1e-3)
    assert slot["py"] == pytest.approx(300.0, abs=1e-3)


def test_segments_rejected_without_transform(client: TestClient):
    client.post("/api/sites/site_00")
    res = client.put("/api/sites/site_00/floors/1/segments", json=[{"px1": 0, "py1": 0, "px2": 1, "py2": 1}])
    assert res.status_code == 400


def test_create_floor(client: TestClient):
    client.post("/api/sites/site_00")
    assert client.get("/api/sites/site_00/floors").json() == [1]
    client.post("/api/sites/site_00/floors/2")
    assert client.get("/api/sites/site_00/floors").json() == [1, 2]


def test_capture_location_and_photo_lifecycle(client: TestClient):
    _calibrated_site(client)

    create_res = client.post(
        "/api/sites/site_00/floors/1/capture-locations",
        json={"location_id": "loc_01", "zone": "Zone_A", "px": 100, "py": 100},
    )
    assert create_res.status_code == 200
    body = create_res.json()
    assert body["x"] == pytest.approx(0.0, abs=1e-6)
    assert body["y"] == pytest.approx(0.0, abs=1e-6)
    assert body["photo_count"] == 0

    listing = client.get("/api/sites/site_00/floors/1/capture-locations").json()
    assert len(listing) == 1
    assert listing[0]["location_id"] == "loc_01"
    assert listing[0]["px"] == pytest.approx(100.0, abs=1e-3)

    # Upload two photos with different headings
    photo1 = client.post(
        "/api/sites/site_00/capture-locations/loc_01/photos",
        data={"heading_degrees": "90"},
        files={"file": ("p1.jpg", _tiny_jpeg_bytes(), "image/jpeg")},
    )
    assert photo1.status_code == 200
    photo1_body = photo1.json()
    assert photo1_body["heading_degrees"] == 90
    assert "direction_px" in photo1_body

    photo2 = client.post(
        "/api/sites/site_00/capture-locations/loc_01/photos",
        data={"heading_degrees": "270"},
        files={"file": ("p2.jpg", _tiny_jpeg_bytes((50, 50, 200)), "image/jpeg")},
    )
    assert photo2.status_code == 200
    image_id_2 = photo2.json()["image_id"]

    photos = client.get("/api/sites/site_00/capture-locations/loc_01/photos").json()
    assert len(photos) == 2

    # Photo count reflects on the location listing too
    listing = client.get("/api/sites/site_00/floors/1/capture-locations").json()
    assert listing[0]["photo_count"] == 2

    # Serving the actual photo file works
    img_res = client.get(f"/api/sites/site_00/photos/{image_id_2}")
    assert img_res.status_code == 200

    # Update a heading
    patch_res = client.patch(
        f"/api/sites/site_00/capture-locations/loc_01/photos/{image_id_2}",
        json={"heading_degrees": 180},
    )
    assert patch_res.status_code == 200
    assert patch_res.json()["heading_degrees"] == 180
    updated_image_id = patch_res.json()["image_id"]
    assert updated_image_id.endswith("_180")
    assert updated_image_id != image_id_2
    assert client.get(f"/api/sites/site_00/photos/{updated_image_id}").status_code == 200
    assert client.get(f"/api/sites/site_00/photos/{image_id_2}").status_code == 404
    image_id_2 = updated_image_id

    # Reject an out-of-range heading
    bad_res = client.patch(
        f"/api/sites/site_00/capture-locations/loc_01/photos/{image_id_2}",
        json={"heading_degrees": 999},
    )
    assert bad_res.status_code == 400

    # Delete one photo
    del_res = client.delete(f"/api/sites/site_00/capture-locations/loc_01/photos/{image_id_2}")
    assert del_res.status_code == 200
    photos = client.get("/api/sites/site_00/capture-locations/loc_01/photos").json()
    assert len(photos) == 1
    assert client.get(f"/api/sites/site_00/photos/{image_id_2}").status_code == 404

    # Delete the whole location -- removes the remaining photo too
    del_loc_res = client.delete("/api/sites/site_00/capture-locations/loc_01")
    assert del_loc_res.status_code == 200
    assert del_loc_res.json()["photos_removed"] == 1
    assert client.get("/api/sites/site_00/floors/1/capture-locations").json() == []


def test_photo_upload_rejects_bad_content_type(client: TestClient):
    _calibrated_site(client)
    client.post(
        "/api/sites/site_00/floors/1/capture-locations",
        json={"location_id": "loc_01", "zone": "Zone_A", "px": 100, "py": 100},
    )
    res = client.post(
        "/api/sites/site_00/capture-locations/loc_01/photos",
        data={"heading_degrees": "0"},
        files={"file": ("notes.txt", b"hello", "text/plain")},
    )
    assert res.status_code == 400


def test_photo_upload_requires_existing_location(client: TestClient):
    _calibrated_site(client)
    res = client.post(
        "/api/sites/site_00/capture-locations/does_not_exist/photos",
        data={"heading_degrees": "0"},
        files={"file": ("p1.jpg", _tiny_jpeg_bytes(), "image/jpeg")},
    )
    assert res.status_code == 404


def test_build_embeddings_endpoint_calls_site_index_builder(client: TestClient, monkeypatch):
    _calibrated_site(client)
    client.post(
        "/api/sites/site_00/floors/1/capture-locations",
        json={"location_id": "loc_01", "zone": "Zone_A", "px": 100, "py": 100},
    )
    upload = client.post(
        "/api/sites/site_00/capture-locations/loc_01/photos",
        data={"heading_degrees": "90"},
        files={"file": ("p1.jpg", _tiny_jpeg_bytes(), "image/jpeg")},
    )
    assert upload.status_code == 200

    calls = []

    def fake_build_index(site):
        calls.append(site.site_id)
        records = server_module.load_records(site.dataset_jsonl_path)
        np.savez(
            site.embeddings_path,
            ids=np.array([record.image_id for record in records]),
            embeddings=np.zeros((len(records), 4), dtype=np.float32),
            dataset_version=np.array("test-version"),
        )

    monkeypatch.setattr(server_module, "build_index", fake_build_index)
    response = client.post("/api/sites/site_00/embeddings/build")

    assert response.status_code == 200
    assert calls == ["site_00"]
    assert response.json() == {
        "indexed_images": 1,
        "embedding_dim": 4,
        "dataset_version": "test-version",
    }


def test_build_embeddings_endpoint_rejects_empty_site(client: TestClient):
    client.post("/api/sites/site_00")

    response = client.post("/api/sites/site_00/embeddings/build")

    assert response.status_code == 400
    assert "No ingested photos" in response.json()["detail"]


def test_capture_location_upsert_moves_existing_location(client: TestClient):
    _calibrated_site(client)
    client.post(
        "/api/sites/site_00/floors/1/capture-locations",
        json={"location_id": "loc_01", "zone": "Zone_A", "px": 100, "py": 100},
    )
    moved = client.post(
        "/api/sites/site_00/floors/1/capture-locations",
        json={"location_id": "loc_01", "zone": "Zone_B", "px": 300, "py": 100},
    )
    assert moved.status_code == 200
    listing = client.get("/api/sites/site_00/floors/1/capture-locations").json()
    assert len(listing) == 1  # still just one location, not duplicated
    assert listing[0]["zone"] == "Zone_B"
    assert listing[0]["x"] == pytest.approx(10.0, abs=1e-6)
