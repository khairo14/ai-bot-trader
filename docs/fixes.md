# AI Bot Trader — Audit Fix Tracker

Progress log for every confirmed bug, gap, and removal identified in the full system audit.
Updated after each fix is applied.

**Skipped per user instruction:**
- #25 (JWT 7-day expiry) — only one user, not a priority now
- #26 (bcrypt pre-hash raw bytes) — only one user, not a priority now
- #32 (Docker bind-mount) — still in development
- #37 (docs/audit*.md) — leave docs alone
- #38 (.bat files) — keep them
- #39 (Alembic migration squash) — skip, migrations are necessary history
- #16 (/ws/kline auth) — **verified present**: `kline_ws.py` already has JWT auth before `await websocket.accept()`

---

## 🔴 Confirmed Bugs

| # | File | Issue | Status |
|---|------|-------|--------|
| 1 | `core/engine/forward_engine.py` | `_daily_pnl()` strips `tzinfo=None` → tz-naive datetime compared against DB timestamps | ✅ Fixed |
| 2 | `brokers/binance_client.py` | SL/TP guard failure logs error and continues — position left unprotected silently | ✅ Fixed |
| 3 | `core/engine/forward_engine.py` | `process_signal` logic checked for duplicate blocks | ✅ No fix needed (already fixed via `_G1_DEFAULT_THRESHOLD` module-level constant) |
| 4 | `brokers/alpaca_client.py` | `"3d"` timeframe silently returns `1d` bars — `_map_timeframe` and `_TF_MINUTES` both wrong | ✅ Fixed |
| 5 | `core/risk_manager.py` | `max_exposure_per_asset_pct` computed but the guard block never fires (missing comparison) | ✅ Fixed |
| 6 | `core/engine/forward_engine.py` | `except RuntimeError: raise` in `close_position` skips DB PENDING→OPEN revert guard | ✅ Fixed |
| 7 | `core/engine/forward_engine.py` | Open-position unrealized P&L not filtered by `opened_at >= today_start` in circuit-breaker query | ✅ Fixed |

---

## 🟠 Gaps

| # | File | Issue | Status |
|---|------|-------|--------|
| 8 | `api/routes/auth.py` | Redis rate-limiter opens a new connection per call instead of reusing a pool | ✅ Fixed |
| 9 | `brokers/alpaca_client.py` | `_TF_MINUTES["1w"] = 1440` under-fetches bars for weekly strategies | ✅ Fixed |
| 10 | `brokers/binance_client.py` | `get_positions()` makes N serial price calls with no timeout | ✅ Fixed |
| 11 | `main.py` | `__import__('zoneinfo')` and `from datetime import datetime, timezone` inside `while True` loop body | ✅ Fixed |
| 12 | `notifications/notifier.py` | `notifier.warning()` hard-codes `category="system"` — cannot categorize risk/broker warnings differently | ✅ Fixed |
| 13 | `tasks/outcome_resolver.py` | `resample("4h")` anchors at UTC midnight — stock 4h candles misaligned with market open | ✅ Fixed |
| 14 | `brokers/ibkr_client.py` | Reconnect loop hits 20-failure limit and stops — no notification dispatched, silent death | ✅ No fix needed (notification already present at max-failure threshold) |
| 15 | `models/portfolio_optimizer.py` | Weight collision — two strategies with same `strategy_type` in params both get the same weight | ✅ Fixed |
| 16 | `api/routes/kline_ws.py` | **Verified OK** — JWT auth is present before `websocket.accept()` | ✅ No fix needed |
| 17 | `main.py` + `docker-compose.yml` | Alembic runs in both `lifespan()` subprocess AND Docker CMD — double-migration race on container start | ✅ Fixed |
| 18 | `tasks/signal_runner.py` | `ForwardEngine.initialize()` called once before loop — in-memory state stale after each strategy commit | ✅ Fixed |
| 19 | `tasks/signal_runner.py` | Regime hysteresis state (`_regime_history`, `_stable_regime`) lost on Celery worker restart | ✅ Fixed |
| 20 | `core/engine/forward_engine.py` | Multi-leg options: warning logged but execution continues as plain market order | ✅ Fixed |

---

## 🟡 Improvements

| # | File | Issue | Status |
|---|------|-------|--------|
| 21 | `core/risk_manager.py` | `max_exposure_per_asset_pct` capped at position sizing but the enforcement guard was dead code (see bug #5) | ✅ Fixed (via Bug #5) |
| 22 | `core/features.py` | Only 6 ML features — too sparse for multi-asset multi-broker system | ✅ Fixed |
| 23 | `notifications/notifier.py` | `asyncio.create_task()` for email is abandoned when `asyncio.run()` returns in Celery workers | ✅ Fixed |
| 24 | `db/models.py` + new migration | No DB index on `signals.dismissed` / `signals.acted_on` — slow scans as table grows | ✅ Fixed |
| 27 | `core/engine/forward_engine.py` | IBKR FX minimum lot hardcoded to `25_000` — actual IBKR IDEALPRO minimum is `20_000` | ✅ Fixed |
| 28 | `db/database.py` + `config.py` | No SSL parameters passed to asyncpg connection — plaintext DB in production | ✅ Fixed |
| 29 | `core/engine/backtest_engine.py` | Backtest slippage model is flat pct — inadequate for stocks/options spread/commission | ✅ Fixed |
| 30 | `core/regime_classifier.py` | ADX threshold (`25.0`) fixed for all asset classes — crypto/FX need different tuning | ✅ Fixed |
| 31 | `tasks/signal_runner.py` + `main.py` | No PENDING trade timeout — stuck trades block position slots indefinitely | ✅ Fixed |

---

## 🟤 Removals

| # | File | Issue | Status |
|---|------|-------|--------|
| 33 | `data/fetcher.py` | Unused wrapper class — all callers use `get_broker()` directly | ✅ Fixed |
| 34 | `data/streamer.py` | Superseded by `PriceStreamManager` in `core/engine/price_stream.py` | ✅ Fixed |
| 35 | `backend/test_portfolio.py` | Orphan test file — not part of `tests/` suite, not run by pytest | ✅ Fixed |
| 36 | `config.py` | `openai_api_key` field declared unused in comments — dead config | ✅ Fixed |

---

## Fix Log

*(Updated after each fix is applied)*

| Session | Items Fixed |
|---------|-------------|
| Session 1 | Bugs 1, 2, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 20; Gaps 14 (verified OK), 16 (verified OK) |
| Session 2 | Bugs 3 (verified OK), 15, 17, 18, 19; Improvements 22, 23, 24, 27, 28, 29, 30, 31; Removals 33, 34, 35, 36 |

All confirmed items from the audit have been addressed. Skipped items remain intentionally:
- #25 JWT expiry, #26 bcrypt pre-hash — single-user system, not a priority
- #32 Docker bind-mount — still in development
- #37 audit docs — leave alone
- #38 .bat files — keep them
- #39 Alembic squash — skip, migrations are necessary history
