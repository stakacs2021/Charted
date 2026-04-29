# Testing the MPA-violation suppression pipeline

This guide covers everything you need to verify the four-layer suppression pipeline (heuristics, type policy, per-MMSI allowlist, DB trigger) before, during, and after a rollout — including how to validate it on a live/production deployment **without contaminating real data or interrupting the running ingest**.

```
docs link map: README.md (top-level)
                 ├── SELF_HOST.md   (Ubuntu deploy + nginx + systemd)
                 ├── CONTRIBUTING.md (git cadence)
                 └── TESTING.md     (you are here)
```

---

## 1. Test inventory

Tests live in [`backend/tests/`](backend/tests/). They split into two groups by infrastructure requirement:

### 1a. No-DB unit tests (fast, hermetic)

| File | What it covers |
|---|---|
| [`tests/test_aisstream_parsing.py`](backend/tests/test_aisstream_parsing.py) | `parse_ais_sog_kn` sentinel handling; `message_to_record` extracts SOG / type from `ExtendedClassBPositionReport`; `message_to_static_record` returns a `StaticVesselRecord` for `ShipStaticData` only. |
| [`tests/test_enrich_vessel_types.py`](backend/tests/test_enrich_vessel_types.py) | Name-regex classifier (`USCGC`, `R/V`, `F/V`, `PILOT`, `FERRY`, etc.); CSV loader skips comments/headers/blanks. |
| [`tests/test_seed_policy.py`](backend/tests/test_seed_policy.py) | Builds the `zone_vessel_type_policy` matrix correctly; transit-legal defaults; `--strict-unknown` flag; no duplicate `(bracket, bucket)` rows. |
| [`tests/test_zone_classification.py`](backend/tests/test_zone_classification.py) | (Pre-existing.) `classify_bracket` maps SMR/SMCA/SMP designations; unknown designations stay stable. |

These are **safe to run anywhere**: no network, no DB, no docker. They'll catch most regressions in the parsers and rule builders.

### 1b. DB-integration tests (require a live PostGIS)

| File | What it covers |
|---|---|
| [`tests/test_ingest_entry_detection.py`](backend/tests/test_ingest_entry_detection.py) | The full pipeline end-to-end: <br>• `test_outside_to_inside_creates_single_entry_violation` — baseline entry detection (heuristics monkeypatched off). <br>• `test_high_speed_transit_does_not_violate` — Layer 1 speed gate. <br>• `test_short_dwell_does_not_violate` / `test_long_dwell_records_violation` — Layer 1 dwell gate (defer-then-fire semantics). <br>• `test_type_policy_allows_passenger_in_smr_via_unknown_bucket` — Layer 2 transit-legal policy. <br>• `test_type_policy_denies_fishing_in_smr` — Layer 2 fishing-deny in NoTake. <br>• `test_wildcard_allowlist_suppresses_violation` — Layer 3 `(mmsi, zone_id=NULL)` wildcard wins over even fishing-in-SMR. <br>• `test_unknown_vessel_type_treated_as_unknown_bucket` — NULL `vessel_type` collapses to `'unknown'` bucket. |
| [`tests/test_history_mpa_entries.py`](backend/tests/test_history_mpa_entries.py) | (Pre-existing.) `mpa_violations`/`zones` join shape used by `/history/mpa-entries`. |

All DB tests use a fixture that **prefixes test rows** (`mmsi LIKE 'test-mmsi-%'`, `zones.source = 'tests'`) and tears them down in a `finally:` block. They will not pollute production data **as long as you point them at a non-production database** (see §3).

---

## 2. Run tests locally (developer workstation)

### 2a. Just the no-DB tests

These need only Python + pytest. No DB, no docker:

```bash
cd chartedaismonitor/backend
pip install -r requirements.txt
pytest tests/test_aisstream_parsing.py \
       tests/test_enrich_vessel_types.py \
       tests/test_seed_policy.py \
       tests/test_zone_classification.py -v
```

Expected: **15 passed**.

### 2b. The full suite against a local docker-compose database

```bash
cd chartedaismonitor
cp .env.example .env       # set POSTGRES_PASSWORD
docker compose up -d db backend
docker compose exec backend python scripts/import_mpas.py    # zones (idempotent)

# Two ways to run pytest:
# (1) On the host, against the published 127.0.0.1:5432 port
cd backend
export DATABASE_URL=postgresql://ais_user:YOUR_PASSWORD@127.0.0.1:5432/ais
pytest tests/ -v

# (2) Inside the backend container (host = "db")
docker compose exec backend sh -c 'cd /app && pytest tests/ -v'
```

Expected: **23 passed** (15 unit + 8 integration).

---

## 3. Test against a live deployment WITHOUT interfering with it

