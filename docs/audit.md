# Codebase Audit — AI Bot Trader
**Date:** March 7, 2026  
**Findings:** 60 total — 3 Critical · 9 High · 34 Medium · 14 Low

Legend: ✅ Fixed | 🔧 In Progress | ⏳ Pending | ❌ Skipped

---

## 🔴 Critical

| # | Status | Location | Issue |
|---|--------|----------|-------|
| F-001 | ✅ | `config.py` | `SECRET_KEY` defaults to `"change_this"` — only a warning logged, never a startup block. Forged JWTs possible. |
| F-037 | ✅ | `tasks/outcome_resolver.py` | Outcome resolver always fetches **daily** OHLCV regardless of signal timeframe. Intraday (1m/1h) SL/TP hits are entirely missed. All ML training labels for intraday strategies are systematically wrong. |
| F-050 | ✅ | `frontend/src/App.tsx` | Sidebar Emergency Stop uses native `fetch()` (no auth header) instead of axios. The emergency stop button always fails silently with HTTP 401. |

---

## 🟠 High

| # | Status | Location | Issue |
|---|--------|----------|-------|
| F-002 | ✅ | `config.py` | Missing broker API keys / bad config never fails startup — real orders could go nowhere silently. |
| F-004 | ✅ | `main.py` | `POST /internal/ml/reload` is unauthenticated (hidden from Swagger but fully reachable). |
| F-011 | ✅ | `api/routes/auth.py` | Race condition on first-user admin check — two simultaneous registrations both become admins. |
| F-012 | ✅ | `api/routes/auth.py` | No rate limiting on `POST /auth/login` — brute-force password attacks go unchecked. |
| F-014 | ✅ | `api/routes/signals.py` | Signal approval path has no market-hours check — live orders can be placed on weekends/holidays. |
| F-015 | ✅ | `api/routes/forward_test.py` | Emergency stop partially fails silently — broker calls that fail still mark the trade as FILLED in DB (phantom positions). |
| F-023 | ✅ | `api/routes/strategies.py` | Any authenticated user can patch any strategy to `is_paper=false, execution_mode=full-auto`. No admin gate on mutations. |
| F-026 | ✅ | `core/engine/forward_engine.py` | If `broker.place_order()` raises during `close_position()`, the trade is still marked FILLED. Phantom positions at broker. |
| F-057 | ✅ | Cross-cutting | No audit log for sensitive actions (login, strategy activation, signal approval, emergency stop). |

---

## 🟡 Medium

### Config / Core
| # | Status | Location | Issue |
|---|--------|----------|-------|
| F-003 | ✅ | `main.py` | Alembic runs via `subprocess.run` with `capture_output=True` — silent migration failures. |
| F-005 | ✅ | `main.py` | Scheduler `last_fired` is in-memory only. Every restart fires all strategies immediately. |
| F-006 | ✅ | `main.py` | Single `asyncio.Lock` serializes ALL strategies. A slow IBKR call blocks every other strategy. |
| F-009 | ✅ | `db/database.py` | `create_all()` + `alembic upgrade head` both run on startup, causing schema conflicts on fresh DBs. |

### DB Models
| # | Status | Location | Issue |
|---|--------|----------|-------|
| F-007 | ✅ | `db/models.py` | All `DateTime` columns are timezone-naive (`datetime.utcnow` deprecated in Python 3.12). |
| F-008 | ✅ | `db/models.py` | `Signal.execution_mode` is a raw `String(20)`, not an enum — invalid strings silently stored. |

### Routes
| # | Status | Location | Issue |
|---|--------|----------|-------|
| F-010 | ✅ | `celery_app.py` | Celery `run-signals-every-5m` has no timeframe-awareness. A 1d strategy gets evaluated 288×/day, no dedup. |
| F-016 | ✅ | `api/routes/forward_test.py` | `days_running` strips tzinfo unsafely — wrong counter displayed. |
| F-017 | ✅ | `api/routes/analytics.py` | Analytics summary loads ALL resolved `TradeOutcome` rows with no LIMIT — memory blowup over time. |
| F-019 | ✅ | `api/routes/analytics.py` | Win/loss uses `ml_label == 1` instead of `outcome == WIN` — overstates win rate. |
| F-020 | ✅ | `api/routes/backtest.py` | `BacktestResult(**result)` spreads raw dict directly into ORM with no validation. |
| F-022 | ✅ | `api/routes/portfolio.py` | Today's P&L uses `date.today()` (local TZ) vs UTC-stored trades — off-by-hours on non-UTC servers. |
| F-024 | ✅ | `api/routes/strategies.py` | `asset_class` / `broker` stored as raw strings, not enum-validated — runtime `ValueError` during execution. |
| F-025 | ✅ | `api/routes/charts.py` | Charts endpoint can issue 5 sequential broker calls per request; no auth-level rate limit. |

