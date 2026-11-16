# AWS EC2 Infra Setup — Runbook (Redis + Postgres + LiveKit)

> **What this is:** the exact steps performed to go from *no AWS account* → a running
> single-EC2 server self-hosting **Redis + Postgres + LiveKit** in Docker, including how to
> extract the service credentials on the server.
>
> **Recorded:** 2026-10-16 · **Region:** Asia Pacific (Sydney) `ap-southeast-2`
>
> ⚠️ **Secrets note:** this file intentionally does **not** contain the live passwords/secrets.
> They are generated on the server and retrievable anytime with the `cat` commands in
> [Phase 7](#phase-7--extract-the-credentials-on-the-server). Your backend copy lives in the
> (git-ignored) `.env`. Never commit real secrets into this `docs/` file.

---

## Final architecture

```
                 ┌─────────────────────────── EC2 instance (Ubuntu, t3.small) ───────────────────────────┐
  Internet  ───► │  LiveKit  :7880 (ws/api)  :7881 (rtc/tcp)  :7882/udp (audio)   [host networking]       │
                 │                                                                                         │
                 │  Redis    127.0.0.1:6379  (private — localhost only)                                    │
                 │  Postgres 127.0.0.1:5432  (private — localhost only)                                    │
                 │                                                                                         │
                 │  (later) api + worker containers → connect to the above over localhost                 │
                 └─────────────────────────────────────────────────────────────────────────────────────── ┘
```

- Everything runs in **Docker** on **one** EC2 instance (no AWS-managed Docker/ECS).
- Redis & Postgres are bound to `127.0.0.1` → reachable only from *inside* the server (secure).
- LiveKit uses **host networking** + STUN external-IP detection → reachable from the internet.

---

## Phase 0 — Prerequisites

- An email, a phone (for OTP).
- A credit/debit card is **NOT** required on the new AWS Free Plan (only needed later if you upgrade).
- Windows 11 with built-in OpenSSH (PowerShell `ssh` works out of the box).

---

## Phase 1 — Create the AWS account

1. Go to <https://aws.amazon.com> → **Create an AWS Account**.
2. Enter email, choose an account name, verify the email code.
3. Set a strong **root password**.
4. Account type **Personal**; fill name/address/phone.
5. **Payment:** on the new Free Plan, no card is requested.
6. Verify phone via SMS/call OTP.
7. Support plan: **Basic — Free**.
8. Choose the **Free Plan** → gives **$100 credit (up to $200), valid 6 months** (≈ 183 days).
9. Sign in to the AWS Console.

**Facts captured this session:** Free Plan, **$100 credits**, **~183 days** (until ≈ 2027-04-16),
project name *"Side Quest Accepted"*.

---

## Phase 2 — Safety net (recommended)

- **Billing budget alert:** Console search → **Budgets** → create a simple monthly cost budget
  (~$15) with your email → early warning when credits run low.
- **MFA on root:** Console search → **IAM** → add MFA to the root user (authenticator app).

> On the new Free Plan, spend is *capped at your credits* and no card is on file, so surprise
> bills are essentially impossible — but the budget alert + MFA are still good habits.

---

## Phase 3 — Region lock (IMPORTANT gotcha)

The new Free Plan **locks your project to ONE region**. Ours was auto-assigned
**Asia Pacific (Sydney) `ap-southeast-2`**. Attempting to use Mumbai showed:

> *"Region Asia Pacific (Mumbai) unavailable. To access other commercial AWS Regions,
> upgrade your plan first."*

**Decision:** stay on **Sydney** (don't upgrade). To open the real console, go to
**Settings/Projects → your project → "AWS Console"**. Do all work in Sydney.

---

## Phase 4 — Launch the EC2 instance

Console → **EC2** → **Launch instance**:

| Setting | Value |
|---|---|
| Name | `voice-agent-server` |
| AMI | **Ubuntu Server 24.04 LTS**, 64-bit (x86) |
| Instance type | `t3.small` (2 vCPU, 2 GB) — **resize to `t3.medium` (4 GB) before running the backend** |
| Key pair | **Create new** → `voice-agent-key`, RSA, `.pem` → downloads `voice-agent-key.pem` (save it!) |
| Storage | **30 GiB** gp3 (default 8 GiB is too small) |
| Network / firewall | allow SSH (My IP), HTTP, HTTPS (add LiveKit ports in Phase 5) |

**Facts captured:** instance `i-04875e21d32fbbf02`, AZ `ap-southeast-2b`,
**Public IPv4 `3.107.33.209`** *(changes on stop/start — allocate an Elastic IP to pin it)*.

> The `.pem` key cannot be re-downloaded. Losing it = losing SSH access.

---

## Phase 5 — Open the firewall ports (Security Group)

EC2 → Instances → select instance → **Security** tab → click the `sg-...` → **Edit inbound rules**.

Final inbound rules:

| Type | Protocol | Port | Source | Purpose |
|---|---|---|---|---|
| SSH | TCP | 22 | **My IP** | your terminal only |
| HTTP | TCP | 80 | 0.0.0.0/0 | (future TLS issuance) |
| HTTPS | TCP | 443 | 0.0.0.0/0 | (future TLS) |
| Custom TCP | TCP | 7880 | 0.0.0.0/0 | LiveKit API / WebSocket |
| Custom TCP | TCP | 7881 | 0.0.0.0/0 | LiveKit media over TCP (fallback) |
| **Custom UDP** | **UDP** | **7882** | 0.0.0.0/0 | **LiveKit live audio (must be UDP!)** |

> **Why 7882 is UDP:** real-time audio favors speed over perfection. TCP would stall to resend
> lost packets and make calls stutter; UDP keeps the stream flowing. Signaling/web/SSH are TCP
> because correctness matters more than instant delivery there.

---

## Phase 6 — SSH in + install Docker

**On your Windows laptop (PowerShell):**

```powershell
# 1. Lock the key's permissions (Windows refuses a too-open key)
icacls "C:\Users\ADMIN\Downloads\voice-agent-key.pem" /inheritance:r
icacls "C:\Users\ADMIN\Downloads\voice-agent-key.pem" /grant:r "ADMIN:R"

# 2. Connect (default user for Ubuntu AMI is 'ubuntu')
ssh -i "C:\Users\ADMIN\Downloads\voice-agent-key.pem" ubuntu@3.107.33.209
```

> If SSH hangs/times out: your home IP changed → re-set the SSH (22) rule's Source to **My IP**.

**Inside the server:**

```bash
# Install Docker + Compose plugin
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh
sudo usermod -aG docker ubuntu
exit                              # log out so the docker group applies
```

Reconnect with the same `ssh` command, then verify:

```bash
docker --version
docker compose version
```

---

## Phase 7 — Create the infra stack (`~/voice-infra`)

**Inside the server:**

```bash
mkdir -p ~/voice-infra && cd ~/voice-infra
```

**`.env`** (Postgres credentials — password auto-generated):

```bash
cat > .env <<EOF
POSTGRES_USER=voiceagent
POSTGRES_PASSWORD=$(openssl rand -hex 16)
POSTGRES_DB=voiceagent
EOF
```

**`livekit.yaml`** (LiveKit config — API secret auto-generated):

```bash
cat > livekit.yaml <<EOF
port: 7880
rtc:
  tcp_port: 7881
  udp_port: 7882
  use_external_ip: true
redis:
  address: localhost:6379
keys:
  devkey: $(openssl rand -hex 32)
logging:
  level: info
EOF
```

**`docker-compose.yml`** (the 3 services):

```bash
cat > docker-compose.yml <<'EOF'
services:
  redis:
    image: redis:7-alpine
    restart: unless-stopped
    command: redis-server --appendonly yes
    volumes:
      - redis-data:/data
    ports:
      - "127.0.0.1:6379:6379"

  postgres:
    image: postgres:16-alpine
    restart: unless-stopped
    env_file: .env
    volumes:
      - postgres-data:/var/lib/postgresql/data
    ports:
      - "127.0.0.1:5432:5432"

  livekit:
    image: livekit/livekit-server:latest
    restart: unless-stopped
    command: --config /etc/livekit.yaml
    volumes:
      - ./livekit.yaml:/etc/livekit.yaml
    network_mode: host
    depends_on:
      - redis

volumes:
  redis-data:
  postgres-data:
EOF
```

**Launch + verify:**

```bash
docker compose up -d          # downloads images and starts all 3
docker compose ps             # all three should be "Up"
docker compose logs livekit --tail 20
curl http://localhost:7880    # prints "OK" when LiveKit API is alive
```

Healthy LiveKit log shows: `connecting to redis addr=localhost:6379`,
`found external IP via STUN externalIP=3.107.33.209`, and
`starting LiveKit server portHttp=7880 nodeIP=3.107.33.209 version=1.13.10`.
*(The two `could not validate external IP` WARNs about `172.17.0.1`/`172.18.0.1` are
Docker's internal bridges — harmless.)*

---

## Phase 7 — Extract the credentials on the server

```bash
cd ~/voice-infra
cat livekit.yaml      # LiveKit: API key = 'devkey', API secret = the string after 'devkey:'
cat .env              # Postgres: POSTGRES_USER / POSTGRES_PASSWORD / POSTGRES_DB
```

**Known (non-secret) identifiers:**

| Service | Value |
|---|---|
| Public IP | `3.107.33.209` *(changes on stop/start)* |
| LiveKit URL | `ws://3.107.33.209:7880` |
| LiveKit API key | `devkey` |
| LiveKit API secret | *(from `livekit.yaml`)* |
| Postgres user / db | `voiceagent` / `voiceagent` |
| Postgres password | *(from `.env`)* |
| Postgres port | `5432` (localhost only) |
| Redis port | `6379` (localhost only) |

---

## Backend connection values

**If the backend runs ON the same EC2 (recommended):** everything is local —
```env
LIVEKIT_URL=ws://localhost:7880
LIVEKIT_API_KEY=devkey
LIVEKIT_API_SECRET=<from livekit.yaml>
DATABASE_URL=postgresql+asyncpg://voiceagent:<password>@localhost:5432/voiceagent
REDIS_URL=redis://localhost:6379/0
```

**If the backend runs on your laptop (dev):** LiveKit works over the public IP, but Postgres/Redis
are private — reach them with an **SSH tunnel** (keep this window open while developing):
```powershell
ssh -i "C:\Users\ADMIN\Downloads\voice-agent-key.pem" -N `
  -L 5432:localhost:5432 -L 6379:localhost:6379 ubuntu@3.107.33.209
```
Then use `ws://3.107.33.209:7880` for LiveKit and `localhost:5432` / `localhost:6379` for DB/Redis.

> Do **not** expose Postgres/Redis to the internet. If you must reach them directly from your
> laptop without a tunnel, open ports `5432`/`6379` to **My IP only** AND change the Docker binds
> from `127.0.0.1:…` to `…` — but the SSH tunnel is safer (Redis has no password).

---

## Key decisions & gotchas (why things are the way they are)

- **New AWS Free Plan** = $100 credits / 6 months / no card; **region-locked** (Sydney for us).
- **Self-managed Docker on EC2**, not ECS/Fargate — simpler to learn and control.
- **Redis/Postgres bound to `127.0.0.1`** = defense in depth; a password alone is not enough to
  safely expose a database to the internet (bots scan 5432/6379 constantly; Redis has no default
  password). Access them via SSH tunnel or by running the backend on the same box.
- **LiveKit host networking + `use_external_ip: true`** auto-detects the public IP via STUN —
  resilient to IP changes. (The repo's `docker-compose.prod.yml` instead uses bridge networking +
  a hardcoded `--node-ip` and UDP range `50000-50060`; **we chose to ignore that file** and keep
  infra decoupled from the app.)
- **Public IP changes on stop/start** → allocate a free **Elastic IP** to pin it.
- **`t3.small` (2 GB) is too small** for the backend `worker` (torch + Silero VAD ≈ 1–1.5 GB).
  Resize to **`t3.medium` (4 GB)** before running api + worker.
- **Deploy model chosen:** infra (`~/voice-infra`) stays put; the app (`api` + `worker`) deploys
  separately and connects over `localhost`. One instance for now; split the `worker` to its own
  instance only when peak concurrent calls demand it.

---

## Daily-use cheat sheet

```bash
# Connect
ssh -i "C:\Users\ADMIN\Downloads\voice-agent-key.pem" ubuntu@3.107.33.209

# Infra control
cd ~/voice-infra
docker compose ps                 # status
docker compose logs -f livekit    # follow logs
docker compose restart livekit    # restart one service
docker compose down               # stop all (data kept in volumes)
docker compose up -d              # start all

# Health / resources
curl http://localhost:7880        # LiveKit alive?
docker stats --no-stream          # CPU/RAM per container
free -h                           # server memory
```
```

> Resize instance: EC2 → Instances → select → **Instance state ▸ Stop** → **Actions ▸ Instance
> settings ▸ Change instance type** → `t3.medium` → **Start**. Volumes/containers survive; the
> public IP changes unless an Elastic IP is attached.
