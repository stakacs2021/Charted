# AIS MPA Monitor

Web app that monitors vessel locations near the California coast and checks whether they are inside California Marine Protected Areas (MPAs). It supports live vessel traffic on a map, vessel trails (dotted paths), and MPA violation counts (entry-only). **Stack:** Backend (Python + FastAPI), DB (PostgreSQL + PostGIS in Docker), Frontend (Next.js, TypeScript), Map (MapLibre GL).

### Features (map and API)

- **Nationality / flag** — Country name and ISO2 code are derived from the vessel MMSI’s **Maritime Identification Digits (MID)** (first three digits for ship MMSI). This is the standard ITU registry for the flag state; it may not match every edge case in the real world. Callsign is stored when provided by AIS. See [`backend/mmsi_mid.py`](backend/mmsi_mid.py).
- **Course / heading** — COG and true heading are ingested when present; the map rotates ship icons using **bearing** (COG, else heading, else bearing from the last two stored positions).
- **Trails** — Optional **MPA violator** trails (red) and optional **all vessels in view** trails (blue, capped at 40 ships per refresh for performance).
- **Leaderboard** — `/leaderboard` lists top MMSIs by MPA entry count.
- **Violation suppression pipeline** — Four layers, applied in this order on every AIS fix:
  1. **Ingest heuristics** in `process_batch` — drop fast-moving transits (`SOG > MPA_SPEED_TRANSIT_KN`, default 5 kn), brief crossings (`MIN_DWELL_SECONDS`, default 120 s), and edge clipping (`BOUNDARY_BUFFER_M`, default 30 m).
  2. **Per-MMSI allowlist** (`mpa_violation_allowlist`) — `(mmsi, zone_id)` rows. `zone_id IS NULL` means "all zones" (used for institutional fleets like USCG, NOAA, port pilots).
  3. **Vessel-type policy** (`zone_vessel_type_policy`) — `(zone_bracket_class, vessel_type) → allowed`. Default stance after `seed_policy.py`: transit is legal for non-fishing types in every bracket; fishing in `NoTake` and `SpecialClosure` is denied.
  4. **DB trigger** `trg_mpa_violations_policy` — calls `is_vessel_allowed_in_zone(mmsi, zone_id)`, which combines the above and treats NULL `vessel_type` as the configurable `'unknown'` bucket.
- All layers are live-editable: tune `MPA_*` env vars in `.env`, run `seed_policy.py` / `seed_allowlist.py` / `enrich_vessel_types.py` whenever rules change. No redeploy required.

### Seeding the suppression rules (one-time per environment)

```bash
# 1. Type-policy defaults (transit-legal; fishing-deny in strict zones).
docker compose exec backend python scripts/seed_policy.py
# 1b. Optionally tighten unknown-type fallback once vessel_type coverage is good:
docker compose exec backend python scripts/seed_policy.py --strict-unknown

# 2. Wildcard allowlist for institutional fleets (edit allowlist_seed.csv first).
docker compose exec backend python scripts/seed_allowlist.py

# 3. Backfill vessel_type for rows ingest didn't label (CSV overrides + name regex).
docker compose exec backend python scripts/enrich_vessel_types.py --dry-run
docker compose exec backend python scripts/enrich_vessel_types.py
```

### Curating the allowlist by hand

```sql
-- Whitelist a single MMSI in every zone (institutional / wildcard).
INSERT INTO mpa_violation_allowlist (mmsi, zone_id, category, note)
VALUES ('123456789', NULL, 'government', 'USCGC Foo')
ON CONFLICT (mmsi) WHERE zone_id IS NULL DO UPDATE SET note = EXCLUDED.note;

-- Whitelist a single MMSI in one specific zone (per-site permit).
INSERT INTO mpa_violation_allowlist (mmsi, zone_id, note)
VALUES ('123456789', 42, 'Charter permit')
ON CONFLICT (mmsi, zone_id) WHERE zone_id IS NOT NULL DO UPDATE SET note = EXCLUDED.note;

-- Remove either kind.
DELETE FROM mpa_violation_allowlist WHERE mmsi = '123456789' AND zone_id IS NULL;
DELETE FROM mpa_violation_allowlist WHERE mmsi = '123456789' AND zone_id = 42;
```

From Docker (host shell):

```bash
docker compose exec db psql -U ais_user -d ais \
  -c "SELECT mmsi, zone_id, category, note FROM mpa_violation_allowlist ORDER BY mmsi, zone_id NULLS FIRST;"
```

### Admin / curation endpoints

- `GET /admin/vessel-type-coverage` — what fraction of vessels (and current violators) have a known type. Drives the decision to flip `seed_policy.py --strict-unknown`.
- `GET /admin/violators/review?limit=50` — top violators enriched with `vessel_type`, `country`, `callsign`, `bucket_source`, and `allowlisted_global` flag. Use this to discover which MMSIs to add to `allowlist_seed.csv`.