### Engines
| # | Status | Location | Issue |
|---|--------|----------|-------|
| F-027 | ✅ | `core/engine/forward_engine.py` | `ForwardEngine.emergency_stop()` has no `is_paper` filter — closes both paper AND live trades. |
| F-028 | ✅ | `core/engine/forward_engine.py` | Paper balance aggregates P&L from ALL brokers — cross-broker contamination of risk sizing. |

### Core
| # | Status | Location | Issue |
|---|--------|----------|-------|
| F-031 | ✅ | `core/risk_manager.py` | `RiskManager` reads/writes JSON state with no file lock — concurrent writes from FastAPI + Celery corrupt state. |
| F-032 | ✅ | `core/risk_manager.py` | Circuit breaker is portfolio-wide. One losing strategy halts all others. |
| F-034 | ✅ | `core/ml_scorer.py` | `MLScorer` uses a blocking `threading.Lock` in async context — event loop blocks on first model load. |
| F-035 | ✅ | `core/ml_scorer.py` | Fuzzy model lookup matches on base currency prefix only — `BTC/BUSD` silently gets `BTC/USDT` model. |

### Tasks
| # | Status | Location | Issue |
|---|--------|----------|-------|
| F-039 | ✅ | `tasks/signal_runner.py` | Celery `signal_runner` has no deduplication. Multiple identical signals per candle persist and trigger duplicate orders. |
| F-041 | ✅ | `config.py` | `api_internal_url` defaults to `127.0.0.1` — Celery worker can't reach FastAPI inside Docker; ML cache never flushes. |

### Brokers
| # | Status | Location | Issue |
|---|--------|----------|-------|
| F-042 | ✅ | `brokers/alpaca_client.py` | `get_event_loop()` used throughout (deprecated Python 3.10+). |
| F-043 | ✅ | `brokers/alpaca_client.py` | `available` balance = `buying_power` (includes leverage) — risk manager overestimates tradable balance. |
| F-044 | ✅ | `brokers/alpaca_client.py` | Alpaca live stream always uses paper credentials — live strategies never receive real price events. |
| F-045 | ✅ | `brokers/binance_client.py` | Binance stream only normalizes USDT pairs — `ETH/BTC`, `BTC/BUSD` etc. get no price updates. |
| F-047 | ✅ | `brokers/ibkr_client.py` | IBKR `_do_price()` sleeps 1s then returns `0.0` if no tick — ForwardEngine records 100% loss on close. |
| F-048 | ✅ | `brokers/ibkr_client.py` | IBKR uses hardcoded `clientId=1` — FastAPI + Celery simultaneously kick each other's connection. |
| F-049 | ✅ | `data/fetcher.py` | `DataFetcher.get_ohlcv()` wraps DataFrame in `pd.DataFrame(raw, columns=[...])` incorrectly — returns NaN. |

### Frontend
| # | Status | Location | Issue |
|---|--------|----------|-------|
| F-051 | ✅ | `frontend/src/pages/Dashboard.tsx` | `Signal.reasons` typed as `string` but API returns `string[]` — renders as `[object Object]`. |
| F-056 | ✅ | `frontend/src/lib/auth.ts` | JWT stored in `localStorage` — vulnerable to XSS from any dependency. |
| F-058 | ✅ | Cross-cutting | CORS config in `.env` must be manually set; no enforcement against `*` in production. |
| F-059 | ✅ | Cross-cutting | No data-level multi-tenancy — any authenticated user reads all other users' trade history. |
| F-060 | ✅ | Cross-cutting | Broker clients created/destroyed per call — `BinanceClient` reloads all markets per strategy per tick. |

---

## ⚪ Low

