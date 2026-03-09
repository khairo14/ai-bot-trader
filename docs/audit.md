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
| F-077 | ✅ | `brokers/ibkr_client.py` `place_order()` | **IBKR paper orders stuck as PENDING forever — Gateway never simulates fills without an active market data subscription.** IBKR paper account requires `reqMktData` to be active for a contract to simulate paper fills. Without it, orders reach "Submitted" status in TWS but are never executed. Additionally, the 15s fill-confirmation timeout was too short for bracket orders (which have 2×1s internal settle), leaving only ~13s of actual polling. Observed: EUR/GBP SHORT bracket order 494 hit the 15s timeout and stayed PENDING indefinitely. **Fix:** (1) In `_place_bracket_async`, call `ib.reqMktData(contract, "", False, False)` + 1s wait before transmitting orders so Gateway has a live price for fill simulation; (2) added `subscribe_mkt_data_for_fill()` fire-and-forget helper for non-bracket market orders; (3) increased `_FILL_TIMEOUT` from 15s to 30s; (4) wrapped polling loop in `try/finally` to ensure `unsubscribe_mkt_data()` is always called. Stuck trade #9 manually resolved to REJECTED. |
| F-076 | ✅ | `brokers/binance_client.py` `place_order()` | **Binance LOT_SIZE step not applied when `load_markets()` fails and the fallback raw-`exchangeInfo` parser is used.** CCXT normalizes quantity via `amountToPrecision()` internally, but this reads `market['precision']['amount']` — a key only present in the CCXT-parsed market structure. The fallback loader stores raw Binance `exchangeInfo` dicts (which carry step size inside a `filters` array, not under `precision`), so CCXT silently skips normalization. If a float quantity violates the symbol's stepSize, Binance rejects the order with error code `-1111` (LOT_SIZE filter). **Fix:** added `_normalize_qty(symbol, qty)` method that truncates quantity to the LOT_SIZE step by reading from either format (CCXT `precision.amount` or raw `filters[LOT_SIZE].stepSize`); called at the top of `place_order()` with a `<= 0` guard. |
| F-075 | ✅ | `core/engine/signal_engine.py`, `api/routes/forward_test.py`, `tasks/signal_runner.py` | **IBKR signals saved with wrong `asset_class` (always `"crypto"` regardless of instrument).** `SignalEngine.run()` applies a `BROKER_ASSET_CLASS` map (`binance→crypto`, `alpaca→stock`) to tag every persisted signal. IBKR is intentionally absent (it trades stocks, forex, and options), so for IBKR signals the fallback was the strategy's class-level default — which is `"crypto"` for `HybridMACDRSIStrategy`. This caused GBP/USD FOREX signals to be stored with `asset_class=CRYPTO` in the DB. **Fix:** added an `asset_class: Optional[str] = None` parameter to `SignalEngine.run()` and updated both callers (`_run_one_strategy()` in forward_test.py and `run_signals()` in signal_runner.py) to pass `strat.asset_class.value` from the strategy's DB row — the authoritative source. Binance and Alpaca remain unaffected (hardmapped via `BROKER_ASSET_CLASS`). |

---

## Round 4 Audit (F-082) — Full Re-audit Pass
**Date:** March 2026  
**Scope:** All broker clients, ForwardEngine, signal runner, and task files following live IBKR paper trading. 4 new findings identified; all resolved in the same pass.