The pipeline is designed for hot-deployment. Schema additions are all `ADD COLUMN IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS`, applied automatically by `ensure_core_schema` (ingest) and `ensure_extended_schema` (FastAPI startup). You don't need a maintenance window. But you should still verify behavior **without writing test rows to the production database** and **without touching the running ingest**.

There are three escalating tactics, pick one:

### 3a. Read-only smoke checks against the live API (zero risk)

Safe to run any time. Hits real endpoints; no writes:

```bash
HOST=https://your-domain.example   # or http://localhost:8000

# Layer 0 — vessel-type coverage. Watch this trend up after the rollout.
curl -s "$HOST/admin/vessel-type-coverage" | jq .

# Layer 3 — confirm wildcard allowlist rows are present where you seeded them.
curl -s "$HOST/admin/violators/review?limit=20" | jq '.[] | {mmsi, name, vessel_type, violation_count, allowlisted_global}'

# Verify the trigger function exists and the policy table is populated.
curl -s "$HOST/debug/stats" | jq .

# Recent entry events (sanity-check that violations are still being recorded for cases you DO want flagged)
curl -s "$HOST/history/mpa-entries?limit=20" | jq '.[] | {entry_ts, mmsi, zone_name, source}'
```

Verifying the **rate** of new violations dropped after seeding:

```bash
# Compare before vs after seeding policy + allowlist:
date -u +"%FT%TZ"        # T0
docker compose exec backend python scripts/seed_policy.py
docker compose exec backend python scripts/seed_allowlist.py
# wait 10–30 minutes
curl -s "$HOST/history/mpa-entries/window?start_ts=$T0&end_ts=$(date -u +%FT%TZ)&limit=5000" | jq 'length'
```

### 3b. Dry-run all writers before they run for real

Every seed/enrichment script supports `--dry-run`. Use it on the live DB to preview without writing:

```bash
# Vessel-type backfill (CSV + name-regex)
docker compose exec backend python scripts/enrich_vessel_types.py --dry-run

# Institutional allowlist
docker compose exec backend python scripts/seed_allowlist.py --dry-run

# (seed_policy.py is idempotent and only ever upserts ~25 rows; preview by inspecting the SQL it would emit:)
docker compose exec backend python -c "from scripts.seed_policy import build_rows; \
  [print(r) for r in build_rows(strict_unknown=False)]"
```

Each prints the row count and the planned changes. Run again without `--dry-run` once the plan looks right.

### 3c. Run the full pytest suite against a sidecar test database (cleanest)

This is the right answer when you want **the integration tests** to pass against a deployment-like environment without touching the production DB or the live ingest.

**Recipe** — spin up a second Postgres on a different port, with the same schema, and point pytest at it:

```bash
# 1. Sidecar Postgres on host-port 5433 (port-isolated from the live 5432).
docker run -d --name ais-pg-test \
  -e POSTGRES_DB=ais -e POSTGRES_USER=ais_user -e POSTGRES_PASSWORD=testpw \
  -p 127.0.0.1:5433:5432 \
  -v "$(pwd)/db/init.sql":/docker-entrypoint-initdb.d/01-init.sql \
  postgis/postgis:15-3.4

# 2. Wait for it to come up, then pop a shell that points at the sidecar.
until docker exec ais-pg-test pg_isready -U ais_user -d ais; do sleep 1; done

# 3. Run pytest against it. Same image as production, schema is identical
#    because db/init.sql is the canonical source.
cd chartedaismonitor/backend
pip install -r requirements.txt
DATABASE_URL=postgresql://ais_user:testpw@127.0.0.1:5433/ais pytest tests/ -v

# 4. Tear down. The live DB and ingest were never touched.
docker rm -f ais-pg-test
```

**Why this is interference-free:**

- Different host port (`5433`), different container name, different volume — the live `db` service on `5432` is untouched.
- Tests use `mmsi LIKE 'test-mmsi-%'` and `zones.source = 'tests'`, but even if a test bug leaked rows, they'd live in the sidecar that gets `docker rm -f`-ed.
- The live `backend` and `ingest` containers keep running, reading/writing the production DB on `5432` exclusively.

**If you want to test against a snapshot of real data** — extend step 1 with a `pg_dump | pg_restore` from a fresh production backup before pytest:

```bash
# Capture (uses scripts/backup-db.sh, which dumps from 127.0.0.1:5432)
./scripts/backup-db.sh
# Restore into the sidecar
docker exec -i ais-pg-test pg_restore -U ais_user -d ais < latest_backup.dump
```

That gives you realistic vessel/violation distributions for testing the seed scripts and admin endpoints without the risk of mutating the source.

---

## 4. Rollout / canary plan

A safe deployment order so you can roll back at each step:

1. **Pre-flight (read-only, on production):**
   ```bash
   curl -s "$HOST/admin/vessel-type-coverage" | jq .   # baseline
   curl -s "$HOST/history/mpa-entries?limit=1" | jq .[0].entry_ts   # last violation timestamp
   ```
2. **Sidecar pytest** (§3c) — confirm 23 tests pass against schema + a snapshot.
3. **Roll the backend image only.** The new schema migrations run idempotently in `ensure_extended_schema` on FastAPI startup; existing data is untouched.
   ```bash
   docker compose build backend && docker compose up -d backend
   docker compose logs --tail=50 backend | grep -i error    # should be clean
   ```
   At this point new heuristics, the new `is_vessel_allowed_in_zone` function, and the wildcard allowlist are live, but `zone_vessel_type_policy` is still empty so the trigger denies-by-default — **no behavior change yet**.
4. **Seed the policy.** This is the moment violations start being suppressed for non-fishing transit. Easy to revert (DELETE FROM `zone_vessel_type_policy`).
   ```bash
   docker compose exec backend python scripts/seed_policy.py
   curl -s "$HOST/admin/violators/review?limit=10" | jq    # pick a non-fishing top violator -- their entries should stop appearing
   ```
5. **Roll the ingest image.** Pulls in the new `process_batch` heuristics + `ShipStaticData` handler.
   ```bash
   docker compose build ingest && docker compose up -d ingest
   docker compose logs --tail=50 ingest | grep -iE 'error|reconnect'
   ```
6. **Seed institutional allowlist** (after curating CSVs against §3a's review endpoint).
   ```bash
   docker compose exec backend python scripts/seed_allowlist.py
   ```
7. **Backfill vessel types** for vessels already in the DB:
   ```bash
   docker compose exec backend python scripts/enrich_vessel_types.py --dry-run
   docker compose exec backend python scripts/enrich_vessel_types.py
   ```
8. **Watch coverage trend.** Re-run §3a's curl every few hours/days until `coverage_pct` stabilizes; consider `seed_policy.py --strict-unknown` once `unknown` is a small minority of *violators*.

### Rollback at any step

| If this breaks… | Reverse with |
|---|---|
| Heuristics too aggressive (too few violations) | Set `MPA_SPEED_TRANSIT_KN=9999`, `MPA_MIN_DWELL_SECONDS=0`, `MPA_BOUNDARY_BUFFER_M=0` in `.env` and `docker compose up -d backend ingest`. No DB change required. |
| Type policy too permissive | `DELETE FROM zone_vessel_type_policy WHERE allowed = TRUE;` (or `TRUNCATE`). The trigger then defaults to deny again, restoring legacy behavior. |
| Wrong MMSI added to allowlist | `DELETE FROM mpa_violation_allowlist WHERE mmsi = 'X';` |
| Whole rollout suspect | Re-deploy the previous backend image; the `IF NOT EXISTS` migrations leave existing rows alone, and the trigger defers to allowlist + policy without breaking anything. |

---

## 5. Manual / curl smoke checklist

Run after each step in the rollout. Each must succeed against the live API:

```bash
HOST=http://localhost:8000

# 1. Sanity / health
curl -fsS $HOST/                    # {"status":"AIS MPA Monitor running"}
curl -fsS $HOST/debug/stats          # zone_count > 0, vessel_count > 0

# 2. Map data still flows
curl -fsS "$HOST/zones/with-stats" | jq '.features | length'    # > 0
curl -fsS "$HOST/vessels/live?limit=5" | jq 'length'            # >= 0

# 3. Suppression pipeline endpoints (new in this rollout)
curl -fsS "$HOST/admin/vessel-type-coverage" | jq .
curl -fsS "$HOST/admin/violators/review?limit=10" | jq '. | length'

# 4. History flow unchanged
curl -fsS "$HOST/history/mpa-entries?limit=5" | jq '. | length'
```

If any of those return non-200, abort and roll back.

---

## 6. Quick reference: thresholds & knobs

| Knob | Default | Where set | Effect |
|---|---|---|---|
| `MPA_SPEED_TRANSIT_KN` | `5.0` | `.env` (read in `ingest_ais.py`) | SOG above this is treated as transit (no violation). |
| `MPA_MIN_DWELL_SECONDS` | `120` | `.env` | Vessel must remain inside this long before recording. `0` disables debounce. |
| `MPA_BOUNDARY_BUFFER_M` | `30` | `.env` | Skip recording if position is within this many meters of zone edge. `0` disables. |
| `zone_vessel_type_policy` | seeded by `scripts/seed_policy.py` | DB (live-editable) | Per `(bracket, bucket)` allow/deny. |
| `mpa_violation_allowlist` | seeded by `scripts/seed_allowlist.py` | DB (live-editable) | Per-MMSI override. `zone_id IS NULL` = all zones. |

Re-run the seed scripts whenever the CSVs change. They're idempotent.