| # | Status | Location | Issue |
|---|--------|----------|-------|
| F-013 | ❌ | `core/auth.py` | No JWT revocation / refresh-token — stolen tokens valid for 7 days. (Future work — requires Redis blocklist; partially mitigated by httpOnly cookie F-056.) |
| F-018 | ✅ | `api/routes/analytics.py` | `import statistics` inside a hot-loop helper function. |
| F-021 | ✅ | `api/routes/backtest.py` | `Content-Disposition` filename uses unsanitized `strategy_name` — header injection. |
| F-030 | ✅ | `core/engine/signal_engine.py` | IBKR asset class always overridden to `"stock"` — forex signals tagged incorrectly in DB. |
| F-033 | ✅ | `core/risk_manager.py` | Daily PnL circuit breaker ignores open (unrealized) losses. |
| F-036 | ✅ | `core/regime_classifier.py` | Regime classifier uses raw closes as EMA approximation — inaccurate in volatile markets. |
| F-038 | ✅ | `tasks/outcome_resolver.py` | `COVER` signal direction treated as short in outcome resolver — inverted PnL label. |
| F-040 | ✅ | `tasks/ml_retrain.py` | Sync `httpx.post` inside async retrain task — blocks event loop up to 5 seconds. |
| F-046 | ✅ | `brokers/binance_client.py` | Binance spot `get_positions()` returns `entry_price=0.0` — PnL computations wrong. |
| F-052 | ✅ | `frontend/src/hooks/useWebSocket.ts` | `ws.onerror = () => {}` swallows all WS errors — undebuggable in production. |
| F-053 | ✅ | `frontend/src/hooks/useWebSocket.ts` | WS reconnect uses fixed 3s delay, no exponential backoff. |
| F-054 | ✅ | `frontend/src/pages/Analytics.tsx` | Local function named `fetch` shadows `window.fetch`. |
| F-055 | ✅ | `frontend/src/pages/Strategies.tsx` | `toggleActive`/`changeMode` have no try/catch — failures invisible to user. |

---

## Round 2 Audit — Full Re-audit Pass
**Date:** Current  
**Scope:** Full codebase re-audit targeting all 11 application code files not previously audited, after all 60 original findings were closed. 6 new findings identified; all resolved in the same pass.

| # | Status | Location | Issue / Fix |
|---|--------|----------|-------------|
| F-061 | ✅ | `core/engine/forward_engine.py` | `datetime.utcnow()` still used for `opened_at`, `closed_at`, and rejected-order ID (3 occurrences). Deprecated in Python 3.12; will raise in Python 3.14. **Fix:** replaced with `datetime.now(timezone.utc)`. |
| F-062 | ✅ | `api/routes/forward_test.py` | Dedup cutoff `_cutoff = datetime.utcnow() - _dedup_window` — naive utcnow() in deduplication WHERE clause. **Fix:** replaced with `datetime.now(timezone.utc)`. |
| F-063 | ✅ | `core/auth.py` | JWT `expire = datetime.utcnow() + ...` — same deprecated call for token expiry. **Fix:** added `timezone` to import, replaced with `datetime.now(timezone.utc)`. |
| F-064 | ✅ | `api/routes/signals.py` | `cutoff = datetime.utcnow() - timedelta(hours=...)` in dismiss-expired endpoint. **Fix:** added `timezone` to import, replaced with `datetime.now(timezone.utc)`. |
| F-065 | ✅ | `tasks/outcome_resolver.py` (×2) | Two `datetime.datetime.utcnow()` calls — one for arithmetic with a naive DB datetime (would break with tz-aware), one for a DB cutoff. **Fix:** both replaced with `datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)` to keep naive-UTC semantics for Python arithmetic and DB comparisons. |
| F-066 | ✅ | `models/trainer.py` (×2) | `datetime.datetime.utcnow().isoformat()` in two return dicts. **Fix:** replaced with `datetime.datetime.now(datetime.timezone.utc).isoformat()`. |
| F-067 | ✅ | `models/portfolio_optimizer.py` | `datetime.datetime.utcnow()` for `optimized_at` timestamp in return dict. **Fix:** replaced with `datetime.datetime.now(datetime.timezone.utc)`. |
| F-068 | ✅ | `api/routes/auth.py` | `_login_attempts` dict (IP → list[timestamp]) never evicts stale IP keys — unbounded memory growth on high-traffic servers (scanner/brute-force bots each add a permanent key). **Fix:** delete IP entry from dict when its pruned list is empty; also refactored to use single `pruned` list to avoid re-reading the dict. |

---

## Round 3 Audit — Full Re-audit Pass
**Date:** Current  
**Scope:** Full codebase audit after fill-confirmation implementation (brokers, forward engine, scheduler, API routes, data models, frontend). 5 new findings identified; all resolved in the same pass.

