# AI Bot Trader

A production-grade algorithmic trading system with AI-assisted entry/exit signals, backtesting, forward testing, and live execution across **Binance**, **Alpaca**, and **Interactive Brokers (IBKR)**.

---

## Current Build Status

| Component | Status | Notes |
|---|---|---|
| FastAPI backend | ✅ Running | Port 8000 |
| React frontend | ✅ Running | Port 5173 (Vite dev) |
| PostgreSQL | ✅ Running | Docker container |
| Redis | ✅ Running | Docker container |
| Binance connection | ✅ Live | Full spot portfolio value in USDT |
| Alpaca connection | ✅ Paper | $100,000 paper balance |
| IBKR connection | ✅ Paper | $1,000,000 paper balance via IB Gateway |
| Dashboard portfolio | ✅ Live data | All 3 broker cards from API |
| Signal pipeline | 🔧 In progress | Engine written, Celery wiring next |
| Forward Test page | 🔧 In progress | UI stub only, engine written |
| Backtest page | 🔧 In progress | UI stub only, engine written |
| WebSocket broadcasts | 🔧 In progress | Skeleton only |

---

## Quick Start

### Option A: Local Development (current setup)

**Prerequisites:**
- Python 3.11, Node.js 18+, Git
- Docker Desktop (for PostgreSQL + Redis only)
- Broker accounts (see [docs/brokers.md](docs/brokers.md))
- For IBKR: [IB Gateway](https://www.interactivebrokers.com/en/trading/ibgateway-stable.php) running locally on port 7497

```bash
git clone <your-repo-url> ai-bot-trader
cd ai-bot-trader
cp .env.example .env
# Edit .env with your API keys
```

**Start infrastructure (PostgreSQL + Redis):**
```bash
docker-compose up -d db redis
```

**Start backend:**
```bash
cd backend
python -m venv .venv
.venv\Scripts\activate          # Windows
source .venv/bin/activate        # Mac/Linux
pip install -r requirements.txt
alembic upgrade head              # run DB migrations
uvicorn main:app --host 127.0.0.1 --port 8000
```

**Start frontend:**
```bash
cd frontend
npm install
npm run dev
```

Services:
| Service | URL |
|---|---|
| Frontend (React) | http://localhost:5173 |
| Backend API | http://localhost:8000 |
| API Docs (Swagger) | http://localhost:8000/docs |
| PostgreSQL | localhost:5432 |
| Redis | localhost:6379 |

### Option B: Full Docker Stack

```bash
docker-compose up --build
```

> Docker frontend runs at **http://localhost:3000** (nginx), dev server runs at **http://localhost:5173** (Vite).

### First Run Checklist
- [ ] Set `BINANCE_TESTNET=true` in `.env` until you're confident (or use Binance Testnet at testnet.binance.vision)
- [ ] Set `ALPACA_BASE_URL=https://paper-api.alpaca.markets` for paper trading
- [ ] Start IB Gateway before using IBKR features (`IBKR_HOST=127.0.0.1` for local dev)
- [ ] If behind a VPN, set `HTTP_PROXY=http://127.0.0.1:<vpn_port>` for Binance
- [ ] Create a strategy in the UI → **Strategies** page
- [ ] Run a backtest → **Backtest** page (require Sharpe ≥ 1.0 before going live)
- [ ] Enable Forward Test in paper mode first → **Forward Test** page
- [ ] Only switch to live trading after 2+ weeks of profitable paper results

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│              React Frontend (5173 dev / 3000 prod)        │
│    Dashboard │ Backtest │ ForwardTest │ Strategies        │
└─────────────────────────┬───────────────────────────────┘
                          │ REST + WebSocket
┌─────────────────────────▼───────────────────────────────┐
│               FastAPI Backend (8000)                      │
│  /api/signals  /api/backtest  /api/positions              │
│  /api/strategies  /api/brokers  /api/tools                │
└──────┬──────────────────┬──────────────────┬────────────┘
       │                  │                  │
┌──────▼──────┐  ┌────────▼──────┐  ┌───────▼──────┐
│  Binance    │  │    Alpaca     │  │    IBKR       │
│  (ccxt)     │  │ (alpaca-api)  │  │ (ib_insync)   │
│  Crypto     │  │  US Stocks    │  │ Stocks+Options│
└─────────────┘  └───────────────┘  └──────────────┘
       │                  │
┌──────▼──────────────────▼───────────────────────────────┐
│              Core Engine                                   │
│  SignalEngine → RiskManager → ForwardEngine              │
│  BacktestEngine  (walk-forward, metrics, quality gate)    │
└─────────────────────────────────────────────────────────┘
       │
┌──────▼──────────────────────────────────────────────────┐
│                Tools Registry                             │
│  Basic: RSI MACD BB MA ATR ADX Volume                   │
│  Advanced: VWAP OrderFlow MarketProfile IV Greeks        │
│  Custom: Drop-in via BaseTool subclass                   │
└─────────────────────────────────────────────────────────┘
```

See [docs/architecture.md](docs/architecture.md) for full diagrams.

---

## Execution Modes

| Mode | What Happens |
|---|---|
| **Suggestion** | Signals shown on dashboard only — you decide |
| **Semi-Auto** | Signals shown + one-click Execute button |
| **Full-Auto** | Orders placed automatically after risk check |

Switchable per strategy in the **Strategies** page, or via `PATCH /api/strategies/{id}`.

---

## Documentation

| Doc | Contents |
|---|---|
| [docs/architecture.md](docs/architecture.md) | System overview, data flow diagrams |
| [docs/brokers.md](docs/brokers.md) | Broker setup, API details, options support |
| [docs/strategies.md](docs/strategies.md) | Strategy types, ML features, regime detection |
| [docs/tools.md](docs/tools.md) | Full tool library reference, custom tool guide |
| [docs/backtesting.md](docs/backtesting.md) | Backtest engine, metrics, quality checklist |
| [docs/execution.md](docs/execution.md) | Execution modes, order lifecycle, paper trading |
| [docs/risk-management.md](docs/risk-management.md) | 5-level risk hierarchy, position sizing, circuit breaker |
| [docs/setup.md](docs/setup.md) | Full setup guide, VPS deployment |

---

## Risk Defaults (`.env`)

```
RISK_PER_TRADE_PCT=2.0          # 2% of account per trade
MAX_OPEN_POSITIONS=5            # max concurrent positions
DAILY_CIRCUIT_BREAKER_PCT=5.0   # halt trading after 5% daily loss
MIN_RR_RATIO=1.5                # minimum reward:risk ratio
```

---

## VPS Deployment

```bash
# Copy files to VPS
scp -r . user@your-vps:/opt/ai-bot-trader

# On the VPS
cd /opt/ai-bot-trader
cp .env.example .env
# Fill in production API keys
docker-compose -f docker-compose.yml -f docker-compose.vps.yml up -d
```

See [docs/setup.md](docs/setup.md) for full VPS setup including headless IB Gateway.

---

## Accuracy Expectations

Realistic targets with proper risk management:
- **Annual Return**: 15–40% (paper → live promotion required)
- **Win Rate**: 45–55% (R:R compensates for lower win rate)
- **Max Drawdown**: < 15% (circuit breaker enforced)
- **Sharpe Ratio**: ≥ 1.0 to qualify for live trading

These are *targets*, not guarantees. Past backtest performance does not guarantee future results. Always start with paper trading and enforce the quality gate before going live.

---

## License

MIT — use at your own risk. Trading financial instruments involves significant risk of loss.
