import os
import math
import json
import time
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, render_template, request, jsonify
import requests

app = Flask(__name__)
DB_PATH = os.environ.get("FLUSHRADAR_DB", os.path.join(os.path.dirname(__file__), "flushradar.db"))

# Fast Overpass mirrors to race in parallel (used as fallback if Places API is unavailable)
OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://lz4.overpass-api.de/api/interpreter",
]
PHOTON_URL = "https://photon.komoot.io/api/"
CACHE_TTL_SECONDS = 86400  # 24 hours for instant repeat loads
HTTP_TIMEOUT = 6  # Strict timeout so the app never hangs


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE IF NOT EXISTS restrooms (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            lat REAL NOT NULL,
            lon REAL NOT NULL,
            address TEXT DEFAULT '',
            is_free INTEGER DEFAULT 1,
            wheelchair INTEGER DEFAULT 0,
            changing_table INTEGER DEFAULT 0,
            open_24h INTEGER DEFAULT 0,
            rating REAL DEFAULT 4.2,
            rating_count INTEGER DEFAULT 1,
            source TEXT DEFAULT 'community',
            created_at REAL DEFAULT (strftime('%s', 'now'))
        );

        CREATE INDEX IF NOT EXISTS idx_restrooms_lat_lon ON restrooms(lat, lon);

        CREATE TABLE IF NOT EXISTS search_cache (
            cache_key TEXT PRIMARY KEY,
            payload TEXT NOT NULL,
            updated_at REAL NOT NULL
        );
        """
    )

    # Seed a few initial community spots so the DB is immediately usable offline
    cur.execute("SELECT COUNT(*) FROM restrooms")
    if cur.fetchone()[0] == 0:
        seed_spots = [
            ("seed_tvm_1", "LuLu Mall Restrooms (Ground & Food Court)", 8.5326, 76.9061, "Akkulam, Thiruvananthapuram", 1, 1, 1, 0, 4.7, 18, "community"),
            ("seed_tvm_2", "Central Railway Station Paid Lounge", 8.4875, 76.9525, "Thampanoor, Thiruvananthapuram", 0, 1, 0, 1, 4.1, 12, "community"),
            ("seed_tvm_3", "Kowdiar Park Public Facility", 8.5241, 76.9587, "Kowdiar, Thiruvananthapuram", 1, 0, 0, 0, 3.8, 6, "community"),
            ("seed_tvm_4", "Museum & Zoo Complex Restroom", 8.5094, 76.9554, "Vikas Bhavan, Thiruvananthapuram", 1, 1, 0, 0, 4.0, 9, "community"),
            ("seed_tvm_5", "Mall of Travancore Restrooms", 8.4884, 76.9178, "Eanchakkal, Thiruvananthapuram", 1, 1, 1, 0, 4.6, 15, "community"),
        ]
        cur.executemany(
            """
            INSERT OR IGNORE INTO restrooms
            (id, name, lat, lon, address, is_free, wheelchair, changing_table, open_24h, rating, rating_count, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            seed_spots,
        )
    conn.commit()
    conn.close()


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def fetch_places_new(lat, lon, radius_m=4000):
    """Query Google Places API (New) for nearby public restrooms."""
    api_key = os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()
    if not api_key:
        return []

    url = "https://places.googleapis.com/v1/places:searchText"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": (
            "places.id,places.displayName,places.formattedAddress,"
            "places.location,places.rating,places.userRatingCount,"
            "places.accessibilityOptions"
        ),
    }
    payload = {
        "textQuery": "public restroom",
        "maxResultCount": 20,
        "locationBias": {
            "circle": {
                "center": {"latitude": lat, "longitude": lon},
                "radius": float(radius_m),
            }
        },
    }

    resp = requests.post(url, json=payload, headers=headers, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    places = resp.json().get("places", [])

    results = []
    for p in places:
        loc = p.get("location", {})
        if "latitude" not in loc or "longitude" not in loc:
            continue
        access = p.get("accessibilityOptions") or {}
        wheelchair = (
            1
            if access.get("wheelchairAccessibleRestroom") or access.get("wheelchairAccessibleEntrance")
            else 0
        )

        results.append(
            {
                "id": f"gplaces_{p.get('id')}",
                "name": (p.get("displayName") or {}).get("text", "Public Restroom"),
                "lat": float(loc["latitude"]),
                "lon": float(loc["longitude"]),
                "address": p.get("formattedAddress", "Google Maps Verified"),
                "is_free": 1,
                "wheelchair": wheelchair,
                "changing_table": 0,
                "open_24h": 0,
                "rating": float(p.get("rating", 4.0)),
                "rating_count": int(p.get("userRatingCount", 1)),
                "source": "google_places",
            }
        )
    return results


def _query_single_overpass(endpoint, query):
    resp = requests.post(
        endpoint,
        data={"data": query},
        headers={"User-Agent": "FlushRadar/2.0 (Fast Restroom Finder)"},
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_overpass_fast(lat, lon, radius_m=3000):
    """Race multiple Overpass mirrors in parallel and return the first valid response."""
    query = (
        f"[out:json][timeout:{HTTP_TIMEOUT}];"
        f"("
        f'node["amenity"="toilets"](around:{radius_m},{lat},{lon});'
        f'way["amenity"="toilets"](around:{radius_m},{lat},{lon});'
        f");"
        f"out center 60;"
    )

    with ThreadPoolExecutor(max_workers=len(OVERPASS_ENDPOINTS)) as pool:
        futures = [pool.submit(_query_single_overpass, ep, query) for ep in OVERPASS_ENDPOINTS]
        for future in as_completed(futures, timeout=HTTP_TIMEOUT + 1):
            try:
                data = future.result()
                if data and "elements" in data:
                    return parse_overpass_elements(data["elements"])
            except Exception:
                continue
    return []


def parse_overpass_elements(elements):
    results = []
    for el in elements:
        tags = el.get("tags", {})
        el_lat = el.get("lat") or (el.get("center") or {}).get("lat")
        el_lon = el.get("lon") or (el.get("center") or {}).get("lon")
        if el_lat is None or el_lon is None:
            continue

        fee_tag = str(tags.get("fee", "")).lower()
        is_free = 0 if fee_tag in ("yes", "true", "1") else 1
        wheelchair = 1 if str(tags.get("wheelchair", "")).lower() in ("yes", "designated", "limited") else 0
        changing = 1 if str(tags.get("changing_table", "")).lower() in ("yes", "limited") else 0
        hours = str(tags.get("opening_hours", ""))
        open_24h = 1 if "24/7" in hours else 0

        name = tags.get("name") or tags.get("operator") or "Public Restroom"
        addr_parts = [tags.get("addr:street", ""), tags.get("addr:suburb", ""), tags.get("addr:city", "")]
        address = ", ".join([p for p in addr_parts if p]) or tags.get("description", "OpenStreetMap Verified")

        results.append(
            {
                "id": f"osm_{el.get('type', 'n')}_{el.get('id')}",
                "name": name,
                "lat": float(el_lat),
                "lon": float(el_lon),
                "address": address,
                "is_free": is_free,
                "wheelchair": wheelchair,
                "changing_table": changing,
                "open_24h": open_24h,
                "rating": 4.0,
                "rating_count": 1,
                "source": "osm",
            }
        )
    return results


def get_cached_results(lat, lon, radius_m):
    # Round to ~1km grid for high cache hit rate
    key = f"{round(lat, 2)}:{round(lon, 2)}:{radius_m}"
    conn = get_db()
    row = conn.execute("SELECT payload, updated_at FROM search_cache WHERE cache_key = ?", (key,)).fetchone()
    conn.close()
    if row and (time.time() - row["updated_at"]) < CACHE_TTL_SECONDS:
        return json.loads(row["payload"]), True
    return None, False


def set_cached_results(lat, lon, radius_m, items):
    key = f"{round(lat, 2)}:{round(lon, 2)}:{radius_m}"
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO search_cache (cache_key, payload, updated_at) VALUES (?, ?, ?)",
        (key, json.dumps(items), time.time()),
    )
    conn.commit()
    conn.close()


def get_local_restrooms(lat, lon, radius_km):
    # Bounding box pre-filter in SQL for speed
    lat_delta = radius_km / 111.0
    lon_delta = radius_km / max(111.0 * math.cos(math.radians(lat)), 1e-5)

    conn = get_db()
    rows = conn.execute(
        """
        SELECT * FROM restrooms
        WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?
        """,
        (lat - lat_delta, lat + lat_delta, lon - lon_delta, lon + lon_delta),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.route("/")
def index():
    gmaps_key = os.environ.get("GOOGLE_MAPS_API_KEY", "")
    return render_template("index.html", gmaps_key=gmaps_key)


@app.route("/api/geocode")
def api_geocode():
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify({"results": []})
    try:
        resp = requests.get(
            PHOTON_URL,
            params={"q": q, "limit": 5},
            headers={"User-Agent": "FlushRadar/2.0"},
            timeout=4,
        )
        resp.raise_for_status()
        features = resp.json().get("features", [])
        results = []
        for f in features:
            props = f.get("properties", {})
            coords = f.get("geometry", {}).get("coordinates", [0, 0])
            label_parts = [props.get("name"), props.get("city"), props.get("state"), props.get("country")]
            label = ", ".join([p for p in label_parts if p])
            results.append({"label": label or q, "lat": coords[1], "lon": coords[0]})
        return jsonify({"results": results})
    except Exception:
        return jsonify({"results": []})


@app.route("/api/restrooms")
def api_restrooms():
    t0 = time.perf_counter()
    try:
        lat = float(request.args.get("lat", 8.5241))
        lon = float(request.args.get("lon", 76.9366))
        radius_m = min(max(int(request.args.get("radius", 4000)), 500), 15000)
    except ValueError:
        return jsonify({"error": "Invalid coordinates or radius"}), 400

    radius_km = radius_m / 1000.0
    local_items = get_local_restrooms(lat, lon, radius_km)

    remote_items, cache_hit = get_cached_results(lat, lon, radius_m)
    if remote_items is None:
        try:
            remote_items = fetch_places_new(lat, lon, radius_m)
        except Exception:
            remote_items = []

        if not remote_items:
            try:
                remote_items = fetch_overpass_fast(lat, lon, radius_m)
            except Exception:
                remote_items = []

        if remote_items:
            set_cached_results(lat, lon, radius_m, remote_items)

    # Merge & deduplicate by ID
    merged = {}
    for item in remote_items + local_items:
        dist = haversine_km(lat, lon, item["lat"], item["lon"])
        if dist <= radius_km:
            item_copy = dict(item)
            item_copy["distance_km"] = round(dist, 2)
            merged[item_copy["id"]] = item_copy

    # Optional server-side filters
    if request.args.get("free") == "1":
        merged = {k: v for k, v in merged.items() if v.get("is_free") == 1}
    if request.args.get("wheelchair") == "1":
        merged = {k: v for k, v in merged.items() if v.get("wheelchair") == 1}
    if request.args.get("changing") == "1":
        merged = {k: v for k, v in merged.items() if v.get("changing_table") == 1}
    if request.args.get("open24") == "1":
        merged = {k: v for k, v in merged.items() if v.get("open_24h") == 1}

    sorted_items = sorted(merged.values(), key=lambda x: x["distance_km"])
    elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)

    return jsonify(
        {
            "count": len(sorted_items),
            "cache_hit": cache_hit,
            "elapsed_ms": elapsed_ms,
            "restrooms": sorted_items,
        }
    )


@app.route("/api/restrooms", methods=["POST"])
def api_add_restroom():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    try:
        lat = float(data.get("lat"))
        lon = float(data.get("lon"))
    except (TypeError, ValueError):
        return jsonify({"error": "Valid lat and lon are required"}), 400

    if not name:
        return jsonify({"error": "Restroom name is required"}), 400

    rid = f"user_{int(time.time() * 1000)}"
    conn = get_db()
    conn.execute(
        """
        INSERT INTO restrooms (id, name, lat, lon, address, is_free, wheelchair, changing_table, open_24h, rating, rating_count, source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'community')
        """,
        (
            rid,
            name,
            lat,
            lon,
            (data.get("address") or "Community Added").strip(),
            1 if data.get("is_free", True) else 0,
            1 if data.get("wheelchair", False) else 0,
            1 if data.get("changing_table", False) else 0,
            1 if data.get("open_24h", False) else 0,
            5.0,
            1,
        ),
    )
    conn.commit()
    conn.close()
    return jsonify({"id": rid, "status": "created"}), 201


@app.route("/api/restrooms/<rid>/rate", methods=["POST"])
def api_rate_restroom(rid):
    data = request.get_json(silent=True) or {}
    try:
        score = float(data.get("rating", 5))
        if not (1.0 <= score <= 5.0):
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"error": "Rating must be between 1 and 5"}), 400

    conn = get_db()
    row = conn.execute("SELECT rating, rating_count FROM restrooms WHERE id = ?", (rid,)).fetchone()
    if row:
        new_count = row["rating_count"] + 1
        new_rating = round(((row["rating"] * row["rating_count"]) + score) / new_count, 2)
        conn.execute("UPDATE restrooms SET rating = ?, rating_count = ? WHERE id = ?", (new_rating, new_count, rid))
        conn.commit()
        conn.close()
        return jsonify({"id": rid, "rating": new_rating, "rating_count": new_count})

    conn.close()
    return jsonify({"id": rid, "rating": score, "rating_count": 1})


init_db()

if __name__ == "__main__":
    app.run(debug=True, host="127.0.0.1", port=5000)