The ingest scripts and API both run `ensure_core_schema` / `ensure_extended_schema` at startup, so existing databases pick up the new columns, indexes, and trigger automatically on the next backend or ingest restart.

### Docs

- **[Self-hosting on Ubuntu](SELF_HOST.md)** — Docker, Nginx, TLS, firewall, ingest service, backups.
- **[Testing & live rollout](TESTING.md)** — test inventory, sidecar-DB recipe for testing without touching production, canary plan, rollback knobs.
- **[Contributing & Git cadence](CONTRIBUTING.md)** — Weekly commits, branches, secrets hygiene.

---

## Project structure

```
chartedaismonitor/
├── backend/                 # FastAPI API and ingestion
│   ├── main.py              # API routes (zones, vessels/live, vessels/{mmsi}/trail, zones/{id}/stats)
│   ├── database.py          # Postgres connection (DATABASE_URL)
│   ├── Dockerfile           # Image for API server and ingest service
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
├── deploy/                  # Nginx sample, systemd unit, Compose override for Nginx
├── frontend/                # Next.js app (standalone Docker image)
│   ├── Dockerfile
│   ├── app/
│   │   ├── page.tsx         # Home
│   │   ├── map/page.tsx     # Map page wrapper
│   │   └── map/Map.tsx      # MapLibre: zones, live vessels, trails, MPA click popups
│   └── package.json         # next, maplibre-gl, react
├── scripts/
│   └── backup-db.sh         # pg_dump helper (Postgres on 127.0.0.1:5432)
├── docker-compose.yml       # db, backend, frontend; ingest optional (profile `ais`)
├── .env.example             # POSTGRES_PASSWORD, DATABASE_URL, NEXT_PUBLIC_API_URL, AISSTREAM_API_KEY
├── Makefile                 # up, up-ais, import-mpas, backup
├── SELF_HOST.md             # Production-style deploy on Ubuntu (implemented stack)
├── CONTRIBUTING.md          # Git workflow and weekly commit habit
└── README.md
```

---

## Testing

### 1. Start services

```bash
cd chartedaismonitor
cp .env.example .env
# Edit .env: set POSTGRES_PASSWORD, NEXT_PUBLIC_API_URL (e.g. http://YOUR_LAN_IP:8000), AISSTREAM_API_KEY as needed
docker compose up --build -d
```

This starts **PostGIS**, **FastAPI** (port **8000**), and **Next.js** (port **3000**). Optional **AISStream ingest**: add `COMPOSE_PROFILES=ais` to `.env` or run `make up-ais` after setting `AISSTREAM_API_KEY`.

If you change backend code, rebuild so the API container uses it:

```bash
docker compose up --build -d backend
```

If you change **`NEXT_PUBLIC_API_URL`**, rebuild the frontend image (the value is baked in at build time):

```bash
docker compose build --no-cache frontend && docker compose up -d frontend
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
export DATABASE_URL=postgresql://ais_user:YOUR_PASSWORD@127.0.0.1:5432/ais
pytest tests/ -v
```

Or run pytest inside the backend container (DB host = `db`; password must match `.env`):

```bash
docker compose exec backend sh -c 'cd /app && pytest tests/ -v'
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

3. Run the WebSocket ingester continuously — either add `COMPOSE_PROFILES=ais` to `.env` and `docker compose up -d` (see **SELF_HOST.md**), or in a terminal:
   ```bash
   docker compose exec backend python scripts/ingest_aisstream.py
   ```
   It will connect to `wss://stream.aisstream.io/v0/stream`, subscribe to the California bounding box, and process `PositionReport` (and related) messages. Foreground mode: stop with Ctrl+C. On disconnect it will try to reconnect.

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
| Start stack | `docker compose up --build -d` (frontend in Docker on port 3000) |
| Import MPAs | `docker compose exec backend python scripts/import_mpas.py` |
| Ingest AIS | AISStream: `COMPOSE_PROFILES=ais` / `make up-ais`, or `docker compose exec backend python scripts/ingest_aisstream.py`. Sample: `docker compose exec backend python scripts/ingest_ais.py --source scripts/sample_ais.json`. Or `curl -X POST http://localhost:8000/vessels/update ...` |
| Frontend (dev) | Optional: `cd frontend && npm install && npm run dev` → http://localhost:3000 |
| UI | Map + zones, live vessels, “Show MPA violator trails”, vessel click → popup/trail, MPA click → stats popup |
| Backend tests | `cd backend && DATABASE_URL=... pytest tests/ -v` |
| API smoke | `curl http://localhost:8000/` and curl commands above |
