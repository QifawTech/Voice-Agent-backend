# 🎤 Voice Agent Platform — Backend

AI-powered voice ordering agent platform for restaurants. Enables customers to browse menus, place orders, and check order status via natural voice conversations.

## Tech Stack

| Layer | Technology |
|-------|-----------|
| **Framework** | Python 3.11 + FastAPI |
| **Database** | PostgreSQL 16 (async via asyncpg) |
| **Cache/Sessions** | Redis 7 |
| **ORM** | SQLAlchemy 2.0 (async) |
| **Migrations** | Alembic |
| **AI/LLM** | DeepSeek (function calling) |
| **Auth** | JWT + API Keys |
| **Logging** | structlog (structured JSON) |

## Quick Start

### 1. Prerequisites
- [Anaconda/Miniconda](https://docs.conda.io/en/latest/miniconda.html)
- [Docker Desktop](https://www.docker.com/products/docker-desktop/)

### 2. Start Infrastructure
```bash
# Start PostgreSQL + Redis via Docker
docker-compose up -d

# Verify containers are running
docker-compose ps
```

### 3. Set Up Python Environment
```bash
# Create conda environment
conda create -n voice-agent python=3.11 -y

# Activate
conda activate voice-agent

# Install dependencies
pip install -r requirements.txt
```

### 4. Configure Environment
```bash
# Copy example env file
cp .env.example .env

# Edit .env with your values (database URL is pre-configured for Docker)
```

### 5. Run Database Migrations
```bash
alembic upgrade head
```

### 6. Start Development Server
```bash
uvicorn app.main:app --reload --port 8000
```

### 7. Verify
- **API Root:** http://localhost:8000
- **Health Check:** http://localhost:8000/health
- **Swagger Docs:** http://localhost:8000/docs
- **ReDoc:** http://localhost:8000/redoc

## Project Structure

```
Voice-Agent-backend/
├── app/
│   ├── main.py              # FastAPI app entry point
│   ├── config.py            # Settings (pydantic-settings)
│   ├── database.py          # Async SQLAlchemy engine
│   ├── redis_client.py      # Redis async connection
│   ├── logger.py            # Structured logging
│   ├── models/              # SQLAlchemy ORM models
│   ├── schemas/             # Pydantic request/response schemas
│   ├── routes/              # API route handlers
│   ├── middleware/          # Auth, tenant resolver, rate limiter
│   └── services/            # Business logic
├── alembic/                 # Database migrations
├── docker-compose.yml       # PostgreSQL + Redis
├── requirements.txt         # Python dependencies
├── .env.example             # Environment template
└── alembic.ini              # Migration config
```

## API Endpoints

### System
| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | API root info |
| `GET` | `/health` | Health check (DB + Redis status) |
| `GET` | `/docs` | Swagger UI |
| `GET` | `/redoc` | ReDoc documentation |

### Auth (Day 4)
| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/auth/register` | Register admin user |
| `POST` | `/auth/login` | Login, get JWT |
| `GET` | `/auth/me` | Get current user |

### Tenants (Day 5)
| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/tenants` | Register restaurant |
| `GET` | `/tenants` | List restaurants |
| `GET` | `/tenants/:id` | Get restaurant details |
| `PUT` | `/tenants/:id` | Update restaurant |
| `POST` | `/tenants/:id/test-api` | Test API connection |

### Voice (Day 10)
| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/voice/session` | Create session |
| `POST` | `/voice/message` | Send message |
| `WS` | `/ws/voice` | Real-time voice |

## Docker Services

| Service | Port | Credentials |
|---------|------|-------------|
| PostgreSQL | `5432` | `voice_agent_user` / `voice_agent_pass_2026` |
| Redis | `6379` | No auth (local dev) |

## License

Proprietary — Qifaw Technologies
---

## Multi-Tenant Accounts

An account chooses its architecture at signup:

- **Single tenant** — one product, no sub-tenants. Configuration lives on the tenant.
- **Multi tenant** — many sub-tenants, **all served by the same one API**. The sub-tenant
  is selected by sending its slug to that API as `?restaurantId=<slug>`.

There is only ever **one API connection per account**. `api_base_url` and `api_auth_token`
always live on the parent tenant; sub-tenants hold identity and display overrides only.

### Required integration step for multi-tenant customers

Multi-tenant customers must expose a listing endpoint. We read their sub-tenants from it
rather than having them typed in, because the slug is sent back to their API verbatim and
a one-character mismatch fails silently on real calls.

```http
GET {api_base_url}/voice-agent/restaurants
Authorization: Bearer <api_auth_token>

200 OK
[
  { "id": "spice-garden", "name": "Spice Garden" },
  { "id": "ocean-grill",  "name": "Ocean Grill"  }
]
```

The full customer-facing contract, starter code and troubleshooting table is in
[`docs/integration-guide.md`](docs/integration-guide.md). The same content is served in-app
at `/integration-guide`, and publicly at `/guide/{id}` so a developer without an account
can read it.

### Request headers

| Header | When |
|---|---|
| `X-Tenant-ID` + `X-API-Key` | Always — identifies the account |
| `X-Sub-Tenant-ID` | Multi-tenant accounts. Rejected on single-tenant ones. |

Browsers cannot set headers on a WebSocket, so `/api/v1/voice/ws` takes the slug as the
`restaurant_id` query parameter instead. It receives the same ownership validation.

Every sub-tenant slug is checked against the resolved tenant before use. A slug belonging
to another account returns `401`; a deactivated sub-tenant returns `403`.
