# Deployment & Operations Guide (DevOps)

Audience: the engineer deploying and operating the Voice Agent Platform. Covers
**backend** (FastAPI API + LiveKit voice worker) and **frontend** (React/Vite
admin console), plus the infrastructure they depend on.

> For local development, see the root `Readme.md`. This document is about
> **production / staging** deployment.

---

## 1. Architecture at a glance

```
                         ┌─────────────────────────┐
   Browser / Phone  ───▶ │  Reverse proxy (nginx)   │  TLS termination
                         │  :443                    │
                         └───────┬─────────┬────────┘
                   static files  │         │  /api, /ws
                         ┌───────▼───┐ ┌───▼──────────────┐
                         │ Frontend  │ │ Backend API      │  uvicorn
                         │ dist/ (SPA)│ │ app.main:app     │  :8001
                         └───────────┘ └───┬───────┬──────┘
                                           │       │
                              ┌────────────▼┐ ┌────▼─────────┐
                              │ PostgreSQL  │ │ Redis        │
                              │ :5432       │ │ :6379        │
                              └─────────────┘ └──────────────┘

   ┌───────────────────────────┐        ┌─────────────────────┐
   │ LiveKit voice worker       │ ◀────▶ │ LiveKit server      │  :7880
   │ python -m app.livekit_agent│  rooms │ (SFU, media)        │
   └───────────────────────────┘        └─────────────────────┘
```

### Processes you must run

| Component | Process | Scales |
|-----------|---------|--------|
| REST API | `uvicorn app.main:app` | horizontally (stateless; sessions in Redis) |
| Voice worker | `python -m app.livekit_agent start` | horizontally (LiveKit load-balances jobs) |
| Frontend | static files from `dist/` | via CDN / nginx |

### Backing services

| Service | Port | Notes |
|---------|------|-------|
| PostgreSQL 16 | 5432 | Primary datastore. Use managed DB in prod. |
| Redis 7 | 6379 | Sessions + rate limiting. |
| LiveKit server | 7880 (ws), 7881 (tcp), 50000-50060/udp | Real-time media SFU. |

---

## 2. Host prerequisites

- Linux host (Ubuntu 22.04+ assumed in examples)
- Python 3.11
- Node.js 20+ (for building the frontend; not needed at runtime)
- Docker + Docker Compose (if using the container path)
- Reverse proxy: nginx (or Caddy/Traefik)
- Outbound HTTPS to the AI providers: DeepSeek, OpenRouter, Sarvam, Deepgram

---

## 3. Configuration & secrets

All backend config is environment-driven (see `.env.example` for the full list).
**Never commit `.env`** — it is git-ignored. In production, inject these via your
secret manager (Docker/K8s secrets, AWS SSM, Vault, etc.), not a file on disk
where avoidable.

### Must-change-before-production values

| Variable | Why |
|----------|-----|
| `SECRET_KEY` | JWT signing. Min 32 chars, random. |
| `ENCRYPTION_KEY` | AES-256 for tenant API tokens. **Exactly 32 bytes.** Rotating it invalidates all stored tenant tokens. |
| `DATABASE_URL` | Point at the managed Postgres. Keep the `postgresql+asyncpg://` scheme. |
| `REDIS_URL` | Point at the managed Redis. |
| `CORS_ORIGINS` | Must include the frontend's **production** origin (e.g. `https://console.example.com`). |
| `DEBUG` | Set `false`. |
| `ENVIRONMENT` | `production`. |
| `LIVEKIT_URL` / `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET` | Real LiveKit cluster creds (see §7). The dev `devkey/secret` is **not** for production. |
| `DEEPSEEK_API_KEY`, `OPENROUTER_API_KEY`, `SARVAM_API_KEY`, `DEEPGRAM_API_KEY` | Provider keys. |

Generate strong secrets:
```bash
# SECRET_KEY (any length ≥ 32)
openssl rand -hex 32
# ENCRYPTION_KEY — must be exactly 32 bytes; use a 32-char value
openssl rand -base64 24 | cut -c1-32
```