| # | Status | Location | Issue / Fix |
|---|--------|----------|-------------|
| F-069 | ✅ | `core/engine/forward_engine.py` | **PENDING trades not hydrated on restart.** `initialize()` only loads `OPEN` trades into `_paper_positions`. After a server restart, any order submitted but not yet fill-confirmed (`status=PENDING`) is excluded from in-memory state — the engine doesn't know the symbol is already position-tracked. Two consequences: (1) if the scheduler fires again before the pending fill reconciles, a second order could be placed on the same symbol; (2) PENDING trades in DB have no path to OPEN or REJECTED and accumulate indefinitely if the broker never re-notifies. **Fix:** changed `initialize()` to load both `OPEN` and `PENDING` trades so pending orders are immediately included in position tracking on restart. |
| F-070 | ✅ | `main.py` `_forward_test_scheduler()` | **Live strategies executed twice — by both the in-process scheduler AND the Celery `run_signals` task.** The Celery task (signal_runner.py) explicitly comments that it excludes `is_paper=True` strategies ("handled by wall-clock scheduler"). However, the scheduler query had no `is_paper` filter and fired for ALL active strategies — including live ones. This creates a race condition where both Celery and the scheduler generate signals and place orders for the same live strategy candle simultaneously; DB deduplication may not catch this if both tasks run within the same tick. **Fix:** added `StrategyModel.is_paper == True` filter to the scheduler's strategy query so live strategies are handled exclusively by Celery. |
| F-071 | ✅ | `core/engine/forward_engine.py` `close_position()` | **COVER trade side direction and PnL multiplier are wrong.** The closing side was `"sell" if trade.side == "buy" else "buy"` — for a COVER trade (`side="cover"`, meaning a buy-to-cover that created a long position), this resolves to `"buy"` (buying instead of selling). Additionally, the PnL multiplier `side_mult = 1.0 if trade.side == "buy" else -1.0` resolves to `-1.0` for COVER, inverting profit and loss on COVER trade closures. **Fix:** replaced both checks with `trade.side in {"buy", "cover"}` to treat COVER positions identically to BUY positions in close direction and PnL sign. |
| F-072 | ✅ | `brokers/ibkr_client.py` (×9), `api/routes/portfolio.py`, `tasks/ml_retrain.py`, `api/routes/kline_ws.py` | **`asyncio.get_event_loop()` used inside async functions — deprecated in Python 3.10+.** In async contexts where there is always a running event loop, the correct form is `asyncio.get_running_loop()`. The deprecated `get_event_loop()` may emit `DeprecationWarning` in Python 3.12 and is scheduled for removal. Affected: `_do_price()` (×2), `connect()`, `get_price()`, `get_ohlcv()`, `get_orderbook()`, `get_options_chain()`, `place_order()`, `stream_prices()` in ibkr_client.py; `_safe_balance()` in portfolio.py; the httpx executor wrapper in ml_retrain.py; and the IBKR ticker WebSocket handler in kline_ws.py. **Fix:** replaced all 12 occurrences with `asyncio.get_running_loop()`. (One call inside a sync callback registered on ib_insync's pendingTickersEvent was left unchanged as it runs in a sync context where only `get_event_loop()` is valid.) |
| F-073 | ✅ | `brokers/ibkr_client.py` `_place_bracket_async()` | **Hardcoded 0.5 s settle after placing bracket legs may cause `parentId` mismatch.** After `ib.placeOrder()` is called for each leg, the code sleeps 0.5 s to allow IBKR's Gateway to assign a server-side order ID to the parent before child orders reference it via `parentId`. Under Gateway load or paper-account processing delays, 0.5 s may be insufficient — child orders referencing an unresolved `parentId` are silently rejected at the exchange level. **Fix:** increased to 1.0 s. |
| F-074 | ✅ | `brokers/ibkr_client.py` `place_order()` | **IBKR rejects fractional lot sizes (error 10318) — all orders with a decimal quantity are immediately cancelled.** IBKR IDEALPRO (forex) and SMART (stocks) do not support fractional quantities. The risk manager computes a float position size (e.g. `112738.724249`) which is passed directly to IBKR, causing every order to be silently rejected with `"This order doesn't support fractional quantity trading"`. Observed on the first GBP/USD SHORT paper trade (id=8, orderId=462, Mar 9 2026). **Fix:** added `quantity = math.floor(quantity)` and a `< 1` guard at the top of `place_order()` so IBKR always receives a whole-number lot size. |
