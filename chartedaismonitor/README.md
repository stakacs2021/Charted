# AIS MPA Monitor

Web app that monitors vessel locations near the California coast and checks whether they are inside California Marine Protected Areas (MPAs).

**Stack:** Backend (Python + FastAPI), DB (PostgreSQL + PostGIS in Docker), Frontend (Next.js, TypeScript only), Map (MapLibre GL).

---

## Data source (California MPAs)

**Chosen source:** [California Marine Protected Areas [ds582]](https://data.ca.gov/dataset/california-marine-protected-areas-ds582), California Open Data / CDFW.

- **GeoJSON URL:** `https://data-cdfw.opendata.arcgis.com/api/download/v1/items/117a99c8745a48c6a48bac70005b1b11/geojson?layers=0`
- **Why this one:** Official CDFW dataset, single comprehensive GeoJSON, maintained (updated Jan 2025), no signup. Alternatives (e.g. shapefile-only or by-region) are either not GeoJSON or require extra conversion.

---

## Database schema

- **zones:** `id` (serial pk), `name` (text), `designation` (text, nullable), `geom` (MULTIPOLYGON, SRID 4326), `source` (text), `created_at` (timestamptz). GIST index on `geom`.
- **vessels:** `mmsi` (text pk), `name` (text), `last_lat`, `last_lon`, `last_ts` (timestamptz).

See `db/init.sql` for full DDL.

---

## Run end-to-end

### 1. Environment

```bash
cp .env.example .env
# Edit .env if needed (default: postgresql://ais_user:ais_pass@localhost:5432/ais)
```

### 2. Start DB and backend (Docker)

```bash
docker compose up --build -d
```

This starts PostGIS (`db`) and the FastAPI backend. Backend uses hostname `db` and `DATABASE_URL=postgresql://ais_user:ais_pass@db:5432/ais` inside Docker.

### 3. Import California MPAs

Run the import script **after** the DB is up. Either inside the backend container or on the host with DB on localhost:

**Option A – run inside backend container (recommended):**

```bash
docker compose exec backend python scripts/import_mpas.py
```

**Option B – run on host (requires Python deps and DB on localhost:5432):**

```bash
cd backend
pip install -r requirements.txt
export DATABASE_URL=postgresql://ais_user:ais_pass@localhost:5432/ais
python scripts/import_mpas.py
```

**Optional – use a local GeoJSON file instead of download:**

```bash
docker compose exec backend python scripts/import_mpas.py /path/to/ca_mpas.geojson
```

### 4. Start frontend

```bash
cd frontend
npm install
npm run dev
```

Open http://localhost:3000, then go to **View map (California MPAs)**. The map fetches `GET /zones` from the backend; set `NEXT_PUBLIC_API_URL=http://localhost:8000` in `.env` if the frontend is not on the same host as the API.

---

## API summary

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/zones` | All MPAs as GeoJSON FeatureCollection |
| GET | `/inside?lat=<lat>&lon=<lon>` | Whether point is inside any MPA; returns `inside` (bool) and `matched_zones` |
| POST | `/vessels/update` | Body: `{ "mmsi", "name?", "lat", "lon" }` – update vessel position |
| GET | `/vessels/{mmsi}/inside` | Whether vessel’s last position is inside any MPA |

**Point-in-polygon:** The app uses **ST_Intersects** so that points on the boundary of an MPA count as “inside” (consistent and conservative for “in or on” the zone).

---

## Example curl commands

**1. Point inside a known MPA (e.g. Point Lobos SMR area, ~36.52°N, 121.95°W):**

```bash
curl -s "http://localhost:8000/inside?lat=36.52&lon=-121.95"
```

Expected shape: `{"lat":36.52,"lon":-121.95,"inside":true,"matched_zones":[{"id":...,"name":"..."}]}` (after import).

**2. Point outside MPAs (e.g. San Francisco Bay, ~37.8°N, 122.4°W):**

```bash
curl -s "http://localhost:8000/inside?lat=37.8&lon=-122.4"
```

Expected: `"inside":false,"matched_zones":[]`.

**3. Update a vessel and check if it’s inside an MPA:**

```bash
# Set position inside an MPA
curl -s -X POST http://localhost:8000/vessels/update \
  -H "Content-Type: application/json" \
  -d '{"mmsi":"123456789","name":"Test Vessel","lat":36.52,"lon":-121.95}'

# Check if that vessel is inside any MPA
curl -s "http://localhost:8000/vessels/123456789/inside"
```

---

## Geospatial tooling

The MPA import uses **pure Python + psycopg2 + shapely** (and httpx for download). No ogr2ogr. This keeps the project easy to run in class environments (no GDAL install), and shapely’s `make_valid` plus PostGIS `ST_MakeValid` handle invalid polygons from the source.
