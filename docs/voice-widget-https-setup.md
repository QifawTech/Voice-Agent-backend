# Making the Voice Widget Work on a Customer's HTTPS Website — Runbook

> **What this is:** the exact steps we performed to make the embeddable voice widget
> actually work when placed on a customer's **HTTPS** website (first customer: Sopzy,
> `https://www.sopzy.in`). It covers **two jobs** done on **2026-10-23**:
>
> 1. **Exposing the backend over HTTPS** (so the browser can load `widget.js` and get a token).
> 2. **Exposing LiveKit over HTTPS without owning a domain** (so the live voice call connects),
>    plus the Redis bug that was the final blocker.
>
> **Recorded:** 2026-10-23 · **Voice server:** EC2 public IP `15.207.137.4`, Mumbai `ap-south-1`,
> AWS account `053672718028`.
>
> ⚠️ **Secrets note:** this file does **not** contain real passwords/keys. Where a secret is
> needed, it points you to where the real value lives (e.g. `docker-compose.yml`, the server
> `.env`). Never paste live secrets into this `docs/` file.

---

## 1. The problem, in one picture

A website is either **locked (HTTPS 🔒)** or **unlocked (HTTP)**. Browsers enforce one hard
rule, called **mixed content**:

> A **locked** page (HTTPS) is **not allowed** to load scripts or open connections over an
> **unlocked** address (HTTP / WS). The browser silently **blocks** them.

Our customer's site `www.sopzy.in` is **locked (HTTPS)**. But our voice server was only
reachable over **unlocked** addresses (`http://15.207.137.4:8000`, `ws://...:7880`). So the
browser blocked everything and the widget never worked.

**The whole job was: give every part of the voice server a LOCKED (HTTPS/WSS) address the
browser can reach.**

### Think of it like a hotel

The voice feature needs **two different servers** working together:

| Real thing | Hotel analogy | Port |
|---|---|---|
| **Backend** (serves `widget.js`, issues the call token) | The **receptionist** who sets up your meeting | `8000` |
| **LiveKit** (the actual live audio) | The **meeting room** where you talk | `7880` (setup) + `7882/udp` (voice) |

A customer must be able to reach **both**, and both must be **locked**. We fixed them one at a time.

---

## 2. Final architecture (after this runbook)

```
  Customer browser  (https://www.sopzy.in  🔒)
      │
      │  (A) load widget.js + get call token  ── HTTPS ─►  Cloudflare Tunnel  ─►  Backend :8000
      │
      │  (B) call SETUP (signaling)  ── WSS ─►  Caddy :443 (TLS)  ─►  LiveKit :7880
      │
      └─ (C) live AUDIO  ── UDP ─────────────────────────────────►  LiveKit :7882  (direct to 15.207.137.4)

  On the EC2 box (15.207.137.4):
    Caddy     :80/:443   → TLS front door for LiveKit (hostname: 15-207-137-4.sslip.io)
    LiveKit   :7880 ws   :7881 tcp   :7882 udp     [host networking, use_external_ip: true]
    Backend   :8000      [docker]   ── Cloudflare tunnel wraps this ──► https://<random>.trycloudflare.com
    Redis     :6379      (password-protected)
    Postgres  :5432      (localhost only)
```