| # | Status | Location | Issue / Fix |
|---|--------|----------|-------------|
| F-078 | ✅ | `brokers/ibkr_client.py` | **IBKR market data subscription for bracket orders not kept alive after fill.** After the entry order fills, the paper Gateway needs an active `reqMktData` subscription to trigger the child SL/TP orders. The original code called `reqMktData` before transmitting but cancelled it in a `try/finally` after polling — so the subscription was torn down before SL/TP legs could ever fire. **Fix:** introduced `_bracket_subscriptions: dict` on `_IBKRManager`; subscription is added at entry and only removed via `cancel_bracket_subscription(symbol)` which is called from `ForwardEngine.close_position()` after the trade is confirmed closed. |
| F-079 | ✅ | `brokers/ibkr_client.py` | **IBKR `_reconnect_loop` did not re-subscribe bracket market data after Gateway reconnect.** After a disconnect/reconnect cycle all `reqMktData` subscriptions are lost. Open bracket orders on paper accounts would therefore have no price feed after reconnect and would never fire. **Fix:** `_reconnect_loop` now iterates `_bracket_subscriptions` and calls `reqMktData` for each contract after a successful reconnect. |
| F-080 | ✅ | `brokers/binance_client.py` | **Binance SL/TP guard placement silently dropped on transient exchange errors.** A single failed `create_order` call for the SL or TP leg was logged at DEBUG and ignored. A network blip during guard placement left the position completely unprotected. **Fix:** SL and TP guard placement now retries once (0.5 s delay); permanently logs at ERROR level on second failure. |
| F-081 | ✅ | `core/engine/forward_engine.py` | **`monitor_sl_tp` fetched the same broker price multiple times when multiple trades shared a symbol.** Redundant API calls on every scheduler tick for any strategy with scaled entries. **Fix:** added `_price_cache: dict[(broker, symbol), float]` local to each `monitor_sl_tp` call; price is fetched once and reused for subsequent trades on the same `(broker, symbol)` pair. |

---

## Round 5 Audit (F-083) — Full Re-audit Pass
**Date:** March 2026  
**Scope:** ForwardEngine session isolation, signal runner, forward_test route — following identification of missing pre-commit in signal processing loop. 3 new findings identified; all resolved in the same pass.

| # | Status | Location | Issue / Fix |
|---|--------|----------|-------------|
| F-082 | ✅ | `tasks/signal_runner.py`, `api/routes/forward_test.py` | **CRITICAL: `session.commit()` missing between `monitor_sl_tp` / `reconcile_positions` and the strategy processing loop.** SL/TP closing orders are issued to the broker live, but if the first strategy in the loop then raises an exception and triggers `session.rollback()`, all the DB trade closures are undone — leaving the DB with `status=OPEN` for trades the broker has already closed (ghost positions). **Fix:** Added `await session.commit()` immediately after the monitor/reconcile block in both `signal_runner.py` and `_run_one_strategy()` in `forward_test.py`, before any strategy loop iteration that could raise. |
| F-083 | ✅ | `tasks/signal_runner.py`, `api/routes/forward_test.py` | **Duplicate WS broadcast + notification after `process_signal()`.** `process_signal()` already broadcasts a `"trade"` WS event and sends a trade notification (+ email) before returning. Both callers also broadcast the same event and fired the same notification after commit — resulting in 2× WS events, 2× in-app notification rows, and 2× emails per filled trade. **Fix:** Removed the redundant `manager.broadcast("trade", ...)` and `notifier.trade(...)` blocks from both callers. |
| F-084 | ✅ | `core/engine/forward_engine.py` | **`asset_class_exposure` always 0.0 — RiskManager Level 3 (max exposure per asset class) never fired.** The value was computed as `0.0` without querying the DB, so strategies could bypass the asset-class concentration limit entirely. **Fix:** Added a DB query inside `process_signal()` that sums `quantity × entry_price` for all `OPEN` trades matching the incoming signal's `(asset_class, broker)` pair; result passed to `risk_manager.validate()`. |

---

## Round 6 Audit (F-084) — Full Re-audit Pass
**Date:** March 2026  
**Scope:** All remaining previously-unread files: all 7 strategy implementations, all ML code, all API routes, `main.py`, all frontend files. 3 new findings identified; all resolved in the same pass.

| # | Status | Location | Issue / Fix |
|---|--------|----------|-------------|
| F-085 | ✅ | `models/trainer.py` | **`StratifiedKFold(n_splits=0)` crash during weekly ML retrain.** When the training split after `train_test_split` contains only one class (e.g. 49 wins / 1 loss total where the sole loss lands in the test split), `min(5, n_pos, n_neg)` evaluates to `0`, and `StratifiedKFold(n_splits=0)` raises `ValueError`, crashing the Celery retrain worker. **Fix:** Added class-count guard — if either class has fewer than 2 samples in the training split, CV is skipped and the model is fitted directly; otherwise `n_splits = max(2, min(5, n_pos, n_neg))`. AUC gate (0.55) still enforces quality regardless. |
| F-086 | ✅ | `api/routes/portfolio.py` | **`ThreadPoolExecutor` leak — new executor created on every IBKR balance call.** `_safe_balance()` created a fresh `ThreadPoolExecutor(max_workers=1)` on every invocation. The Dashboard polls this endpoint every few seconds, so executors accumulated indefinitely. **Fix:** Moved to a module-level `_ibkr_pool` created once at import time and reused for all subsequent calls. |
| F-087 | ✅ | `api/websocket.py` | **Unauthenticated `/ws` WebSocket endpoint.** Any client could connect and receive live trade fills, P&L updates, and signal events without a valid session. **Fix:** Added JWT validation from the `access_token` httpOnly cookie (or `?token=` query param for non-browser clients) before `manager.connect(websocket)`; responds with close code `4001` on auth failure. No frontend changes required — the browser sends the httpOnly cookie automatically on same-origin WebSocket upgrades. |

