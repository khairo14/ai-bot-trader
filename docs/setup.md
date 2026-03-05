# Setup Guide

## Prerequisites

Before running the bot, complete the following:

### 1. Software to Install

| Software | Purpose | Download |
|---|---|---|
| **Docker Desktop** | Runs the entire app (backend, frontend, database) | [docker.com/products/docker-desktop](https://www.docker.com/products/docker-desktop/) |
| **Git** | Clone the repository | [git-scm.com](https://git-scm.com) |
| **IB Gateway** | Required for IBKR broker integration | [IBKR Download](https://www.interactivebrokers.com/en/trading/ibgateway.php) |

> **Python and Node.js are NOT required on your machine.** They run inside Docker containers.

---

### 2. Accounts to Create

#### Binance (Crypto)
1. Sign up at [binance.com](https://binance.com)
2. Complete identity verification (KYC)
3. Go to **Account → API Management → Create API**
4. Enable permissions: **Enable Reading** + **Enable Spot & Margin Trading**
5. For futures: also enable **Enable Futures**
6. Save your `API_KEY` and `API_SECRET`
7. For paper trading: create a separate account at [testnet.binance.vision](https://testnet.binance.vision)

#### Alpaca (Stocks)
1. Sign up at [alpaca.markets](https://alpaca.markets)
2. Paper trading is available immediately upon signup — no approval needed
3. Go to **Dashboard → View → API Keys**
4. Click **Regenerate** to get your keys
5. Save your `API_KEY` and `API_SECRET`
6. Paper base URL: `https://paper-api.alpaca.markets`
7. For real-time data (optional, $9/mo): upgrade to **Unlimited plan**

#### Interactive Brokers (Stocks + Options)
1. Apply for an account at [interactivebrokers.com](https://www.interactivebrokers.com)
   - Approval takes 1–3 business days
   - Paper trading account is automatically provided upon approval
2. Download **IB Gateway** (not full TWS — lighter, better for servers)
3. Launch IB Gateway and log in
4. Configure API settings:
   - Go to **Configure → API → Settings**
   - Check ✅ `Enable ActiveX and Socket Clients`
   - Set Socket port: `7497` (paper) or `7496` (live)
   - Check ✅ `Allow connections from localhost only`
   - Set `Master API client ID`: `1`

---

## Option A: Local Development Setup (Recommended)

This runs PostgreSQL + Redis in Docker but the backend and frontend directly on your machine. This is the active development setup.

### Step 1: Clone the Repository
```bash
git clone https://github.com/khairo14/ai-bot-trader.git
cd ai-bot-trader
```

### Step 2: Start Infrastructure (DB + Redis only)
```bash
docker-compose up -d db redis
```

### Step 3: Configure Environment Variables
```bash
cp .env.example .env
```

Open `.env` and fill in your API keys. Key settings for local dev:
```env
# ── Binance ──────────────────────────────────────
BINANCE_API_KEY=your_binance_api_key
BINANCE_API_SECRET=your_binance_api_secret
BINANCE_TESTNET=false                 # true = Binance Testnet (testnet.binance.vision)
# If running behind a VPN, route Binance calls through the VPN's local proxy:
# HTTP_PROXY=http://127.0.0.1:61892  # Remove line entirely if not needed

# ── Alpaca ───────────────────────────────────────
ALPACA_API_KEY=your_alpaca_api_key
ALPACA_API_SECRET=your_alpaca_api_secret
ALPACA_BASE_URL=https://paper-api.alpaca.markets  # Change to live URL when ready

# ── IBKR ─────────────────────────────────────────
IBKR_HOST=127.0.0.1                   # Local dev: 127.0.0.1  |  Docker: host.docker.internal
IBKR_PORT=7497                        # 7497 = paper, 7496 = live
IBKR_CLIENT_ID=1
IBKR_PAPER=true

# ── Database (local dev uses localhost, not Docker service name) ──
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_USER=trader
POSTGRES_PASSWORD=change_this_password
POSTGRES_DB=ai_trader
DATABASE_URL=postgresql://trader:change_this_password@localhost:5432/ai_trader

# ── Redis ─────────────────────────────────────────
REDIS_URL=redis://localhost:6379/0

# ── App ──────────────────────────────────────────
SECRET_KEY=change_this_to_a_random_string
PAPER_INITIAL_BALANCE=10000
RISK_PER_TRADE_PCT=2.0
DAILY_CIRCUIT_BREAKER_PCT=5.0

# ── Auto-Scheduler ────────────────────────────────
# 0 = disabled (manual "Run Now" only)
# Non-zero = scheduler enabled; fires each strategy at its own timeframe boundary
# (1h strategy → fires every hour at candle close)
FORWARD_TEST_INTERVAL_MINUTES=1
```

### Step 4: Set Up Python Environment
```bash
cd backend
python -m venv .venv

# Windows:
.venv\Scripts\activate
# Mac/Linux:
source .venv/bin/activate

pip install -r requirements.txt
```

### Step 5: Run Database Migrations
```bash
# From backend/ directory with venv active:
alembic upgrade head
```

### Step 6: Start IB Gateway (IBKR only)
Before starting the backend, ensure IB Gateway is running and logged in.
- Port: `7497` (paper) or `7496` (live)
- In IB Gateway: **Configure → API → Settings**
  - ✅ Enable ActiveX and Socket Clients
  - ✅ Allow connections from localhost only
  - ❌ Read-Only API must be **OFF** (unticked) to allow trading
  - Socket port: `7497`

Skip this step if not using IBKR yet.

### Step 7: Start the Backend
```bash
# From backend/ directory with venv active:
uvicorn main:app --host 127.0.0.1 --port 8000
```

### Step 8: Start the Frontend
```bash
cd frontend
npm install
npm run dev
```

Open your browser:
| Service | URL |
|---|---|
| Frontend (React) | http://localhost:5173 |
| Backend API | http://localhost:8000 |
| API Docs (Swagger) | http://localhost:8000/docs |

---

## Option B: Full Docker Stack

> Python and Node.js are NOT required on your machine — they run inside containers.

For Docker, use these `.env` settings instead:
```env
IBKR_HOST=host.docker.internal   # Reaches IB Gateway on the host machine
POSTGRES_HOST=db                 # Docker service name
REDIS_URL=redis://redis:6379/0   # Docker service name
DATABASE_URL=postgresql://trader:change_this_password@db:5432/ai_trader
```

Then:
```bash
docker-compose up --build
```

| Service | URL |
|---|---|
| Frontend (React) | http://localhost:3000 |
| Backend API | http://localhost:8000 |
| PostgreSQL | localhost:5432 |
| Redis | localhost:6379 |

---

## Starting on Boot (Windows)

To have the bot start automatically when Windows boots:

```bash
# Run once to register as a startup task
docker-compose up -d
```

Then in Docker Desktop → **Settings → General** → enable **"Start Docker Desktop when you log in"**.

Your containers will restart automatically with Docker on each boot (because they are configured with `restart: unless-stopped` in `docker-compose.yml`).

---

## Stopping the Bot

```bash
# Stop the bot (containers remain, data preserved)
docker-compose stop

# Stop and remove containers (data preserved in volumes)
docker-compose down

# Stop and remove EVERYTHING including data (⚠️ destructive)
docker-compose down -v
```

---

## Updating the Bot

```bash
git pull origin production
docker-compose down
docker-compose up -d --build
```

---

## VPS Deployment (24/7 Autonomous)

### Recommended VPS Specs
- **Provider:** Hetzner CX22 (~$5/mo), DigitalOcean Droplet (~$12/mo), or any Linux VPS
- **OS:** Ubuntu 22.04 LTS
- **RAM:** 4GB minimum
- **CPU:** 2 vCPU minimum
- **Storage:** 40GB minimum

### IBKR on VPS (Headless IB Gateway)
IB Gateway supports headless operation via `Xvfb` (virtual display). This allows it to run on a server with no monitor.

```bash
# Install dependencies
sudo apt-get install -y xvfb

# Start IB Gateway headless
Xvfb :99 -screen 0 1024x768x24 &
export DISPLAY=:99
./ibgateway/ibgateway &
```

Alternatively, use the community Docker image for headless IB Gateway:
```bash
# In docker-compose.yml (VPS), add the IBGateway service
```
See `docker-compose.vps.yml` in the repository for the VPS-specific configuration.

### VPS Setup Steps
```bash
# 1. Connect to VPS
ssh user@your_vps_ip

# 2. Install Docker
curl -fsSL https://get.docker.com | bash
sudo usermod -aG docker $USER

# 3. Clone repo
git clone https://github.com/khairo14/ai-bot-trader.git
cd ai-bot-trader

# 4. Copy and configure .env
cp .env.example .env
nano .env   # Fill in your API keys

# 5. Start the bot
docker-compose -f docker-compose.yml -f docker-compose.vps.yml up -d
```

### Access Dashboard from Anywhere
On VPS, the dashboard is accessible at:
```
http://your_vps_ip:3000
```

**Recommended:** Set up a reverse proxy (Nginx) with SSL for secure access:
```
https://yourdomain.com
```

Instructions for Nginx + Certbot SSL are in `nginx/README.md`.

---

## Troubleshooting

### Bot can't connect to IBKR
- Ensure IB Gateway is running and logged in
- Check port: `7497` for paper, `7496` for live
- In IB Gateway settings: confirm "Allow connections from localhost only" is enabled
- Confirm **Read-Only API is OFF** (unticked) — required for trading
- **Local dev:** `IBKR_HOST=127.0.0.1` in `.env`
- **Docker:** `IBKR_HOST=host.docker.internal` in `.env`

### Database connection errors on first start
- Wait 10–15 seconds for PostgreSQL to fully initialize, then the backend will auto-retry

### Frontend shows blank page
- Wait for the frontend container to finish building (first start takes 1–2 minutes)
- Run `docker-compose logs frontend` to check build progress

### Check all container logs
```bash
docker-compose logs -f              # All containers
docker-compose logs -f backend      # Backend only
docker-compose logs -f frontend     # Frontend only
docker-compose logs -f db           # Database only
```