> **Dependency pin, do not "fix":** `bcrypt==4.0.1` is pinned on purpose
> (see the comment in `requirements.txt`). bcrypt ≥ 4.1 breaks passlib and
> makes register/login return 500. Do not bump it during an upgrade sweep.

---

## 4. Backend deployment — bare-metal / VM (systemd)

```bash
# 1. Code + virtualenv
git clone <repo> /opt/voice-agent-backend
cd /opt/voice-agent-backend
python3.11 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
pip install gunicorn            # recommended process manager for the API

# 2. Environment
cp .env.example .env            # then edit with production values (§3)

# 3. Database schema
alembic upgrade head

# 4. First super-admin (once per environment)
python create_super_admin.py
```

### API service — `/etc/systemd/system/voice-agent-api.service`
```ini
[Unit]
Description=Voice Agent API
After=network.target

[Service]
WorkingDirectory=/opt/voice-agent-backend
EnvironmentFile=/opt/voice-agent-backend/.env
ExecStart=/opt/voice-agent-backend/.venv/bin/gunicorn app.main:app \
    --worker-class uvicorn.workers.UvicornWorker \
    --workers 4 \
    --bind 127.0.0.1:8001 \
    --timeout 60
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```
> Pick `--workers` ≈ `2 × CPU cores`. Workers are stateless — session state
> lives in Redis — so scaling out is safe.

### Voice worker service — `/etc/systemd/system/voice-agent-worker.service`
```ini
[Unit]
Description=Voice Agent LiveKit Worker
After=network.target

[Service]
WorkingDirectory=/opt/voice-agent-backend
EnvironmentFile=/opt/voice-agent-backend/.env
ExecStart=/opt/voice-agent-backend/.venv/bin/python -m app.livekit_agent start
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```
> Use `start` (production), not `dev` (auto-reload). Run one or more worker
> instances; LiveKit distributes jobs across all connected workers.

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now voice-agent-api voice-agent-worker
sudo systemctl status voice-agent-api voice-agent-worker
```

---

## 5. Backend deployment — Docker

No Dockerfile ships in the repo yet. A minimal production one:

```dockerfile
# Dockerfile  (place in Voice-Agent-backend/)
FROM python:3.11-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn
COPY . .
EXPOSE 8001
CMD ["gunicorn", "app.main:app", \
     "--worker-class", "uvicorn.workers.UvicornWorker", \
     "--workers", "4", "--bind", "0.0.0.0:8001", "--timeout", "60"]
```

The API and the worker share the image but run different commands. Example
`docker-compose.prod.yml` fragment:

```yaml
services:
  api:
    build: .
    env_file: .env
    ports: ["8001:8001"]
    depends_on: [postgres, redis]

  worker:
    build: .
    env_file: .env
    command: python -m app.livekit_agent start
    depends_on: [api, livekit]
```

Run migrations as a one-shot before starting the API:
```bash
docker compose -f docker-compose.prod.yml run --rm api alembic upgrade head
docker compose -f docker-compose.prod.yml run --rm api python create_super_admin.py
```

> The committed `docker-compose.yml` runs Postgres/Redis/LiveKit in **dev
> mode** (LiveKit `--dev`, hard-coded DB password). Use managed services or a
> hardened compose/K8s manifest for production — do not ship dev mode.

---

## 6. Database migrations

- Schema changes are Alembic migrations in `alembic/versions/`.
- Apply on every deploy **before** starting/replacing the API:
  ```bash
  alembic upgrade head
  ```
- Roll back one step: `alembic downgrade -1`.
- Run migrations as a single job, not from every API worker, to avoid races.

---

## 7. LiveKit server (real-time voice)

The dev `docker-compose.yml` starts `livekit-server --dev` with keys
`devkey: secret`. **That is for local only.** For production either:

- **LiveKit Cloud** — create a project, use its URL (`wss://<proj>.livekit.cloud`)
  and API key/secret; or