---

## Round 7 Audit (F-085) — Full Re-audit Pass
**Date:** March 2026  
**Scope:** Full re-audit of all execution paths: `execute-signal` endpoint, `approve_signal` endpoint, all broker clients, risk manager, scheduler, DB models. 2 new findings identified; all resolved in the same pass.

| # | Status | Location | Issue / Fix |
|---|--------|----------|-------------|
| F-088 | ✅ | `api/routes/forward_test.py` | **Double WS broadcast on manual signal execution.** The `POST /execute-signal/{signal_id}` endpoint called `manager.broadcast("trade", ...)` after commit, while `process_signal()` had already broadcast the same event internally before returning. Frontend received 2× `"trade"` events per manual execution. **Fix:** Removed the redundant `manager.broadcast("trade", ...)` block from the endpoint; `process_signal()` is the single source of truth for trade WS events. |
| F-089 | ✅ | `api/routes/signals.py` | **Paper signal approval blocked outside market hours.** The `POST /{signal_id}/approve` endpoint ran the market-hours gate before looking up the strategy's `is_paper` flag, so paper signal approvals were blocked on weekends and after hours just like live signals — even though paper execution is pure simulation with no real orders. **Fix:** Moved strategy lookup above the market-hours check; gate now only fires when `not is_paper`, matching the behaviour of `execute-signal`. |

---

## Round 8 Audit (F-090) — IBKR Live-Trading & Execution Reliability Pass
**Date:** May 2026
**Scope:** Full re-audit after enabling IBKR live paper trading. Triggered by live log errors during first "Run Now" cycle: event-loop clash in position fetch, enum type mismatch in notification insert, silent position cache staleness, stale ghost DB positions. 12 new findings identified; all resolved.

