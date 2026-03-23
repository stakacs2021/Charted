# Self-hosting AIS MPA Monitor on Ubuntu

This guide describes running the stack on an Ubuntu PC (22.04+): Docker Compose for PostGIS and the API, optional Nginx reverse proxy and TLS, and a separate long-running AIS ingest process.

## Architecture

- **Browser** → **Nginx** (optional) → **Next.js** (frontend) and **FastAPI** (backend API)
- **Backend** → **PostgreSQL/PostGIS** (only on a private Docker network; do not expose Postgres to the public internet in production)

## 1. Prerequisites

- Ubuntu 22.04 or newer
- Docker Engine and Docker Compose plugin (`docker compose`)
- `git`
- Optional: `ufw` for firewall, `certbot` for Let’s Encrypt TLS

Install Docker (follow [Docker’s official docs](https://docs.docker.com/engine/install/ubuntu/)).

## 2. Clone and configure

```bash
git clone <your-repo-url>
cd chartedaismonitor
cp .env.example .env
```

Edit `.env`:

- `DATABASE_URL` — For apps **on the host** talking to Postgres **published on localhost**:  
  `postgresql://ais_user:YOUR_STRONG_PASSWORD@localhost:5432/ais`  
  Match user/password/database to what you set for Postgres (see below).
- `AISSTREAM_API_KEY` — From [AISStream](https://aisstream.io/) if you use WebSocket ingest.
- `NEXT_PUBLIC_API_URL` — When the browser loads the frontend from another host or path, set this to the **public URL of your API** (e.g. `https://yourdomain.com/api` or `http://your-server-ip:8000`).

**Security:** Never commit `.env`. Keep secrets out of Git.

## 3. Harden `docker-compose.yml` for production

Recommended changes:

1. **Postgres password** — Replace the example password with a strong value and pass it via `.env` or Compose `environment` (parameterize `POSTGRES_PASSWORD` and `DATABASE_URL` consistently).
2. **Do not publish Postgres to the world** — For production, either:
   - Remove the `ports: "5432:5432"` mapping from the `db` service so only containers on the Compose network can reach Postgres, or  
   - Bind to localhost only: `"127.0.0.1:5432:5432"` if you need host access for backups.
3. **Backend** — Keep `8000:8000` only behind Nginx or firewall-restricted if possible.

Rebuild after code changes:

```bash
docker compose up --build -d
```

## 4. Run database and API

```bash
docker compose up --build -d
docker compose exec backend python scripts/import_mpas.py
```

Verify: `curl -s http://localhost:8000/` → JSON status.

## 5. Frontend in production

The repo’s Compose file may only include `db` and `backend`. Run the frontend in one of these ways:

### Option A: Node on the host

```bash
cd frontend
npm ci
npm run build
npm run start
```

Default Next.js listens on port 3000. Set `NEXT_PUBLIC_API_URL` before `npm run build` so the client bundle points at your API.

### Option B: Next.js standalone in Docker

Add a `frontend` service to Compose (or a separate Dockerfile) that runs `npm run build` and `node .next/standalone/server.js` (enable `output: 'standalone'` in `next.config.js` if you use this pattern).

### Option C: Static export

If you switch to `output: 'export'`, serve the `out/` directory with Nginx. (Map routes and env usage must be compatible with static export.)

## 6. Nginx reverse proxy (example)

Install Nginx on the host. Example server block (HTTP) — adjust `server_name` and upstream ports:

```nginx
server {
    listen 80;
    server_name yourdomain.com;

    location / {
        proxy_pass http://127.0.0.1:3000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /api/ {
        proxy_pass http://127.0.0.1:8000/;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

If the frontend expects the API at `http://localhost:8000`, either:

- Proxy `/api` to the backend and set `NEXT_PUBLIC_API_URL` to `https://yourdomain.com/api`, or  
- Serve the API on a subdomain (e.g. `api.yourdomain.com`) and set `NEXT_PUBLIC_API_URL` accordingly.

Reload Nginx: `sudo nginx -t && sudo systemctl reload nginx`.

## 7. TLS (Let’s Encrypt)

With a public DNS name pointing to your server:

```bash
sudo apt install certbot python3-certbot-nginx
sudo certbot --nginx -d yourdomain.com
```

For LAN-only use, you can skip TLS or use a self-signed certificate (browsers will warn).

## 8. Firewall

```bash
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
```

Do not open Postgres (5432) to the public internet.

## 9. AIS ingest (long-running)

Run the WebSocket ingester **outside** the API process so restarts of the API do not stop ingestion. Examples:

**Foreground (testing):**

```bash
docker compose exec backend python scripts/ingest_aisstream.py
```

**Dedicated Compose service (recommended):** add a service using the same backend image:

```yaml
  ingest:
    build: ./backend
    env_file: .env
    environment:
      DATABASE_URL: postgresql://ais_user:ais_pass@db:5432/ais
    command: python scripts/ingest_aisstream.py
    depends_on:
      db:
        condition: service_healthy
    restart: unless-stopped
```

Or a **systemd** unit that runs `docker compose run --rm ingest` or the script on the host with `DATABASE_URL` pointing at `localhost` if Postgres is published locally.

## 10. Backups

Schedule periodic dumps:

```bash
docker compose exec db pg_dump -U ais_user ais > backup-$(date +%F).sql
```

Store backups off the machine you use for development.

## 11. Boot persistence

Use `restart: unless-stopped` on Compose services. Optionally enable Docker to start on boot and add a systemd unit that runs `docker compose up -d` in your project directory after network is online.

## Checklist

- [ ] Strong database password; `.env` not in Git  
- [ ] Postgres not exposed publicly  
- [ ] `NEXT_PUBLIC_API_URL` matches how browsers reach the API  
- [ ] Nginx/TLS configured if exposed to the internet  
- [ ] Ingest running continuously with `restart` policy  
- [ ] Backups scheduled  