- **Self-hosted LiveKit** — deploy with real keys, TLS, and the UDP media port
  range open (`50000-50060/udp` by default) on the firewall / security group.

Then set on the backend (API **and** worker):
```
LIVEKIT_URL=wss://your-livekit-host
LIVEKIT_API_KEY=...
LIVEKIT_API_SECRET=...
```

> UDP media ports must be reachable end-to-end or audio will fail even though
> signalling (WebSocket) connects.

---

## 8. Frontend deployment

Vite env vars are **baked in at build time**, so set `VITE_API_URL` before
building — changing it later requires a rebuild.

```bash
cd Voice-Agent-frontend
npm ci                                   # reproducible install from lockfile
VITE_API_URL=https://api.example.com \   # your public backend origin
  npm run build                          # type-checks + outputs to dist/
```

Serve `dist/` as static files behind the same domain, with SPA fallback so
client-side routes resolve to `index.html`.

> If you serve the frontend and API on the **same origin** and proxy `/api` +
> `/ws` to the backend (as in §9), you can leave `VITE_API_URL` empty and the
> app will use relative paths — no CORS needed.

---

## 9. Reverse proxy (nginx)

Single-origin setup: static SPA + proxied API/WebSocket. Example:

```nginx
server {
    listen 443 ssl http2;
    server_name console.example.com;

    # TLS certs (certbot / your CA) ...
    ssl_certificate     /etc/letsencrypt/live/console.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/console.example.com/privkey.pem;

    # Frontend static build
    root /var/www/voice-agent-frontend/dist;
    index index.html;

    location / {
        try_files $uri $uri/ /index.html;   # SPA fallback
    }

    # REST API
    location /api/ {
        proxy_pass http://127.0.0.1:8001;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # Voice WebSocket
    location /ws/ {
        proxy_pass http://127.0.0.1:8001;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 3600s;           # long-lived voice sockets
    }
}
```

If frontend and API live on **different** domains instead, skip the proxy and
set `CORS_ORIGINS` to the frontend origin and `VITE_API_URL` to the API origin.

---

## 10. Health checks & monitoring

- **Liveness/readiness:** `GET /health` returns DB + Redis status. Wire it into
  your load balancer / K8s probes and uptime monitor.
- **Docs (lock down in prod):** `/docs`, `/redoc` are enabled; restrict or
  disable on public environments if you don't want the schema exposed.
- **Logs:** the app uses `structlog` (JSON). Ship stdout/stderr to your log
  aggregator (CloudWatch, Loki, ELK). `journalctl -u voice-agent-api -f` for
  systemd.
- **Worker health:** watch that `voice-agent-worker` stays `active`; a crashed
  worker means no new voice calls are answered even while the API is up.

---

## 11. Backups & data

- **PostgreSQL** is the source of truth — tenants, users, tools, analytics,
  conversation logs. Enable automated backups / PITR on the managed DB.
- **Redis** holds ephemeral sessions + rate-limit counters — safe to lose, but
  active voice sessions drop on flush.
- **`ENCRYPTION_KEY`** must be backed up alongside the DB: tenant API tokens are
  encrypted with it, and losing it means every tenant must re-enter credentials.

---

## 12. Deploy checklist

- [ ] `.env` populated from secret manager; `DEBUG=false`, `ENVIRONMENT=production`
- [ ] `SECRET_KEY` and 32-byte `ENCRYPTION_KEY` set and backed up
- [ ] `CORS_ORIGINS` includes the real frontend origin (if cross-origin)
- [ ] Managed Postgres + Redis reachable; `DATABASE_URL` / `REDIS_URL` set
- [ ] `alembic upgrade head` run
- [ ] Super-admin created (first environment only)
- [ ] API service up and `/health` green
- [ ] LiveKit worker service up; LiveKit server reachable; UDP media ports open
- [ ] Frontend built with correct `VITE_API_URL` and deployed to `dist/` host
- [ ] nginx TLS valid; `/`, `/api`, `/ws` all route correctly
- [ ] Backups + log shipping + uptime alerts configured
