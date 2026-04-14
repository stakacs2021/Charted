# Self-hosting AIS MPA Monitor on Ubuntu

This guide matches the **implemented** layout in this repo: Docker Compose runs **PostGIS**, **FastAPI**, **Next.js (standalone)**, and optionally a dedicated **AISStream ingest** service. Postgres is bound to **127.0.0.1:5432** only. Use your **LAN IP** for browsers on other machines until you have a public DNS name.

## Architecture

- **Browser** → **Next.js** (port **3000**) and **FastAPI** (port **8000**), or optionally **Nginx** (port **80**) in front of both
- **Backend / ingest** → **PostgreSQL/PostGIS** on the Compose network (`db:5432`); Postgres is **not** exposed on the LAN (only `127.0.0.1:5432` on the host for backups/tools)

## 1. Prerequisites

- Ubuntu 22.04 or newer (other Linux distros work with equivalent packages)
- Docker Engine and Docker Compose plugin (`docker compose`)
- `git`
- Optional: `ufw` for firewall, `nginx` for a single port **80**, `certbot` when you have a domain later

Install Docker: [Docker Engine on Ubuntu](https://docs.docker.com/engine/install/ubuntu/).

## 2. Clone and configure

```bash
git clone <your-repo-url>
cd chartedaismonitor
cp .env.example .env
```

Edit `.env`:

| Variable | Purpose |
|----------|---------|
| `POSTGRES_PASSWORD` | Strong password; must match `DATABASE_URL` on the host and what Compose uses for `db` / `backend` / `ingest`. |
| `DATABASE_URL` | For **host** tools (`pg_dump`, local pytest): `postgresql://ais_user:YOUR_PASSWORD@127.0.0.1:5432/ais` |
| `NEXT_PUBLIC_API_URL` | **Before** building the frontend image: base URL **as the browser will use it**. LAN example: `http://192.168.1.50:8000` (replace with this machine’s IPv4). Same machine only: `http://127.0.0.1:8000`. |
| `AISSTREAM_API_KEY` | From [AISStream](https://aisstream.io/) — required if you enable the **ingest** profile. |
| `COMPOSE_PROFILES` | Set to `ais` to start the long-running AISStream ingester (see below). |

**Security:** Never commit `.env`.

## 3. Start the stack

```bash
docker compose up --build -d
docker compose exec backend python scripts/import_mpas.py
```

Verify the API:

```bash
curl -s http://localhost:8000/
```

Open the UI from another device on the LAN: `http://YOUR_LAN_IP:3000` (map, leaderboard). The UI calls the API at `NEXT_PUBLIC_API_URL`; if that URL is wrong or still `YOUR_LAN_IP`, rebuild the frontend (see below).

### Changing `NEXT_PUBLIC_API_URL` or `POSTGRES_PASSWORD`

- **API URL baked into the frontend:** Changing `NEXT_PUBLIC_API_URL` requires rebuilding the frontend image:
  ```bash
  docker compose build --no-cache frontend
  docker compose up -d frontend
  ```
- **Postgres password:** If you change `POSTGRES_PASSWORD` after the database volume already exists, credentials will not match unless you recreate the volume (destructive) or change the password inside Postgres manually. For a fresh start: `docker compose down -v` (drops DB data), then `up` again.

## 4. AIS ingest (AISStream)

The **ingest** service is behind the Compose profile **`ais`** so the stack can run without an API key.

**Foreground (testing):**

```bash
docker compose exec backend python scripts/ingest_aisstream.py
```

**Recommended — dedicated service:**

1. Set `AISSTREAM_API_KEY` in `.env`.
2. Add `COMPOSE_PROFILES=ais` to `.env` **or** run:
   ```bash
   COMPOSE_PROFILES=ais docker compose up -d --build
   ```

Ingest uses the same backend image and connects to `db` on the internal network.

## 5. Optional: Nginx on port 80 (single HTTP entry)

Use this when you want clients to open `http://YOUR_LAN_IP/` only (no `:3000` / `:8000` in the URL).

1. **Bind Compose to loopback** so only Nginx listens on the LAN:
   ```bash
   cp deploy/docker-compose.override.nginx.example.yml docker-compose.override.yml
   docker compose up -d --build
   ```
2. Set **`NEXT_PUBLIC_API_URL=/api`** (no trailing slash), then rebuild the frontend:
   ```bash
   docker compose build --no-cache frontend && docker compose up -d frontend
   ```
3. Install Nginx and install the site file:
   ```bash
   sudo cp deploy/nginx-aismonitor-ip.conf /etc/nginx/sites-available/chartedaismonitor
   sudo ln -sf /etc/nginx/sites-available/chartedaismonitor /etc/nginx/sites-enabled/
   sudo nginx -t && sudo systemctl reload nginx
   ```

The sample config proxies `/` → `127.0.0.1:3000` and `/api/` → `127.0.0.1:8000/`.

## 6. TLS (Let’s Encrypt) — when you have a domain

With DNS pointing at your server:

```bash
sudo apt install certbot python3-certbot-nginx
sudo certbot --nginx -d yourdomain.com
```

If you prefer a pre-made TLS site template in this repo, copy [`deploy/nginx-aismonitor-tls.conf`](deploy/nginx-aismonitor-tls.conf) into `/etc/nginx/sites-available/chartedaismonitor`, replace `yourdomain.com`, then run certbot.

For LAN-only or raw IP, skip TLS or use a self-signed cert (browsers will warn).

## 7. Firewall

```bash
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
# If not using Nginx, allow app ports for LAN access:
sudo ufw allow 3000/tcp
sudo ufw allow 8000/tcp
sudo ufw enable
```

Do **not** expose Postgres **5432** to the internet. The default Compose file binds Postgres to **127.0.0.1** only.

## 8. Backups

From the `chartedaismonitor` directory:

```bash
./scripts/backup-db.sh backup-$(date +%F).sql
```

Or manually:

```bash
docker compose exec db pg_dump -U ais_user ais > backup-$(date +%F).sql
```

Store backups off the machine.

## 9. Boot persistence

Compose services use `restart: unless-stopped`. Enable Docker on boot (distribution default on Ubuntu). Optional **systemd** unit:

```bash
# Edit paths in the file, then:
sudo cp deploy/chartedaismonitor.service.example /etc/systemd/system/chartedaismonitor.service
sudo systemctl daemon-reload
sudo systemctl enable --now chartedaismonitor.service
```

## 10. Checklist

- [ ] Strong `POSTGRES_PASSWORD`; `.env` not in Git  
- [ ] Postgres not exposed on `0.0.0.0:5432` (default is `127.0.0.1:5432` only)  
- [ ] `NEXT_PUBLIC_API_URL` matches how browsers reach the API; frontend rebuilt after changes  
- [ ] Nginx/TLS configured if exposed to the internet  
- [ ] Ingest running with `COMPOSE_PROFILES=ais` when using AISStream  
- [ ] Backups scheduled  

## Files added for self-hosting

- `docker-compose.yml` — `db`, `backend`, `frontend`, optional `ingest` (profile `ais`)
- `frontend/Dockerfile` — Next.js `standalone` image  
- `deploy/nginx-aismonitor-ip.conf` — Nginx example for IP / default server  
- `deploy/docker-compose.override.nginx.example.yml` — loopback-only ports for use with Nginx  
- `deploy/chartedaismonitor.service.example` — systemd template  
- `scripts/backup-db.sh` — `pg_dump` helper  