| # | Status | Location | Issue / Fix |
|---|--------|----------|-------------|
| F-090 | ✅ | `core/engine/forward_engine.py`, `api/routes/charts.py` | **Bid/ask SL/TP enforcement missing — engine used mid-price for all exits.** `monitor_sl_tp()` compared stop-loss and take-profit against the mid-price tick even for live IBKR symbols where bid/ask spread matters. LONG exits against bid, SHORT exits against ask; forced execution at the wrong side could suppress or falsely trigger exits. Chart data was also served as mid-price OHLCV with gaps on illiquid symbols. **Fix:** Added `_price_cache: dict[(broker, symbol), tuple[float, float]]`; `stream_prices()` now populates `(bid, ask)` pairs; `monitor_sl_tp()` uses `bid` for LONG exits, `ask` for SHORT exits. Chart route gap-fills sub-24 h in-progress bars from the live price cache. |
| F-091 | ✅ | `brokers/ibkr_client.py` | **IBKR balance returns 0 after reconnect — `reqAccountUpdates` not called.** `_IBKRManager.get_balance()` polled `accountValues()` which is only populated after `reqAccountUpdates(subscribe=True)` is called. On reconnect the subscription was dropped, so balance always returned 0 until manual restart. **Fix:** `_ensure_connected()` calls `self._ib.reqAccountUpdates(subscribe=True)` immediately after a successful connect; `_fetch_async()` polls `accountValues()` with a configurable timeout (`_ACCT_TIMEOUT = 3 s`). |
| F-092 | ✅ | `api/routes/charts.py` | **IBKR historical chart gaps — forward-fill missing bars.** `get_ohlcv()` for IBKR returned raw bar lists with missing intervals (e.g. illiquid overnight gaps). Frontend chart showed jagged holes. **Fix:** After fetching, bars are forward-filled so every expected interval (based on timeframe) has an OHLCV entry; missing bars copy the previous bar's `close` as `open/high/low/close` and `volume=0`. |
| F-093 | ✅ | `brokers/ibkr_client.py` | **IBKR reconnect used same stale `IB()` object — reconnect silently failed.** `_ensure_connected()` kept the same `ib_insync.IB()` instance across reconnects. A hard disconnect (Gateway crash, network drop) left the object in an unrecoverable state; subsequent `connect()` calls raised immediately. **Fix:** On each reconnect attempt a fresh `IB()` instance is created; `asyncio.Lock` serialises concurrent reconnect attempts; up to 5 retries with 2 s back-off before giving up; on success, account update subscription is re-established. |
| F-094 | ✅ | `core/engine/forward_engine.py`, `api/routes/forward_test.py`, `tasks/signal_runner.py` | **Duplicate close orders — two concurrent monitor cycles could close the same trade twice.** `close_position()` had no guard against concurrent callers; when monitor and reconcile overlapped both would attempt a broker close. **Fix:** DB atomic guard at the top of `close_position()` — `UPDATE trades SET status='PENDING' WHERE id=X AND status='OPEN'`; `rowcount==0` means another caller already won, bail immediately. `reconcile_positions()` added before `monitor_sl_tp()` in the run loop so ghost DB positions are cleared before monitoring begins. |
| F-095 | ✅ | `brokers/ibkr_client.py` | **`get_positions()` returned stale cache — positions not refreshed after reconnect.** `_IBKRManager.get_positions()` cached broker positions in `_positions_cache` and only refreshed when the cache was older than `_POSITIONS_TTL`. After a reconnect the cache was stale (broker had flat positions but cache still showed open ones), causing reconcile to miss ghost trade cleanup. **Fix:** `get_positions()` accepts a `force_refresh=True` parameter; reconcile always calls with `force_refresh=True` to bypass the cache after a reconnect cycle. |
| F-096a | ✅ | `brokers/ibkr_client.py` | **`_do_positions()` blocking call inside running async event loop — `"This event loop is already running"`.** `reconcile_positions()` → `get_positions()` → `_do_positions()` → `self._ib.reqPositions()` internally calls `util.run()` → `loop.run_until_complete()`, which throws `RuntimeError` when the loop is already running (always in the IBKR background loop context). Exception was caught at reconcile level and logged as `"ibkr positions unavailable"`, so ghost positions were never cleared. **Fix:** Replaced with `await self._ib.reqPositionsAsync()` which is a proper coroutine and requires no nested `run_until_complete()`. |
| F-096b | ✅ | `db/models.py` | **`SAEnum(NotificationLevel)` stored uppercase enum NAMES instead of lowercase enum VALUES — PostgreSQL rejected inserts.** SQLAlchemy 2.x `SAEnum(SomePythonEnum)` maps to the enum's `.name` attribute (`"ERROR"`) for native PG enum types by default. The `notificationlevel` PG type was created by alembic with lowercase values (`'error'`, `'info'`, …). Every notification INSERT raised `invalid input value for enum notificationlevel: "ERROR"`, rolling back the enclosing trade-close transaction. **Fix:** `values_callable=lambda obj: [e.value for e in obj]` added to both `NotificationLevel` and `NotificationCategory` `SAEnum` columns so SA uses the `.value` strings (`"error"`) matching the DB type. |
| F-097 | ✅ | `api/routes/forward_test.py` | **`_run_signals_background()` fetched ALL active strategies including live ones.** The "Run Now" background task queried `StrategyModel.is_active == True` with no `is_paper` filter. If Celery was also running, live strategies would be executed twice per cycle — once by Celery, once by the manual run. **Fix:** Added `StrategyModel.is_paper == True` to the strategy query so "Run Now" only operates on paper strategies. |
| F-098 | ✅ | `tasks/signal_runner.py` | **Celery market-hours gate ignored `asset_class` — IBKR FX strategies checked against NYSE hours.** `_is_mkt_open(strat.broker.value)` was called without the `asset_class` argument. `is_market_open()` has FX-specific logic (forex trades 24/5 from Sunday 17:00 ET to Friday 17:00 ET) but only activates it when `asset_class == "forex"` is passed. Without it, IBKR forex strategies were evaluated against stock-exchange hours, suppressing execution during valid off-hours FX sessions. **Fix:** Changed call to `_is_mkt_open(strat.broker.value, getattr(strat.asset_class, "value", None))`. |
| F-099 | ✅ | `brokers/ibkr_client.py` | **`_fetch_async()` used deprecated `asyncio.get_event_loop()`.** Lines 235 and 237 called `asyncio.get_event_loop().time()` inside an `async def` coroutine running on the background event loop. In Python 3.10+ `get_event_loop()` emits a `DeprecationWarning` when there is no current event loop in the calling thread (non-issue here since the loop exists, but signals future breakage). **Fix:** Replaced both calls with `asyncio.get_running_loop().time()`, the explicit, non-deprecated API for obtaining the currently-executing loop from within a coroutine. |
| F-100 | ✅ | `api/routes/forward_test.py` | **`emergency_stop` route called `close_position()` without `db_session` — atomic guard bypassed.** The `POST /emergency-stop` endpoint looped over open trades and called `engine.close_position(t, reason="emergency_stop")` without passing `db_session`. F-094's atomic guard (`UPDATE WHERE status=OPEN → PENDING; bail if rowcount==0`) only fires when `db_session is not None`, so concurrent emergency-stop calls could double-close the same trade. **Fix:** Changed call to `engine.close_position(t, reason="emergency_stop", db_session=db)`. |
| F-101 | ✅ | `brokers/ibkr_client.py` | **`_reconnect_loop` used blocking `reqPositions()` inside async coroutine — silently swallowed.** After a successful reconnect `_reconnect_loop` called `self._ib.reqPositions()` (the blocking ib_insync variant) to refresh the position cache. Since `_reconnect_loop` is an `async def` running on the IBKR background event loop, the internal `loop.run_until_complete()` raised `RuntimeError: This event loop is already running`; the bare `except Exception: pass` silently swallowed the error, leaving the position cache stale after every reconnect. **Fix:** Replaced with `await self._ib.reqPositionsAsync()` (same fix class as F-096a). |