**Key idea:** the *setup* traffic (web + signaling) goes through a locked front door (Tunnel / Caddy).
The *live audio* goes **straight** from the browser to LiveKit over UDP — it does **not** go through
any proxy (proxies can't carry live audio; see §5).

---

## JOB 1 — Expose the backend over HTTPS (Cloudflare Tunnel)

The backend serves two web things the browser needs: the `widget.js` script and the call token.
Both are normal web (HTTP) traffic, so a **Cloudflare Tunnel** is perfect — it wraps the backend
in a free HTTPS address with no domain and no certificate work.

### 1a. First we had to fix a `widget.js` 404 (Docker bug)

**Symptom:** `GET /widget.js` returned **404** even on the server itself.

**Cause:** the widget files live in a sibling folder `Voice-Widget/` *outside* this repo. The
Dockerfile's `COPY . .` only copies this repo, so the files never made it into the image.

**Fix (permanent, in this repo):**
1. Added the widget files into the repo at `Voice-Widget/` (`voice-widget.js`, `livekit-client.umd.js`).
2. Added one line to the `Dockerfile` so they land where the `/widget.js` route looks for them:
   ```dockerfile
   COPY . .
   # Widget script + vendored LiveKit SDK. The /widget.js and /livekit-client.js routes
   # look for these under /Voice-Widget (three dirs up from app/main.py == container root).
   COPY Voice-Widget /Voice-Widget
   ```
3. Commit to `dev` → PR to `main` → CI (`.github/workflows/deploy.yml`) rebuilds + redeploys.

**Verify:** `curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8000/widget.js` → `200`.

### 1b. Put the backend behind HTTPS with a Cloudflare Tunnel

Installed `cloudflared` on the server and started a tunnel to the backend's port 8000:

```bash
# install (one time)
curl -L --output cloudflared.deb https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb
sudo dpkg -i cloudflared.deb

# run in the background (survives logout; NOT reboots — see Maintenance)
nohup cloudflared tunnel --url http://localhost:8000 > ~/cloudflared.log 2>&1 &

# read the public HTTPS URL it created
grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' ~/cloudflared.log | head -1
# e.g. https://engaging-rock-requested-charged.trycloudflare.com
```

**Point the customer site at it** — in the Sopzy frontend `.env.production`:
```
VITE_VOICE_WIDGET_URL=https://<tunnel-host>.trycloudflare.com/widget.js
```
then redeploy the Sopzy frontend. The browser now loads `widget.js` over HTTPS. ✅

> ⚠️ The free `trycloudflare.com` URL is **temporary** — it changes whenever the tunnel restarts.
> Good for getting live quickly; see **Maintenance** for the permanent option.

---

## JOB 2 — Expose LiveKit over HTTPS **without a domain**

The live call runs on **LiveKit** (separate server, port 7880). It needs the browser to reach it
over a **locked** address too. We don't own a domain, so we used two free tricks:

- **`sslip.io`** — a free wildcard DNS where `15-207-137-4.sslip.io` automatically resolves to
  `15.207.137.4`. No signup. (A hostname is required because TLS certificates are issued for
  names, not bare IPs.)
- **Caddy** — a tiny web server that automatically fetches a free Let's Encrypt certificate and
  puts LiveKit's signaling behind HTTPS/WSS.

### 2a. Firewall (AWS Security Group) — already open

These ports were already open on the voice server's security group, so **no change was needed**:

| Port | Protocol | Purpose |
|---|---|---|
| 80 | TCP | Caddy — fetch the TLS certificate |
| 443 | TCP | Caddy — secure signaling (wss) |
| 7880 | TCP | LiveKit signaling (WebSocket) |
| 7881 | TCP | LiveKit ICE/TCP fallback |
| **7882** | **UDP** | **LiveKit live audio (WebRTC media)** |

> Note: this LiveKit uses a **single** UDP media port (`7882`), set in `livekit.yaml`
> (`rtc.udp_port`), not a wide range.

### 2b. Install Caddy for TLS in front of LiveKit

```bash
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install -y caddy
```

`/etc/caddy/Caddyfile` — the entire contents:
```
15-207-137-4.sslip.io {
    reverse_proxy localhost:7880
}
```
```bash
sudo systemctl reload caddy
```

**Verify:** `curl -s -o /dev/null -w "%{http_code}\n" https://15-207-137-4.sslip.io` → `406`
(`406` is correct — LiveKit saying "I'm a WebSocket server"; it proves TLS + proxy work).

### 2c. LiveKit config was already correct for media

`~/voice-infra/livekit.yaml` already had the two important media settings:
```yaml
rtc:
  tcp_port: 7881
  udp_port: 7882
  use_external_ip: true      # LiveKit advertises the PUBLIC IP (15.207.137.4) to browsers
```
`use_external_ip: true` is essential — without it LiveKit would hand the browser its private
IP (`172.31.x.x`), which no customer can reach.

### 2d. Tell the backend to hand browsers the secure LiveKit URL

The backend returns a LiveKit address to the browser in the token response. It was empty
(so it fell back to the private `ws://` URL). We set it on the server `~/voice-infra/.env`:
```
LIVEKIT_PUBLIC_URL=wss://15-207-137-4.sslip.io
```
(`LIVEKIT_URL` stays the **private** `ws://172.31.7.161:7880` — that's what the in-container
worker uses internally. Only `LIVEKIT_PUBLIC_URL` is what the browser gets.)

Restart the backend so it picks up the new value:
```bash
cd ~/voice-infra && docker compose up -d backend
# confirm the running container actually has it:
docker exec voice-agent-backend printenv | grep LIVEKIT
```

### 2e. THE FINAL BUG — LiveKit couldn't authenticate to Redis

**Symptom:** the browser connected to `wss://.../rtc/v1` with a valid token, then the socket
dropped → `serverUnreachable`. LiveKit's own logs showed the real reason, repeated:
```
status: 500, path: /rtc/v1,
error: "could not get node for room: NOAUTH Authentication required."
```
`NOAUTH Authentication required` is a **Redis** error. LiveKit was connecting to Redis **without a
password**, but Redis **requires** one (set in `docker-compose.yml` via `--requirepass`). So
LiveKit could never allocate a room. (This had been broken the whole time — even internal tests failed.)

**Fix:** add the matching password to the `redis:` section of `~/voice-infra/livekit.yaml`:
```yaml
redis:
  address: localhost:6379
  password: <same value as --requirepass in docker-compose.yml>   # do NOT paste the real value here
```
```bash
cd ~/voice-infra && docker compose restart livekit
docker logs --tail 15 voice-infra-livekit-1   # should show "connecting to redis" and NO "NOAUTH"
```

A healthy startup log looks like:
```
INFO livekit connecting to redis {addr: localhost:6379}
INFO livekit found external IP via STUN {externalIP: 15.207.137.4}
INFO livekit starting LiveKit server {nodeIP: 15.207.137.4, portTCP: 7881, portUDP: 7882}
INFO livekit.agents worker registered
```

After this, the call connects and audio flows. ✅ **Done.**

---

## 3. Why the audio can't go through the tunnel/Caddy (important to understand)

People always ask: *"we tunneled the backend — why not just tunnel LiveKit too?"*

- The **backend** and LiveKit **signaling** are normal web traffic → **TCP** (think: numbered,
  guaranteed, in-order letters 📮). Tunnels and Caddy carry TCP perfectly.
- The **live audio** is **WebRTC → UDP** (think: a live walkie-talkie stream 📻 where being
  *on time* beats being *complete*). **Cloudflare Tunnels do NOT carry UDP media.**

So the audio must go **direct** from the browser to the server's public IP on UDP `7882`.
That's exactly why the UDP port is open in the firewall and why `use_external_ip: true` matters.
Caddy/Tunnel only handle the *setup*; the voice itself travels on its own.

---

## 4. Final configuration summary (where everything lives)

| What | Where | Value (today) |
|---|---|---|
| Widget script URL (customer site) | Sopzy `.env.production` | `https://<tunnel>.trycloudflare.com/widget.js` |
| Backend public address | Cloudflare tunnel (`~/cloudflared.log`) | `https://<random>.trycloudflare.com` |
| LiveKit public address | Caddy + sslip.io | `wss://15-207-137-4.sslip.io` |
| `LIVEKIT_PUBLIC_URL` | server `~/voice-infra/.env` | `wss://15-207-137-4.sslip.io` |
| `LIVEKIT_URL` (worker, internal) | server `~/voice-infra/.env` | `ws://172.31.7.161:7880` |
| LiveKit media port | `livekit.yaml` + firewall | UDP `7882` |
| Redis password | `docker-compose.yml` **and** `livekit.yaml` | *(see those files)* |
| Caddy config | `/etc/caddy/Caddyfile` | reverse_proxy `localhost:7880` |

---

## 5. How to verify end-to-end (quick checklist)

```bash
# 1. widget.js over the tunnel
curl -s -o /dev/null -w "widget.js -> %{http_code}\n" https://<tunnel>.trycloudflare.com/widget.js   # 200

# 2. LiveKit TLS front door
curl -s -o /dev/null -w "livekit tls -> %{http_code}\n" https://15-207-137-4.sslip.io                 # 406

# 3. backend handing out the secure URL
docker exec voice-agent-backend printenv | grep LIVEKIT_PUBLIC_URL      # wss://15-207-137-4.sslip.io

# 4. LiveKit healthy (no NOAUTH)
docker logs --tail 15 voice-infra-livekit-1                             # "connecting to redis", no NOAUTH
```
Then open `https://www.sopzy.in/shop/<store-slug>`, hard-refresh, tap **Tap to start**, allow the
microphone → you should talk to the agent and hear it reply.

---

## 6. Maintenance & gotchas (READ BEFORE THINGS "BREAK")

1. **The Cloudflare tunnel URL is temporary.** The free `trycloudflare.com` address changes
   whenever `cloudflared` restarts (crash, reboot). When that happens the widget breaks until you:
   grab the new URL from `~/cloudflared.log`, update Sopzy's `VITE_VOICE_WIDGET_URL`, and redeploy
   the frontend. → For production, switch to a **named tunnel** (needs a free Cloudflare account +
   a domain), which gives a fixed URL forever.

2. **Caddy survives reboots automatically** (it's a `systemd` service). LiveKit's `wss` address
   (`15-207-137-4.sslip.io`) is stable **as long as the server keeps the same public IP**. If the
   EC2 public IP changes (stop/start without an Elastic IP), the sslip hostname changes too → update
   `LIVEKIT_PUBLIC_URL` + the Caddyfile. → Use an **Elastic IP** to make this permanent.

3. **Server `.env` edits get overwritten on deploy.** The CI writes `~/voice-infra/.env` from the
   GitHub secret **`ENV_FILE`**. So any change made directly on the server (like `LIVEKIT_PUBLIC_URL`)
   must **also** be added to the `ENV_FILE` secret, or the next deploy wipes it.

4. **`docker compose up -d` may not reload `.env`.** If a change doesn't take effect, force it:
   `docker compose up -d --force-recreate backend`, then confirm with `docker exec ... printenv`.

---

## 7. Recommended follow-ups (not blocking, but do these)

- [ ] **Permanent URLs:** attach an **Elastic IP** + a real domain (or a named Cloudflare tunnel) so
      the widget and LiveKit URLs stop being throwaway.
- [ ] **Rotate the LiveKit API key:** it's still the default name `devkey` (the *secret* is custom,
      so it works, but rename the key for cleanliness) — must match in `livekit.yaml` **and** the
      backend `.env`.
- [ ] **Lock down Redis:** port `6379` is open to the whole internet (`0.0.0.0/0`). It has a password,
      but it should be restricted to internal-only in the security group.
- [ ] **UDP buffer tuning** (LiveKit warns `UDP receive buffer too small`): for crisper audio under
      load, raise it on the host:
      `sudo sysctl -w net.core.rmem_max=5000000` (and persist it in `/etc/sysctl.conf`).
- [ ] **Persist the tunnel as a service** so it restarts on reboot (systemd unit around `cloudflared`).

---

*Last updated: 2026-10-23. Questions → whoever maintains the voice stack.*
