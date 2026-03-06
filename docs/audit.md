# Codebase Audit — AI Bot Trader
**Date:** March 7, 2026  
**Findings:** 60 total — 3 Critical · 9 High · 34 Medium · 14 Low

Legend: ✅ Fixed | 🔧 In Progress | ⏳ Pending | ❌ Skipped

---

## 🔴 Critical

| # | Status | Location | Issue |
|---|--------|----------|-------|
| F-001 | ⏳ | `config.py` | `SECRET_KEY` defaults to `"change_this"` — only a warning logged, never a startup block. Forged JWTs possible. |
| F-037 | ⏳ | `tasks/outcome_resolver.py` | Outcome resolver always fetches **daily** OHLCV regardless of signal timeframe. Intraday (1m/1h) SL/TP hits are entirely missed. All ML training labels for intraday strategies are systematically wrong. |
| F-050 | ⏳ | `frontend/src/App.tsx` | Sidebar Emergency Stop uses native `fetch()` (no auth header) instead of axios. The emergency stop button always fails silently with HTTP 401. |

---

## 🟠 High

| # | Status | Location | Issue |
|---|--------|----------|-------|
| F-002 | ⏳ | `config.py` | Missing broker API keys / bad config never fails startup — real orders could go nowhere silently. |
| F-004 | ⏳ | `main.py` | `POST /internal/ml/reload` is unauthenticated (hidden from Swagger but fully reachable). |
| F-011 | ⏳ | `api/routes/auth.py` | Race condition on first-user admin check — two simultaneous registrations both become admins. |
| F-012 | ⏳ | `api/routes/auth.py` | No rate limiting on `POST /auth/login` — brute-force password attacks go unchecked. |
| F-014 | ⏳ | `api/routes/signals.py` | Signal approval path has no market-hours check — live orders can be placed on weekends/holidays. |
| F-015 | ⏳ | `api/routes/forward_test.py` | Emergency stop partially fails silently — broker calls that fail still mark the trade as FILLED in DB (phantom positions). |
| F-023 | ⏳ | `api/routes/strategies.py` | Any authenticated user can patch any strategy to `is_paper=false, execution_mode=full-auto`. No admin gate on mutations. |
| F-026 | ⏳ | `core/engine/forward_engine.py` | If `broker.place_order()` raises during `close_position()`, the trade is still marked FILLED. Phantom positions at broker. |
| F-057 | ⏳ | Cross-cutting | No audit log for sensitive actions (login, strategy activation, signal approval, emergency stop). |

---

## 🟡 Medium

### Config / Core
| # | Status | Location | Issue |
|---|--------|----------|-------|
| F-003 | ⏳ | `main.py` | Alembic runs via `subprocess.run` with `capture_output=True` — silent migration failures. |
| F-005 | ⏳ | `main.py` | Scheduler `last_fired` is in-memory only. Every restart fires all strategies immediately. |
| F-006 | ⏳ | `main.py` | Single `asyncio.Lock` serializes ALL strategies. A slow IBKR call blocks every other strategy. |
| F-009 | ⏳ | `db/database.py` | `create_all()` + `alembic upgrade head` both run on startup, causing schema conflicts on fresh DBs. |

### DB Models
| # | Status | Location | Issue |
|---|--------|----------|-------|
| F-007 | ⏳ | `db/models.py` | All `DateTime` columns are timezone-naive (`datetime.utcnow` deprecated in Python 3.12). |
| F-008 | ⏳ | `db/models.py` | `Signal.execution_mode` is a raw `String(20)`, not an enum — invalid strings silently stored. |

### Routes
| # | Status | Location | Issue |
|---|--------|----------|-------|
| F-010 | ⏳ | `celery_app.py` | Celery `run-signals-every-5m` has no timeframe-awareness. A 1d strategy gets evaluated 288×/day, no dedup. |
| F-016 | ⏳ | `api/routes/forward_test.py` | `days_running` strips tzinfo unsafely — wrong counter displayed. |
| F-017 | ⏳ | `api/routes/analytics.py` | Analytics summary loads ALL resolved `TradeOutcome` rows with no LIMIT — memory blowup over time. |
| F-019 | ⏳ | `api/routes/analytics.py` | Win/loss uses `ml_label == 1` instead of `outcome == WIN` — overstates win rate. |
| F-020 | ⏳ | `api/routes/backtest.py` | `BacktestResult(**result)` spreads raw dict directly into ORM with no validation. |
| F-022 | ⏳ | `api/routes/portfolio.py` | Today's P&L uses `date.today()` (local TZ) vs UTC-stored trades — off-by-hours on non-UTC servers. |
| F-024 | ⏳ | `api/routes/strategies.py` | `asset_class` / `broker` stored as raw strings, not enum-validated — runtime `ValueError` during execution. |
| F-025 | ⏳ | `api/routes/charts.py` | Charts endpoint can issue 5 sequential broker calls per request; no auth-level rate limit. |

### Engines
| # | Status | Location | Issue |
|---|--------|----------|-------|
| F-027 | ⏳ | `core/engine/forward_engine.py` | `ForwardEngine.emergency_stop()` has no `is_paper` filter — closes both paper AND live trades. |
| F-028 | ⏳ | `core/engine/forward_engine.py` | Paper balance aggregates P&L from ALL brokers — cross-broker contamination of risk sizing. |

