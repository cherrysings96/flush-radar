# FlushRadar ⚡

A fast, lightweight global restroom finder built with **Python, Flask, SQLite, Leaflet.js, and OpenStreetMap**.

## Why it's fast
- **Parallel Overpass Mirror Racing:** Queries 3 Overpass mirrors concurrently with a strict 6-second timeout and returns the fastest response.
- **24-Hour SQLite Spatial Cache:** Repeat searches on the same area load in under 10 ms.
- **Instant Client-Side Filtering:** Filter by Free, Wheelchair Accessible, Baby Changing, and 24/7 with zero server round-trips.
- **Pre-Seeded Local DB:** Works immediately out of the box even if external APIs are slow.

## Quick Start
```bash
pip install -r requirements.txt
python app.py
```
Open `http://127.0.0.1:5000` in your browser.

## Run Tests
```bash
pytest
```

## Deploy on Render
- **Build Command:** `pip install -r requirements.txt`
- **Start Command:** `gunicorn app:app --workers 2 --threads 4 --timeout 30`
