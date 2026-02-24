# AIS MPA Monitor

Web app that monitors vessel locations near the California coast and checks whether they are inside California Marine Protected Areas (MPAs). It supports live vessel traffic on a map, vessel trails (dotted paths), and MPA violation counts (entry-only). **Stack:** Backend (Python + FastAPI), DB (PostgreSQL + PostGIS in Docker), Frontend (Next.js, TypeScript), Map (MapLibre GL).

---

## Project structure

```
chartedaismonitor/
├── backend/                 # FastAPI API and ingestion
│   ├── main.py              # API routes (zones, vessels/live, vessels/{mmsi}/trail, zones/{id}/stats)
│   ├── database.py          # Postgres connection (DATABASE_URL)
│   ├── Dockerfile           # Image for API server
│   ├── requirements.txt     # Python deps (fastapi, uvicorn, psycopg2-binary, shapely, httpx, pytest)
│   ├── scripts/
│   │   ├── import_mpas.py   # Import California MPA GeoJSON into zones table
│   │   ├── ingest_ais.py    # HTTP/file ingest (AISHub, AIS_API_URL, sample_ais.json)
│   │   ├── ingest_aisstream.py  # AISStream.io WebSocket ingest (California coast; AISSTREAM_API_KEY)
│   │   └── sample_ais.json  # Synthetic AIS data for testing
│   └── tests/
│       └── test_ingest_entry_detection.py  # Pytest: outside→inside creates one violation
├── db/
│   └── init.sql             # PostGIS, zones, vessels, vessel_positions, mpa_violations
├── frontend/                # Next.js app
│   ├── app/
│   │   ├── page.tsx         # Home
│   │   ├── map/page.tsx     # Map page wrapper
│   │   └── map/Map.tsx      # MapLibre: zones, live vessels, trails, MPA click popups
│   └── package.json         # next, maplibre-gl, react
├── docker-compose.yml       # db (PostGIS), backend (FastAPI)
├── .env.example             # DATABASE_URL, AISSTREAM_API_KEY, AISHUB_USERNAME, AIS_API_URL
└── README.md
```

---

## Testing

### 1. Start services

```bash
cd chartedaismonitor
cp .env.example .env
docker compose up --build -d
```

If you change backend code, rebuild so the API container uses it:

```bash
docker compose up --build -d backend
```

Wait until backend is healthy. Then import MPA zones (required for point-in-zone and zone stats):

```bash
docker compose exec backend python scripts/import_mpas.py
```

### 2. Backend unit / integration tests (pytest)

With DB reachable at `localhost:5432`, use the **same Python environment** for both installing deps and running pytest (e.g. activate your venv first). `psycopg2` is provided by the `psycopg2-binary` package in `requirements.txt`.

```bash
cd chartedaismonitor/backend
pip install -r requirements.txt
export DATABASE_URL=postgresql://ais_user:ais_pass@localhost:5432/ais
pytest tests/ -v
```

Or run pytest inside the backend container (DB host = `db`):

```bash
docker compose exec backend sh -c 'cd /app && DATABASE_URL=postgresql://ais_user:ais_pass@db:5432/ais pytest tests/ -v'
```

Expected: `test_outside_to_inside_creates_single_entry_violation` passes (one `mpa_violations` row when a vessel moves from outside a test zone to inside).

### 3. API tests (curl)

Base URL: `http://localhost:8000`. Run with backend and DB up.

**Health and debug**

```bash
curl -s http://localhost:8000/
curl -s http://localhost:8000/debug/stats
```

**Point-in-zone (inside)**  
Use a point inside a known MPA after import (e.g. Point Lobos area):

```bash
curl -s "http://localhost:8000/inside?lat=36.52&lon=-121.95"
```

Expect: `"inside":true` and non-empty `matched_zones`.

**Point-in-zone (outside)**

```bash
curl -s "http://localhost:8000/inside?lat=37.8&lon=-122.4"
```

Expect: `"inside":false`, `matched_zones":[]`.

**Vessel update and vessel-in-zone**

```bash
curl -s -X POST http://localhost:8000/vessels/update \
  -H "Content-Type: application/json" \
  -d '{"mmsi":"123456789","name":"Test Vessel","lat":36.52,"lon":-121.95}'

curl -s "http://localhost:8000/vessels/123456789/inside"
```

Expect: `"inside":true` and `matched_zones` when the point is inside an MPA.

**Live vessels (map feed)**

```bash
curl -s "http://localhost:8000/vessels/live?limit=50"
```

Expect: JSON array of `{ mmsi, name, lat, lon, last_ts, inside_any_mpa }`.

**Vessel trail**

```bash
curl -s "http://localhost:8000/vessels/123456789/trail?hours=6&limit=500"
```

Expect: `{ mmsi, hours, count, positions, line }` (line is GeoJSON LineString or null).

**Zone stats (violation count)**

First get a zone id from `/zones` or `/debug/stats`, then:

```bash
curl -s "http://localhost:8000/zones/1/stats"
```

Expect: `{ id, name, designation, violation_count, last_violation_ts }`.

**Zones with violation counts (GeoJSON)**

```bash
curl -s "http://localhost:8000/zones/with-stats"
```

Expect: GeoJSON FeatureCollection; each feature has `properties.violation_count`.

### 4. How to ingest AIS data

AIS ingestion fills the `vessels` and `vessel_positions` tables and records MPA entries in `mpa_violations`. The map shows **live vessels** from `/vessels/live` and **trails only for vessels that have passed through an MPA** (violators).

**Prerequisites:** DB and backend running (`docker compose up -d`), and MPA zones imported (`docker compose exec backend python scripts/import_mpas.py`).

