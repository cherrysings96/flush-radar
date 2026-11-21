import os
import tempfile
import pytest

# Use an isolated temp DB for tests
tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
tmp_db.close()
os.environ["FLUSHRADAR_DB"] = tmp_db.name

import app as flush_app


@pytest.fixture
def client(monkeypatch):
    # Mock live Overpass calls so tests run in milliseconds offline
    monkeypatch.setattr(flush_app, "fetch_overpass_fast", lambda lat, lon, radius_m=3000: [])
    flush_app.init_db()
    flush_app.app.config["TESTING"] = True
    with flush_app.app.test_client() as c:
        yield c


def test_index_loads(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"FlushRadar" in resp.data


def test_seeded_restrooms_return_fast(client):
    resp = client.get("/api/restrooms?lat=8.5241&lon=76.9366&radius=10000")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["count"] >= 3
    assert data["elapsed_ms"] < 500


def test_add_and_filter_restroom(client):
    post_resp = client.post(
        "/api/restrooms",
        json={
            "name": "Test Express Lounge",
            "address": "Test Street",
            "lat": 8.5242,
            "lon": 76.9367,
            "is_free": True,
            "wheelchair": True,
            "changing_table": True,
            "open_24h": True,
        },
    )
    assert post_resp.status_code == 201
    rid = post_resp.get_json()["id"]

    get_resp = client.get("/api/restrooms?lat=8.5242&lon=76.9367&radius=2000&wheelchair=1&open24=1")
    data = get_resp.get_json()
    ids = [r["id"] for r in data["restrooms"]]
    assert rid in ids