---

## Known Design Trade-offs (Not Bugs)

The following are documented conscious design decisions — not defects. They are recorded here so future maintainers understand the intent and the conditions under which they might warrant revisiting.

| # | Location | Trade-off | Detail |
|---|----------|-----------|--------|
| TD-001 | `frontend/src/hooks/useWebSocket.ts` | **WS close code `4001` not handled distinctly.** The frontend reconnects with exponential backoff on *any* WebSocket close code, including `4001` (auth failure added in F-087). An unauthenticated or expired-session client will retry indefinitely, logging repeated `4001` errors on the server. In practice this is harmless — an unauthenticated browser can't reach the Dashboard to trigger a connection — but it produces noisy server logs if a stale session is open in a background tab. **Revisit if:** server WS auth-rejection noise becomes operationally significant; fix is to detect `event.code === 4001` in `onclose` and stop reconnecting. |
| TD-002 | `api/routes/kline_ws.py` | **`/ws/kline` WebSocket is intentionally unauthenticated.** The candle-stream endpoint broadcasts raw public OHLCV ticks identical to what the exchange publishes openly. Requiring auth was omitted by design to keep the live-chart widget simple. **Revisit if:** the kline feed is extended to include account-sensitive data (e.g. position overlays, unrealized P&L ticks); the same JWT auth pattern from F-087 (`api/websocket.py`) can be applied in minutes. |
| TD-003 | `core/engine/forward_engine.py` `emergency_stop()` | **Emergency stop only closes paper trades; live positions must be closed on the broker platform.** Both emergency-stop endpoints (`POST /api/positions/emergency-stop` and `POST /api/forward-test/emergency-stop`) call `ForwardEngine.emergency_stop()` which filters `is_paper=True`. This is an intentional safety boundary — issuing unsupervised market-close orders on a live funded account via an API button is considered higher risk than requiring the user to act directly on the broker's native platform. **Revisit if:** a supervised "live emergency stop" with an explicit confirmation step is added to the UI; the filter would become `is_paper=True OR (is_paper=False AND confirmed=True)`. |
| TD-004 | `core/auth.py` | **No JWT revocation / refresh-token mechanism.** Issued JWTs remain valid for their full 7-day lifetime even if the user logs out or changes their password. Partially mitigated by httpOnly cookie storage (F-056 — prevents JS-based exfiltration). **Revisit if:** multi-user deployment with stricter session control is required; fix requires a Redis-backed token blocklist or short-lived access + refresh token pair. (Originally logged as F-013.) |