#### Option A – AISStream.io (recommended) – Live WebSocket

[AISStream.io](https://aisstream.io/documentation) provides a WebSocket stream of AIS data. The ingest script subscribes to the **California coast only** (lat 32°–42.5°N, lon 125°–114°W) and writes each position to the database.

1. Get an API key from [AISStream](https://aisstream.io/) (sign in, create key at [API Keys](https://aisstream.io/apikeys)).

2. Set it in `.env` in the chartedaismonitor directory:
   ```bash
   AISSTREAM_API_KEY=your_api_key
   ```

3. Start the WebSocket ingester (long-running; leave it in a terminal or run in background):
   ```bash
   docker compose exec backend python scripts/ingest_aisstream.py
   ```
   It will connect to `wss://stream.aisstream.io/v0/stream`, subscribe to the California bounding box, and process `PositionReport` (and related) messages. Stop with Ctrl+C. On disconnect it will try to reconnect.

4. Open the map; with “Show live vessels” and “Show MPA violator trails” on, you should see vessels and trails for any that have entered an MPA.

#### Option B – One-time ingest from the sample file (testing)

From the **chartedaismonitor** directory:

```bash
docker compose exec backend python scripts/ingest_ais.py --source scripts/sample_ais.json
```

This adds a few test vessels. Violations are only created if a position falls inside an MPA (the sample points are outside California MPAs unless you change them). Then open the map and check “Show live vessels” and “Show MPA violator trails”.

#### Option C – AISHub (California coast only, HTTP)

The ingest script can pull from [AISHub](https://www.aishub.net/) with a **fixed bounding box** around the California coast (lat 32°–42.5°N, lon 125°–114°W). You need a free AISHub username.

1. Add your username to `.env` in the chartedaismonitor directory:
   ```bash
   AISHUB_USERNAME=your_aishub_username
   ```

2. One-time ingest (positions up to 60 minutes old):
   ```bash
   docker compose exec backend python scripts/ingest_ais.py --source aishub
   ```
   To limit to positions from the last 30 minutes:
   ```bash
   docker compose exec backend python scripts/ingest_ais.py --source aishub --interval-minutes 30
   ```

3. Continuous ingestion (poll every 60 seconds):
   ```bash
   docker compose exec backend python scripts/ingest_ais.py --source aishub --loop --interval 60
   ```

The script uses human-readable JSON (`format=1`), no compression, and the California bounding box only.

#### Option D – One-time ingest from another AIS API

Set `AIS_API_URL` (and optional `AIS_API_KEY`) in `.env`, then:

```bash
docker compose exec backend python scripts/ingest_ais.py
```

Or pass the URL explicitly:

```bash
docker compose exec backend python scripts/ingest_ais.py --source "https://your-ais-api.com/positions"
```

Each vessel object should have at least `mmsi` (or `MMSI`), `lat` (or `latitude`), and `lon` (or `longitude`). Optional: `name`, `timestamp` (ISO-8601).

#### Option E – Continuous ingestion (polling, HTTP sources)

To keep ingesting every 60 seconds:

```bash
docker compose exec backend python scripts/ingest_ais.py --loop --interval 60
```

Use `--source aishub`, or `AIS_API_URL`, or a URL with `--source`. Stop with Ctrl+C.

#### Option F – Manually add a vessel (for quick testing)

You can create one vessel and its trail without the ingest script:

```bash
# Put a vessel inside an MPA (creates a violation and trail point)
curl -s -X POST http://localhost:8000/vessels/update \
  -H "Content-Type: application/json" \
  -d '{"mmsi":"999888777","name":"Test Boat","lat":36.52,"lon":-121.95}'
```

Run that once; then open the map. The vessel appears as a red point (inside MPA), and after a refresh it will have a violator trail. You can call `/vessels/update` again with different coordinates to build a short trail.

### 5. Frontend / UI tests

Start the frontend:

```bash
cd frontend
npm install
npm run dev
```

Open http://localhost:3000. Set `NEXT_PUBLIC_API_URL=http://localhost:8000` in `frontend/.env` if the API is not on the same host.

**Manual checks**

1. **Map and zones**  
   Go to the map (e.g. “View map (California MPAs)”). Confirm MPA polygons load and the “Show MPA boundaries” toggle works.

2. **Live vessels**  
   Ensure “Show live vessels” is on. After ingesting sample AIS or updating a vessel via API, confirm vessel circles appear (green = outside MPA, red = inside).

3. **Vessel trail**  
   Click a vessel circle. A dotted trail should appear and a small popup show vessel MMSI and trail point count.

4. **MPA popup**  
   Click inside an MPA polygon. A popup should show zone name, designation, violation count, and last violation time.

### 6. Full flow summary

| Step | Command / action |
|------|-------------------|
| Start stack | `docker compose up --build -d` |
| Import MPAs | `docker compose exec backend python scripts/import_mpas.py` |
| Ingest AIS | Option A: `docker compose exec backend python scripts/ingest_ais.py --source scripts/sample_ais.json`. Or Option B/C with `AIS_API_URL` and `--loop` if needed. Or Option D: `curl -X POST http://localhost:8000/vessels/update ...` |
| Frontend | `cd frontend && npm install && npm run dev` → http://localhost:3000 |
| UI | Map + zones, live vessels, “Show MPA violator trails”, vessel click → popup/trail, MPA click → stats popup |
| Backend tests | `cd backend && DATABASE_URL=... pytest tests/ -v` |
| API smoke | `curl http://localhost:8000/` and curl commands above |