### Core
| # | Status | Location | Issue |
|---|--------|----------|-------|
| F-031 | ⏳ | `core/risk_manager.py` | `RiskManager` reads/writes JSON state with no file lock — concurrent writes from FastAPI + Celery corrupt state. |
| F-032 | ⏳ | `core/risk_manager.py` | Circuit breaker is portfolio-wide. One losing strategy halts all others. |
| F-034 | ⏳ | `core/ml_scorer.py` | `MLScorer` uses a blocking `threading.Lock` in async context — event loop blocks on first model load. |
| F-035 | ⏳ | `core/ml_scorer.py` | Fuzzy model lookup matches on base currency prefix only — `BTC/BUSD` silently gets `BTC/USDT` model. |

### Tasks
| # | Status | Location | Issue |
|---|--------|----------|-------|
| F-039 | ⏳ | `tasks/signal_runner.py` | Celery `signal_runner` has no deduplication. Multiple identical signals per candle persist and trigger duplicate orders. |
| F-041 | ⏳ | `config.py` | `api_internal_url` defaults to `127.0.0.1` — Celery worker can't reach FastAPI inside Docker; ML cache never flushes. |

### Brokers
| # | Status | Location | Issue |
|---|--------|----------|-------|
| F-042 | ⏳ | `brokers/alpaca_client.py` | `get_event_loop()` used throughout (deprecated Python 3.10+). |
| F-043 | ⏳ | `brokers/alpaca_client.py` | `available` balance = `buying_power` (includes leverage) — risk manager overestimates tradable balance. |
| F-044 | ⏳ | `brokers/alpaca_client.py` | Alpaca live stream always uses paper credentials — live strategies never receive real price events. |
| F-045 | ⏳ | `brokers/binance_client.py` | Binance stream only normalizes USDT pairs — `ETH/BTC`, `BTC/BUSD` etc. get no price updates. |
| F-047 | ⏳ | `brokers/ibkr_client.py` | IBKR `_do_price()` sleeps 1s then returns `0.0` if no tick — ForwardEngine records 100% loss on close. |
| F-048 | ⏳ | `brokers/ibkr_client.py` | IBKR uses hardcoded `clientId=1` — FastAPI + Celery simultaneously kick each other's connection. |
| F-049 | ⏳ | `data/fetcher.py` | `DataFetcher.get_ohlcv()` wraps DataFrame in `pd.DataFrame(raw, columns=[...])` incorrectly — returns NaN. |

### Frontend
| # | Status | Location | Issue |
|---|--------|----------|-------|
| F-051 | ⏳ | `frontend/src/pages/Dashboard.tsx` | `Signal.reasons` typed as `string` but API returns `string[]` — renders as `[object Object]`. |
| F-056 | ⏳ | `frontend/src/lib/auth.ts` | JWT stored in `localStorage` — vulnerable to XSS from any dependency. |
| F-058 | ⏳ | Cross-cutting | CORS config in `.env` must be manually set; no enforcement against `*` in production. |
| F-059 | ⏳ | Cross-cutting | No data-level multi-tenancy — any authenticated user reads all other users' trade history. |
| F-060 | ⏳ | Cross-cutting | Broker clients created/destroyed per call — `BinanceClient` reloads all markets per strategy per tick. |

---

## ⚪ Low

| # | Status | Location | Issue |
|---|--------|----------|-------|
| F-013 | ⏳ | `core/auth.py` | No JWT revocation / refresh-token — stolen tokens valid for 7 days. |
| F-018 | ⏳ | `api/routes/analytics.py` | `import statistics` inside a hot-loop helper function. |
| F-021 | ⏳ | `api/routes/backtest.py` | `Content-Disposition` filename uses unsanitized `strategy_name` — header injection. |
| F-030 | ⏳ | `core/engine/signal_engine.py` | IBKR asset class always overridden to `"stock"` — forex signals tagged incorrectly in DB. |
| F-033 | ⏳ | `core/risk_manager.py` | Daily PnL circuit breaker ignores open (unrealized) losses. |
| F-036 | ⏳ | `core/regime_classifier.py` | Regime classifier uses raw closes as EMA approximation — inaccurate in volatile markets. |
| F-038 | ⏳ | `tasks/outcome_resolver.py` | `COVER` signal direction treated as short in outcome resolver — inverted PnL label. |
| F-040 | ⏳ | `tasks/ml_retrain.py` | Sync `httpx.post` inside async retrain task — blocks event loop up to 5 seconds. |
| F-046 | ⏳ | `brokers/binance_client.py` | Binance spot `get_positions()` returns `entry_price=0.0` — PnL computations wrong. |
| F-052 | ⏳ | `frontend/src/hooks/useWebSocket.ts` | `ws.onerror = () => {}` swallows all WS errors — undebuggable in production. |
| F-053 | ⏳ | `frontend/src/hooks/useWebSocket.ts` | WS reconnect uses fixed 3s delay, no exponential backoff. |
| F-054 | ⏳ | `frontend/src/pages/Analytics.tsx` | Local function named `fetch` shadows `window.fetch`. |
| F-055 | ⏳ | `frontend/src/pages/Strategies.tsx` | `toggleActive`/`changeMode` have no try/catch — failures invisible to user. |
