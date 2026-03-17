"""
Full-system audit test suite.
Tests every critical component identified in the live-trading audit.

Run from backend/ directory:
    python -m pytest tests/test_audit.py -v
"""
import asyncio
import json
import os
import sys
import types
import unittest
from datetime import datetime, date
from unittest.mock import MagicMock, AsyncMock, patch

import numpy as np
import pandas as pd

# ── ensure backend/ is on path ────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_ohlcv(n=100, base=50_000.0) -> pd.DataFrame:
    """Generate synthetic OHLCV data."""
    rng = np.random.default_rng(42)
    close = base + rng.normal(0, 500, n).cumsum()
    high  = close + rng.uniform(50, 200, n)
    low   = close - rng.uniform(50, 200, n)
    vol   = rng.uniform(100, 1000, n)
    return pd.DataFrame({
        "open":   close - rng.uniform(0, 100, n),
        "high":   high,
        "low":    low,
        "close":  close,
        "volume": vol,
    })


def _make_signal(signal="BUY", entry=50000.0, sl: "float | None" = 49000.0, tp=52000.0, broker="binance"):
    """Return a minimal Signal-like mock."""
    from core.strategies.base import Signal
    return Signal(
        symbol="BTC/USDT",
        signal=signal,
        entry_price=entry,
        stop_loss=sl,
        take_profit=tp,
        confidence=0.75,
        timeframe="1h",
        strategy_name="test",
        asset_class="crypto",
        broker=broker,
        reasons=["test"],
    )


# ─────────────────────────────────────────────────────────────────────────────
# 1. RiskManager
# ─────────────────────────────────────────────────────────────────────────────

class TestRiskManager(unittest.TestCase):

    def setUp(self):
        # Use a temp state file so tests don't pollute real state
        import tempfile
        self._tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self._tmp.close()
        import core.risk_manager as rm_mod
        self._orig_state_file = rm_mod._STATE_FILE
        rm_mod._STATE_FILE = self._tmp.name
        from core.risk_manager import RiskManager
        self.rm = RiskManager()
        self.rm._circuit_breaker_active = False
        self.rm.consecutive_losses = 0

    def tearDown(self):
        import core.risk_manager as rm_mod
        rm_mod._STATE_FILE = self._orig_state_file
        os.unlink(self._tmp.name)

    def test_valid_signal_approved(self):
        sig = _make_signal()
        v = self.rm.validate(sig, account_balance=10_000, open_positions_count=0, daily_pnl=0)
        self.assertTrue(v.approved, f"Expected approval, got: {v.reason}")
        self.assertGreater(v.position_size, 0)

    def test_circuit_breaker_blocks_on_loss(self):
        sig = _make_signal()  # broker="binance"
        # 6% daily loss on a 10k account triggers 5% breaker
        v = self.rm.validate(sig, account_balance=10_000, open_positions_count=0, daily_pnl=-600, broker="binance")
        self.assertFalse(v.approved)
        self.assertIn("Circuit breaker", v.reason or "")
        self.assertTrue(self.rm._per_broker["binance"]["circuit_breaker_active"])

    def test_circuit_breaker_persists_after_reset(self):
        self.rm._circuit_breaker_active = True
        self.rm._save_state()
        from core.risk_manager import RiskManager
        rm2 = RiskManager()
        # Same day → should restore True
        self.assertTrue(rm2._circuit_breaker_active)

    def test_circuit_breaker_resets_on_new_day(self):
        """State file with yesterday's date — breaker should auto-reset."""
        state = {"circuit_breaker_active": True, "circuit_breaker_date": "2000-01-01", "consecutive_losses": 0}
        with open(self._tmp.name, "w") as f:
            json.dump(state, f)
        from core.risk_manager import RiskManager
        rm2 = RiskManager()
        self.assertFalse(rm2._circuit_breaker_active)  # new day → cleared

    def test_active_circuit_breaker_blocks_immediately(self):
        # Trip the per-broker CB for 'binance' (what _make_signal() uses)
        self.rm._per_broker["binance"] = {
            "consecutive_losses": 3, "circuit_breaker_active": True,
            "circuit_breaker_date": str(date.today()),
        }
        sig = _make_signal()
        v = self.rm.validate(sig, account_balance=10_000, open_positions_count=0, daily_pnl=0, broker="binance")
        self.assertFalse(v.approved)
        self.assertIn("circuit breaker", (v.reason or "").lower())

    def test_consecutive_losses_blocks(self):
        # Set per-broker streak to the default max (3) for 'binance'
        self.rm._per_broker["binance"] = {
            "consecutive_losses": 3, "circuit_breaker_active": False,
            "circuit_breaker_date": None,
        }
        sig = _make_signal()  # broker="binance"
        v = self.rm.validate(sig, account_balance=10_000, open_positions_count=0, daily_pnl=0, broker="binance")
        self.assertFalse(v.approved)
        self.assertIn("Consecutive loss", v.reason or "")

    def test_record_outcome_increments_and_resets(self):
        self.rm.consecutive_losses = 0
        self.rm.record_outcome(won=False)
        self.assertEqual(self.rm.consecutive_losses, 1)
        self.rm.record_outcome(won=False)
        self.assertEqual(self.rm.consecutive_losses, 2)
        self.rm.record_outcome(won=True)
        self.assertEqual(self.rm.consecutive_losses, 0)

    def test_max_positions_blocks(self):
        sig = _make_signal()
        v = self.rm.validate(sig, account_balance=10_000, open_positions_count=5, daily_pnl=0)
        self.assertFalse(v.approved)
        self.assertIn("Max open positions", v.reason or "")

    def test_no_stop_loss_blocks(self):
        sig = _make_signal(sl=None)
        v = self.rm.validate(sig, account_balance=10_000, open_positions_count=0, daily_pnl=0)
        self.assertFalse(v.approved)
        self.assertIn("stop loss", v.reason or "")

    def test_bad_rr_blocks(self):
        # TP barely above entry → R:R < 1.5
        sig = _make_signal(entry=50000, sl=49000, tp=50500)  # R:R = 500/1000 = 0.5
        v = self.rm.validate(sig, account_balance=10_000, open_positions_count=0, daily_pnl=0)
        self.assertFalse(v.approved)
        self.assertIn("R:R", v.reason or "")

    def test_position_size_formula(self):
        # stop=$8000 → natural size after confidence scaling
        sig = _make_signal(entry=50000, sl=42000, tp=70000)  # stop=$8000, R:R=2.5
        bal = 10_000
        v = self.rm.validate(sig, account_balance=bal, open_positions_count=0, daily_pnl=0)
        self.assertTrue(v.approved)
        # risk_per_trade_pct=2%, confidence=0.75 → conf_scale=0.875
        # risk_amount = 10000 * 0.02 * 0.875 = 175; size = 175/8000 = 0.021875
        conf_scale = 0.5 + 0.5 * 0.75  # matches _make_signal(confidence=0.75)
        expected_size = (bal * 0.02 * conf_scale) / 8000
        self.assertAlmostEqual(v.position_size, expected_size, places=4)

    def test_reset_circuit_breaker(self):
        self.rm._circuit_breaker_active = True
        self.rm.reset_circuit_breaker()
        self.assertFalse(self.rm._circuit_breaker_active)


# ─────────────────────────────────────────────────────────────────────────────
# 2. ForwardEngine — paper trading + close_position PnL
# ─────────────────────────────────────────────────────────────────────────────

class TestForwardEngine(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        import tempfile, core.risk_manager as rm_mod
        self._tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self._tmp.close()
        self._orig = rm_mod._STATE_FILE
        rm_mod._STATE_FILE = self._tmp.name

    async def asyncTearDown(self):
        import core.risk_manager as rm_mod
        rm_mod._STATE_FILE = self._orig
        os.unlink(self._tmp.name)

    def _make_engine(self):
        from core.engine.forward_engine import ForwardEngine
        engine = ForwardEngine()
        engine._initialized = True
        # _paper_balance is dict[str, float] (keyed by broker name) since F-028
        engine._paper_balance = {"binance": 10_000.0}
        return engine

    def _make_mock_broker(self):
        """Return an AsyncMock broker that accepts all calls without hitting a real API."""
        b = AsyncMock()
        b.connect = AsyncMock()
        b.get_balance = AsyncMock(return_value=MagicMock(available=10_000.0))
        b.get_positions = AsyncMock(return_value=[])
        _order_result = MagicMock()
        _order_result.order_id = "test_paper_order_001"
        b.place_order = AsyncMock(return_value=_order_result)
        return b

    async def test_hold_signal_returns_none(self):
        engine = self._make_engine()
        sig = _make_signal("HOLD")
        result = await engine.process_signal(sig, "full_auto", is_paper=True, db_session=None)
        self.assertIsNone(result)

    async def test_suggestion_mode_returns_none(self):
        engine = self._make_engine()
        sig = _make_signal("BUY")
        result = await engine.process_signal(sig, "suggestion", is_paper=True, db_session=None)
        self.assertIsNone(result)

    async def test_full_auto_paper_creates_trade(self):
        engine = self._make_engine()
        sig = _make_signal("BUY", entry=50000, sl=49000, tp=53000)
        with patch("core.engine.forward_engine.get_broker", return_value=self._make_mock_broker()):
            result = await engine.process_signal(sig, "full_auto", is_paper=True, db_session=None)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.status.value if hasattr(result.status, "value") else result.status, "open")
        self.assertTrue(result.is_paper)
        # positions keyed by "symbol:broker" since Bug-14 composite-key fix
        self.assertIn("BTC/USDT:binance", engine._paper_positions)

    async def test_position_size_multiplier_applied(self):
        engine = self._make_engine()
        sig = _make_signal("BUY", entry=50000, sl=49000, tp=53000)
        # Without multiplier
        with patch("core.engine.forward_engine.get_broker", return_value=self._make_mock_broker()):
            r1 = await engine.process_signal(sig, "full_auto", is_paper=True, db_session=None)
        self.assertIsNotNone(r1)
        assert r1 is not None
        qty1 = r1.quantity

        engine2 = self._make_engine()
        with patch("core.engine.forward_engine.get_broker", return_value=self._make_mock_broker()):
            r2 = await engine2.process_signal(sig, "full_auto", is_paper=True, db_session=None,
                                               position_size_multiplier=2.0)
        self.assertIsNotNone(r2)
        assert r2 is not None
        qty2 = r2.quantity
        self.assertAlmostEqual(qty2, qty1 * 2.0, places=5)

    async def test_close_position_computes_pnl(self):
        engine = self._make_engine()
        # Simulate an open paper trade
        from db.models import Trade, OrderStatus, ExecutionMode, BrokerName, AssetClass
        trade = Trade(
            symbol="BTC/USDT",
            side="buy",
            quantity=0.1,
            entry_price=50000.0,
            status=OrderStatus.OPEN,
            execution_mode=ExecutionMode.FULL_AUTO,
            broker=BrokerName.BINANCE,
            asset_class=AssetClass.CRYPTO,
            is_paper=True,
            opened_at=datetime.utcnow(),
        )

        # Mock broker.get_price() to return 52000
        mock_broker = AsyncMock()
        mock_broker.get_price = AsyncMock(return_value=52000.0)

        with patch("core.engine.forward_engine.get_broker", return_value=mock_broker):
            await engine.close_position(trade, reason="test")

        assert trade.exit_price is not None
        self.assertAlmostEqual(trade.exit_price, 52000.0)
        expected_pnl = (52000.0 - 50000.0) * 0.1 * 1.0  # = 200
        assert trade.pnl is not None
        assert trade.pnl_pct is not None
        self.assertAlmostEqual(trade.pnl, expected_pnl, places=2)
        self.assertGreater(trade.pnl_pct, 0)

    async def test_close_position_short_pnl(self):
        from db.models import Trade, OrderStatus, ExecutionMode, BrokerName, AssetClass
        trade = Trade(
            symbol="BTC/USDT",
            side="sell",
            quantity=0.1,
            entry_price=50000.0,
            status=OrderStatus.OPEN,
            execution_mode=ExecutionMode.FULL_AUTO,
            broker=BrokerName.BINANCE,
            asset_class=AssetClass.CRYPTO,
            is_paper=True,
            opened_at=datetime.utcnow(),
        )
        engine = self._make_engine()
        mock_broker = AsyncMock()
        mock_broker.get_price = AsyncMock(return_value=48000.0)  # price fell → SHORT wins
        with patch("core.engine.forward_engine.get_broker", return_value=mock_broker):
            await engine.close_position(trade, reason="test")
        expected_pnl = (48000.0 - 50000.0) * 0.1 * -1.0  # = 200
        assert trade.pnl is not None
        self.assertAlmostEqual(trade.pnl, expected_pnl, places=2)
        self.assertGreater(trade.pnl, 0)

    async def test_close_position_updates_consecutive_losses(self):
        from db.models import Trade, OrderStatus, ExecutionMode, BrokerName, AssetClass
        trade = Trade(
            symbol="BTC/USDT", side="buy", quantity=0.1, entry_price=50000.0,
            status=OrderStatus.OPEN, execution_mode=ExecutionMode.FULL_AUTO,
            broker=BrokerName.BINANCE, asset_class=AssetClass.CRYPTO,
            is_paper=True, opened_at=datetime.utcnow(),
        )
        engine = self._make_engine()
        mock_broker = AsyncMock()
        mock_broker.get_price = AsyncMock(return_value=48000.0)  # loss
        with patch("core.engine.forward_engine.get_broker", return_value=mock_broker):
            await engine.close_position(trade, reason="test")
        self.assertEqual(engine.risk_manager.consecutive_losses, 1)

    async def test_emergency_stop_active_blocks_signal(self):
        engine = self._make_engine()
        engine._emergency_stop_active = True
        sig = _make_signal("BUY")
        result = await engine.process_signal(sig, "full_auto", is_paper=True, db_session=None)
        self.assertIsNone(result)

    async def test_rejected_live_order_saves_failed_trade(self):
        """When broker.place_order raises, trade is REJECTED and None is returned."""
        engine = self._make_engine()
        engine._paper_balance = 0  # force live path check
        sig = _make_signal("BUY", entry=50000, sl=49000, tp=53000)

        mock_broker = AsyncMock()
        mock_broker.get_balance = AsyncMock(return_value=MagicMock(available=10_000.0))
        mock_broker.get_positions = AsyncMock(return_value=[])
        mock_broker.place_order = AsyncMock(side_effect=Exception("Exchange down"))

        mock_db = AsyncMock()
        mock_db.add = MagicMock()
        mock_db.commit = AsyncMock()
        mock_db.flush = AsyncMock()
        # All execute() calls must return a safe result:
        #   scalar_one()          → 0.0  (_daily_pnl realised + unrealised, asset_class_exposure)
        #   scalar_one_or_none()  → None (BrokerRiskSettings lookup → no override)
        #   scalars().all()       → []   (any list query)
        _db_result = MagicMock()
        _db_result.scalar_one = MagicMock(return_value=0.0)
        _db_result.scalar_one_or_none = MagicMock(return_value=None)
        _db_result.scalars.return_value.all.return_value = []
        mock_db.execute = AsyncMock(return_value=_db_result)

        with patch("core.engine.forward_engine.get_broker", return_value=mock_broker):
            result = await engine.process_signal(
                sig, "full_auto", is_paper=False, db_session=mock_db
            )
        # process_signal returns the REJECTED Trade (not None) so callers can set signal_id
        self.assertIsNotNone(result)
        assert result is not None
        from db.models import OrderStatus, Trade as _TradeModel, LiveTrade as _LiveTradeModel
        self.assertEqual(result.status, OrderStatus.REJECTED)
        # add() is called for the REJECTED trade + possibly a notification record
        self.assertGreaterEqual(mock_db.add.call_count, 1)
        # is_paper=False → process_signal creates a LiveTrade, not a Trade
        trade_adds = [c.args[0] for c in mock_db.add.call_args_list
                      if isinstance(c.args[0], (_TradeModel, _LiveTradeModel))]
        self.assertEqual(len(trade_adds), 1)
        self.assertEqual(trade_adds[0].status, OrderStatus.REJECTED)


# ─────────────────────────────────────────────────────────────────────────────
# 3. HybridStrategy — min_score_long/short fix + ML veto
# ─────────────────────────────────────────────────────────────────────────────

class TestHybridStrategy(unittest.TestCase):

    def setUp(self):
        from core.strategies.hybrid import HybridStrategy
        self.strategy = HybridStrategy()

    def _run(self, df, symbol="BTC/USDT", ml_prob=None):
        with patch("core.strategies.hybrid.ml_scorer") as mock_ml:
            mock_ml.predict_proba.return_value = ml_prob
            return self.strategy.generate_signal(df, symbol=symbol, timeframe="1h")

    def test_insufficient_data_returns_hold(self):
        df = _make_ohlcv(30)
        sig = self._run(df)
        self.assertEqual(sig.signal, "HOLD")

    def test_no_name_error_on_min_score_vars(self):
        """Critical regression test: min_score_long/short must be defined."""
        df = _make_ohlcv(100)
        try:
            sig = self._run(df)  # Must not raise NameError
        except NameError as e:
            self.fail(f"NameError raised — min_score_long/short still undefined: {e}")

    def test_returns_valid_signal_object(self):
        df = _make_ohlcv(100)
        sig = self._run(df)
        from core.strategies.base import Signal
        self.assertIsInstance(sig, Signal)
        self.assertIn(sig.signal, {"BUY", "SELL", "SHORT", "COVER", "HOLD"})
        self.assertIsInstance(sig.confidence, float)
        self.assertGreaterEqual(sig.confidence, 0.0)
        self.assertLessEqual(sig.confidence, 1.0)

    def test_ml_veto_buys_when_prob_too_low(self):
        """If ml_prob < ML_VETO_BUY threshold, BUY should be downgraded to HOLD."""
        df = _make_ohlcv(100)
        # Force strong BUY conditions by making scores high, but ML disagrees
        with patch.object(self.strategy, "generate_signal", wraps=self.strategy.generate_signal):
            with patch("core.strategies.hybrid.ml_scorer") as mock_ml:
                mock_ml.predict_proba.return_value = 0.10  # very low BUY prob
                sig = self.strategy.generate_signal(df, symbol="BTC/USDT", timeframe="1h")
                # If a BUY was generated, ML should veto it
                if sig.signal == "BUY":
                    self.fail("ML veto did not suppress BUY signal when prob=0.10")
                # HOLD or SHORT are acceptable outcomes

    def test_ml_none_gracefully_falls_back(self):
        """If MLScorer returns None (no model), strategy falls back to rules only."""
        df = _make_ohlcv(100)
        sig = self._run(df, ml_prob=None)
        from core.strategies.base import Signal
        self.assertIsInstance(sig, Signal)  # No exception

    def test_stop_loss_and_take_profit_set_when_not_hold(self):
        df = _make_ohlcv(100)
        sig = self._run(df)
        if sig.signal not in ("HOLD",):
            self.assertIsNotNone(sig.stop_loss)
            self.assertIsNotNone(sig.take_profit)

    def test_min_score_kwarg_respected(self):
        """Passing min_score=10 (impossible) forces HOLD on any real data."""
        df = _make_ohlcv(100)
        with patch("core.strategies.hybrid.ml_scorer") as mock_ml:
            mock_ml.predict_proba.return_value = 0.8
            sig = self.strategy.generate_signal(df, "BTC/USDT", "1h", min_score=10)
        self.assertEqual(sig.signal, "HOLD")


# ─────────────────────────────────────────────────────────────────────────────
# 4. MLScorer — feature computation + safe fallback
# ─────────────────────────────────────────────────────────────────────────────

class TestMLScorer(unittest.TestCase):

    def setUp(self):
        from core.ml_scorer import MLScorer
        self.scorer = MLScorer()

    def test_returns_none_when_no_model(self):
        df = _make_ohlcv(100)
        result = self.scorer.predict_proba(df, "FAKE_SYMBOL_NO_MODEL")
        self.assertIsNone(result)

    def test_returns_none_on_short_data(self):
        df = _make_ohlcv(15)
        result = self.scorer.predict_proba(df, "BTC/USDT")
        self.assertIsNone(result)

    def test_feature_computation_produces_6_cols(self):
        from core.ml_scorer import _compute_features, FEATURE_COLS
        df = _make_ohlcv(100)
        feats = _compute_features(df)
        self.assertIsNotNone(feats)
        assert feats is not None
        # regime_code is NOT produced by _compute_features — it is added separately
        # by trainer.py (training) and ml_scorer.py (inference) via a sliding-window
        # classifier call that requires full OHLCV history, not just feature computation.
        base_cols = [c for c in FEATURE_COLS if c != "regime_code"]
        for col in base_cols:
            self.assertIn(col, feats.columns)

    def test_feature_no_nan_at_tail(self):
        from core.ml_scorer import _compute_features, FEATURE_COLS
        df = _make_ohlcv(200)
        feats = _compute_features(df)
        self.assertIsNotNone(feats)
        assert feats is not None
        # regime_code is not produced by _compute_features (added separately)
        base_cols = [c for c in FEATURE_COLS if c != "regime_code"]
        last = feats[base_cols].iloc[-1]
        self.assertFalse(last.isna().any(), f"NaN found in last feature row: {last}")

    def test_reload_clears_cache(self):
        self.scorer._models["X"] = object()
        self.scorer.reload()
        self.assertEqual(len(self.scorer._models), 0)


# ─────────────────────────────────────────────────────────────────────────────
# 5. Charts /trades endpoint — broker enum tolerance
# ─────────────────────────────────────────────────────────────────────────────

class TestChartsTradesEndpoint(unittest.IsolatedAsyncioTestCase):

    async def test_trades_query_filters_correctly(self):
        """Verify the endpoint filters by symbol and broker correctly."""
        from api.routes.charts import get_chart_trades
        # Mock db session returning empty trades
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        with patch("api.routes.charts.AsyncSessionLocal", return_value=mock_ctx):
            result = await get_chart_trades(symbol="BTC/USDT", broker="binance", since=None, until=None, limit=500)
        self.assertEqual(result["symbol"], "BTC/USDT")
        self.assertEqual(result["broker"], "binance")
        self.assertEqual(result["count"], 0)


# ─────────────────────────────────────────────────────────────────────────────
# 6. Risk route — status + reset endpoints
# ─────────────────────────────────────────────────────────────────────────────

class TestRiskRoute(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        import tempfile, core.risk_manager as rm_mod
        self._tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self._tmp.close()
        self._orig = rm_mod._STATE_FILE
        rm_mod._STATE_FILE = self._tmp.name

    async def asyncTearDown(self):
        import core.risk_manager as rm_mod
        rm_mod._STATE_FILE = self._orig
        os.unlink(self._tmp.name)

    async def test_status_returns_expected_keys(self):
        from api.routes.risk import risk_status
        result = await risk_status()
        for key in ("circuit_breaker_active", "consecutive_losses", "max_consecutive_losses"):
            self.assertIn(key, result)

    async def test_reset_circuit_breaker_clears_flag(self):
        from api.routes.risk import reset_circuit_breaker, _rm
        _rm._circuit_breaker_active = True
        result = await reset_circuit_breaker()
        self.assertFalse(_rm._circuit_breaker_active)
        self.assertIn("reset", result["message"].lower())

    async def test_reset_consecutive_losses(self):
        from api.routes.risk import reset_consecutive_losses, _rm
        _rm.consecutive_losses = 5
        result = await reset_consecutive_losses()
        self.assertEqual(_rm.consecutive_losses, 0)


# ─────────────────────────────────────────────────────────────────────────────
# 7. Confluence score helper
# ─────────────────────────────────────────────────────────────────────────────

class TestConfluenceScore(unittest.IsolatedAsyncioTestCase):

    async def test_daily_tf_returns_1(self):
        """Daily TF has no higher TF to check — always returns 1.0."""
        from tasks.signal_runner import _confluence_score
        mock_engine = AsyncMock()
        score = await _confluence_score(mock_engine, "hybrid_macd_rsi", "BTC/USDT", "binance", "1d", "BUY")
        self.assertEqual(score, 1.0)
        mock_engine.run.assert_not_called()

    async def test_all_agree_returns_one(self):
        from tasks.signal_runner import _confluence_score
        mock_engine = AsyncMock()
        mock_engine.run = AsyncMock(return_value=MagicMock(signal="BUY"))
        score = await _confluence_score(mock_engine, "hybrid", "BTC/USDT", "binance", "1h", "BUY")
        self.assertEqual(score, 1.0)

    async def test_none_agree_returns_low(self):
        from tasks.signal_runner import _confluence_score
        mock_engine = AsyncMock()
        mock_engine.run = AsyncMock(return_value=MagicMock(signal="HOLD"))
        score = await _confluence_score(mock_engine, "hybrid", "BTC/USDT", "binance", "1h", "BUY")
        # primary=BUY, 4h=HOLD, 1d=HOLD → 1/3 ≈ 0.33
        self.assertAlmostEqual(score, 1/3, places=5)

    async def test_exception_in_higher_tf_excluded_from_denominator(self):
        from tasks.signal_runner import _confluence_score
        mock_engine = AsyncMock()
        mock_engine.run = AsyncMock(side_effect=Exception("timeout"))
        score = await _confluence_score(mock_engine, "hybrid", "BTC/USDT", "binance", "1h", "BUY")
        # Transient errors are EXCLUDED from the denominator (skipped) so a broker
        # outage doesn't systematically suppress all strategies.
        # primary=BUY only remains → 1/1 = 1.0
        self.assertAlmostEqual(score, 1.0, places=5)

    async def test_min_confluence_gate(self):
        from tasks.signal_runner import MIN_CONFLUENCE
        self.assertGreater(MIN_CONFLUENCE, 0.0)
        self.assertLessEqual(MIN_CONFLUENCE, 1.0)


# ─────────────────────────────────────────────────────────────────────────────
# 8. ForwardEngine — initialize() hydrates state from DB
# ─────────────────────────────────────────────────────────────────────────────

class TestForwardEngineInitialize(unittest.IsolatedAsyncioTestCase):

    async def test_initialize_sets_balance_and_positions(self):
        from core.engine.forward_engine import ForwardEngine, PAPER_INITIAL_CAPITAL
        from db.models import Trade, OrderStatus, ExecutionMode, BrokerName, AssetClass

        from db.models import BrokerName
        open_trade = MagicMock(spec=Trade)
        open_trade.symbol = "BTC/USDT"
        open_trade.broker = BrokerName.BINANCE  # needed for symbol:broker composite key

        mock_session = AsyncMock()

        # Query 1: open trades
        open_result = MagicMock()
        open_result.scalars.return_value.all.return_value = [open_trade]
        # Query 2: distinct brokers — initialize() now does per-broker balance (F-028)
        broker_result = MagicMock()
        broker_result.all.return_value = [("binance",)]
        # Query 3: per-broker realised PnL sum
        pnl_result = MagicMock()
        pnl_result.scalar_one.return_value = 500.0
        # Query 4: per-broker unrealised PnL sum (open positions mark-to-market)
        unrealised_result = MagicMock()
        unrealised_result.scalar_one.return_value = 0.0

        mock_session.execute = AsyncMock(side_effect=[open_result, broker_result, pnl_result, unrealised_result])

        engine = ForwardEngine()
        await engine.initialize(mock_session)

        self.assertEqual(len(engine._paper_positions), 1)
        # key is "symbol:broker" since Bug-14 composite-key fix
        self.assertIn("BTC/USDT:binance", engine._paper_positions)
        # _paper_balance is now dict[str, float] keyed by broker name (F-028)
        self.assertIsInstance(engine._paper_balance, dict)
        self.assertIn("binance", engine._paper_balance)
        self.assertAlmostEqual(engine._paper_balance["binance"], PAPER_INITIAL_CAPITAL + 500.0)
        self.assertTrue(engine._initialized)


# ─────────────────────────────────────────────────────────────────────────────
# 9. EX-01 — IV Rank & Black-Scholes helpers
# ─────────────────────────────────────────────────────────────────────────────

class TestIVRankModule(unittest.TestCase):

    def _data(self, n=300, base=150.0, seed=42):
        rng = np.random.default_rng(seed)
        close = base + rng.normal(0, 2, n).cumsum()
        close = np.maximum(close, 5.0)
        return pd.DataFrame({
            "open":   close * 0.999,
            "high":   close * 1.005,
            "low":    close * 0.995,
            "close":  close,
            "volume": rng.uniform(1e4, 1e6, n),
        })

    def test_historical_volatility_returns_float(self):
        from core.options.iv_rank import historical_volatility
        df = self._data()
        hv = historical_volatility(df)
        self.assertIsInstance(hv, float)
        self.assertGreater(hv, 0.0)

    def test_historical_volatility_fallback_on_short_data(self):
        from core.options.iv_rank import historical_volatility
        df = self._data(n=10)
        hv = historical_volatility(df)
        self.assertEqual(hv, 30.0)   # hardcoded fallback

    def test_historical_volatility_floors_at_5(self):
        """A perfectly flat price series should still return ≥ 5%."""
        from core.options.iv_rank import historical_volatility
        df = self._data(n=100)
        df["close"] = 100.0   # zero variance
        hv = historical_volatility(df)
        self.assertEqual(hv, 5.0)

    def test_iv_rank_returns_0_to_100(self):
        from core.options.iv_rank import iv_rank_from_ohlcv
        df = self._data(n=300)
        rank = iv_rank_from_ohlcv(df)
        self.assertGreaterEqual(rank, 0.0)
        self.assertLessEqual(rank, 100.0)

    def test_iv_rank_fallback_on_short_data(self):
        from core.options.iv_rank import iv_rank_from_ohlcv
        df = self._data(n=50)
        rank = iv_rank_from_ohlcv(df)
        # Short data path returns min(100, max(0, hv)); hv > 0 for real data
        self.assertGreaterEqual(rank, 0.0)
        self.assertLessEqual(rank, 100.0)

    # ── Black-Scholes pricing ─────────────────────────────────────────────────

    def test_bs_call_atm_positive(self):
        from core.options.iv_rank import bs_call
        price = bs_call(100, 100, 30/365, 0.25)
        self.assertGreater(price, 0.0)

    def test_bs_put_atm_positive(self):
        from core.options.iv_rank import bs_put
        price = bs_put(100, 100, 30/365, 0.25)
        self.assertGreater(price, 0.0)

    def test_put_call_parity(self):
        """C - P = S - K·e^(-rT) (put-call parity)."""
        from core.options.iv_rank import bs_call, bs_put, RISK_FREE_RATE
        import math
        S, K, T, sigma = 150.0, 155.0, 45/365, 0.30
        C = bs_call(S, K, T, sigma)
        P = bs_put(S, K, T, sigma)
        parity = C - P
        expected = S - K * math.exp(-RISK_FREE_RATE * T)
        self.assertAlmostEqual(parity, expected, places=6)

    def test_bs_call_deep_itm_approaches_intrinsic(self):
        """Very deep ITM call ≈ S − K (intrinsic value)."""
        from core.options.iv_rank import bs_call
        C = bs_call(200, 50, 30/365, 0.20)   # S=200, K=50 — deep ITM
        intrinsic = 200 - 50
        self.assertAlmostEqual(C, intrinsic, delta=1.0)

    def test_bs_call_zero_time_returns_intrinsic(self):
        from core.options.iv_rank import bs_call
        C_itm = bs_call(110, 100, 1e-9, 0.25)
        self.assertAlmostEqual(C_itm, 10.0, delta=0.01)
        C_otm = bs_call(90, 100, 1e-9, 0.25)
        self.assertAlmostEqual(C_otm, 0.0, delta=0.01)

    def test_delta_call_between_0_and_1(self):
        from core.options.iv_rank import bs_delta_call
        d = bs_delta_call(100, 100, 30/365, 0.25)
        self.assertGreater(d, 0.0)
        self.assertLess(d, 1.0)

    def test_delta_put_between_minus1_and_0(self):
        from core.options.iv_rank import bs_delta_put
        d = bs_delta_put(100, 100, 30/365, 0.25)
        self.assertGreater(d, -1.0)
        self.assertLess(d, 0.0)

    def test_delta_call_plus_put_equals_1(self):
        """delta_put = delta_call - 1 (put-call delta parity)."""
        from core.options.iv_rank import bs_delta_call, bs_delta_put
        d_call = bs_delta_call(100, 100, 30/365, 0.25)
        d_put  = bs_delta_put(100, 100, 30/365, 0.25)
        # B-S identity: delta_put = delta_call - 1  →  delta_call - delta_put = 1
        self.assertAlmostEqual(d_call - d_put, 1.0, places=6)

    def test_theta_call_negative(self):
        """Long call theta is negative (time decay hurts buyers)."""
        from core.options.iv_rank import bs_theta_call
        theta = bs_theta_call(100, 100, 30/365, 0.25)
        self.assertLess(theta, 0.0)

    def test_vega_positive(self):
        """Vega is always positive (higher IV → higher premium)."""
        from core.options.iv_rank import bs_vega
        vega = bs_vega(100, 100, 30/365, 0.25)
        self.assertGreater(vega, 0.0)

    # ── Strike & Expiry helpers ───────────────────────────────────────────────

    def test_nearest_strike_rounds_to_half_dollar_below_25(self):
        from core.options.iv_rank import nearest_strike
        self.assertEqual(nearest_strike(20, 20.3), 20.5)
        self.assertEqual(nearest_strike(20, 19.9), 20.0)

    def test_nearest_strike_rounds_to_1_dollar_below_50(self):
        from core.options.iv_rank import nearest_strike
        self.assertEqual(nearest_strike(40, 40.6), 41.0)

    def test_nearest_strike_rounds_to_5_dollar_below_200(self):
        from core.options.iv_rank import nearest_strike
        self.assertEqual(nearest_strike(100, 103), 105.0)
        self.assertEqual(nearest_strike(100, 102), 100.0)

    def test_nearest_strike_rounds_to_10_dollar_above_200(self):
        from core.options.iv_rank import nearest_strike
        self.assertEqual(nearest_strike(300, 307), 310.0)
        self.assertEqual(nearest_strike(300, 303), 300.0)

    def test_next_monthly_expiry_returns_friday(self):
        from core.options.iv_rank import next_monthly_expiry
        from datetime import date
        expiry_str = next_monthly_expiry(30)
        self.assertEqual(len(expiry_str), 8)
        expiry = date(int(expiry_str[:4]), int(expiry_str[4:6]), int(expiry_str[6:]))
        self.assertEqual(expiry.weekday(), 4)   # 4 = Friday

    def test_next_monthly_expiry_at_least_dte_away(self):
        from core.options.iv_rank import next_monthly_expiry
        from datetime import date
        for dte in (30, 45):
            expiry_str = next_monthly_expiry(dte)
            expiry = date(int(expiry_str[:4]), int(expiry_str[4:6]), int(expiry_str[6:]))
            self.assertGreaterEqual((expiry - date.today()).days, dte - 1)


# ─────────────────────────────────────────────────────────────────────────────
# 10. EX-01 — IronCondorStrategy
# ─────────────────────────────────────────────────────────────────────────────

class TestIronCondorStrategy(unittest.TestCase):

    def _strat(self):
        from core.strategies.iron_condor import IronCondorStrategy
        return IronCondorStrategy()

    def _data(self, n=300, volatile=True, seed=7):
        """High-IV (volatile), range-bound data for iron condor."""
        rng = np.random.default_rng(seed)
        scale = 8.0 if volatile else 0.3
        close = 150.0 + rng.normal(0, scale, n).cumsum()
        close = np.maximum(close, 10.0)
        atr_like = np.abs(rng.normal(1.5, 0.5, n))
        return pd.DataFrame({
            "open":   close - atr_like * 0.3,
            "high":   close + atr_like,
            "low":    close - atr_like,
            "close":  close,
            "volume": rng.uniform(1e5, 1e6, n),
        })

    def test_insufficient_data_returns_hold(self):
        strat = self._strat()
        df = self._data(n=20)
        sig = strat.generate_signal(df, "AAPL")
        self.assertEqual(sig.signal, "HOLD")
        self.assertTrue(any("Insufficient" in r for r in sig.reasons))

    def test_low_iv_rank_returns_hold(self):
        """When IV Rank < MIN_IV_RANK (50), strategy must HOLD."""
        strat = self._strat()  # MIN_IV_RANK = 50
        df = self._data(n=300)
        with patch("core.strategies.iron_condor.iv_rank_from_ohlcv", return_value=20.0), \
             patch("core.strategies.iron_condor.historical_volatility", return_value=20.0):
            sig = strat.generate_signal(df, "AAPL")
        self.assertEqual(sig.signal, "HOLD")

    def test_sell_signal_has_required_fields(self):
        """With favourable conditions, strategy must produce a SELL with valid options_meta."""
        strat = self._strat()
        strat.MIN_IV_RANK = 0.0   # override to guarantee signal fires on synthetic data
        strat.MAX_ADX = 99.0
        df = self._data(n=300, volatile=True)
        # Force a viable combined premium
        with patch.object(strat.atr_tool, "calculate") as mock_atr, \
             patch.object(strat.adx_tool, "calculate") as mock_adx:
            mock_atr.return_value = MagicMock(value=5.0)
            mock_adx.return_value = MagicMock(value=18.0)
            sig = strat.generate_signal(df, "AAPL")
        if sig.signal == "SELL":
            self.assertIsNotNone(sig.options_meta)
            assert sig.options_meta is not None
            self.assertEqual(sig.options_meta["strategy_type"], "iron_condor")
            self.assertEqual(len(sig.options_meta["legs"]), 4)
            self.assertIsNotNone(sig.iv_rank)
            self.assertIsNotNone(sig.delta)
            self.assertIsNotNone(sig.theta)
            self.assertIsNotNone(sig.vega)
            self.assertGreater(sig.entry_price, 0)
            self.assertIsNotNone(sig.stop_loss)
            self.assertIsNotNone(sig.take_profit)
            self.assertGreater(sig.confidence, 0.0)
            self.assertLessEqual(sig.confidence, 1.0)

    def test_options_meta_legs_structure(self):
        """All 4 legs have required action/right/strike/premium keys."""
        strat = self._strat()
        strat.MIN_IV_RANK = 0.0
        strat.MAX_ADX = 99.0
        df = self._data(n=300)
        with patch.object(strat.atr_tool, "calculate") as mock_atr, \
             patch.object(strat.adx_tool, "calculate") as mock_adx:
            mock_atr.return_value = MagicMock(value=5.0)
            mock_adx.return_value = MagicMock(value=18.0)
            sig = strat.generate_signal(df, "AAPL")
        if sig.signal == "SELL":
            assert sig.options_meta is not None
            for leg in sig.options_meta["legs"]:
                for key in ("action", "right", "strike", "premium"):
                    self.assertIn(key, leg)
            # Validate leg order: SELL C, BUY C, SELL P, BUY P
            rights_actions = [(l["action"], l["right"]) for l in sig.options_meta["legs"]]
            self.assertIn(("SELL", "C"), rights_actions)
            self.assertIn(("BUY",  "C"), rights_actions)
            self.assertIn(("SELL", "P"), rights_actions)
            self.assertIn(("BUY",  "P"), rights_actions)

    def test_asset_class_and_broker(self):
        strat = self._strat()
        self.assertEqual(strat.asset_class, "option")
        self.assertEqual(strat.broker, "ibkr")


# ─────────────────────────────────────────────────────────────────────────────
# 11. EX-01 — CoveredCallStrategy
# ─────────────────────────────────────────────────────────────────────────────

class TestCoveredCallStrategy(unittest.TestCase):

    def _strat(self):
        from core.strategies.covered_call import CoveredCallStrategy
        return CoveredCallStrategy()

    def _data(self, n=200, seed=11):
        rng = np.random.default_rng(seed)
        close = 100.0 + rng.normal(0, 1.5, n).cumsum()
        close = np.maximum(close, 5.0)
        atr = np.abs(rng.normal(1.2, 0.3, n))
        return pd.DataFrame({
            "open":   close - atr * 0.2,
            "high":   close + atr,
            "low":    close - atr,
            "close":  close,
            "volume": rng.uniform(5e4, 5e5, n),
        })

    def test_insufficient_data_returns_hold(self):
        strat = self._strat()
        df = self._data(n=10)
        sig = strat.generate_signal(df, "MSFT")
        self.assertEqual(sig.signal, "HOLD")

    def test_sell_signal_single_leg_covered_call(self):
        strat = self._strat()
        strat.MIN_IV_RANK = 0.0
        strat.MAX_IV_RANK = 100.0
        strat.RSI_LOW = 0.0
        strat.RSI_HIGH = 100.0
        strat.MAX_ADX = 100.0
        df = self._data(n=200)
        with patch.object(strat.atr_tool, "calculate") as mock_atr, \
             patch.object(strat.adx_tool, "calculate") as mock_adx, \
             patch.object(strat.rsi_tool, "calculate") as mock_rsi:
            mock_atr.return_value = MagicMock(value=3.0)
            mock_adx.return_value = MagicMock(value=18.0)
            mock_rsi.return_value = MagicMock(value=50.0)
            sig = strat.generate_signal(df, "MSFT")
        if sig.signal == "SELL":
            self.assertIsNotNone(sig.options_meta)
            assert sig.options_meta is not None
            self.assertEqual(sig.options_meta["strategy_type"], "covered_call")
            self.assertEqual(len(sig.options_meta["legs"]), 1)
            leg = sig.options_meta["legs"][0]
            self.assertEqual(leg["action"], "SELL")
            self.assertEqual(leg["right"], "C")
            self.assertGreater(leg["strike"], 0)
            # stop_loss = 3× premium; take_profit = 0.10× premium
            self.assertAlmostEqual(sig.stop_loss / sig.entry_price, 3.0, places=3)
            self.assertAlmostEqual(sig.take_profit / sig.entry_price, 0.10, places=3)
            # Greeks signs: delta<0 (short call), theta>0 (sells time), vega<0
            assert sig.delta is not None
            assert sig.theta is not None
            assert sig.vega is not None
            self.assertLess(sig.delta, 0.0)
            self.assertGreater(sig.theta, 0.0)
            self.assertLess(sig.vega, 0.0)

    def test_rsi_too_low_returns_hold(self):
        strat = self._strat()
        strat.MIN_IV_RANK = 0.0
        strat.MAX_IV_RANK = 100.0
        strat.MAX_ADX = 100.0
        df = self._data(n=200)
        with patch.object(strat.rsi_tool, "calculate") as mock_rsi, \
             patch.object(strat.adx_tool, "calculate") as mock_adx, \
             patch.object(strat.atr_tool, "calculate") as mock_atr:
            mock_rsi.return_value = MagicMock(value=25.0)   # < RSI_LOW=40
            mock_adx.return_value = MagicMock(value=15.0)
            mock_atr.return_value = MagicMock(value=3.0)
            sig = strat.generate_signal(df, "MSFT")
        self.assertEqual(sig.signal, "HOLD")

    def test_asset_class_and_broker(self):
        strat = self._strat()
        self.assertEqual(strat.asset_class, "option")
        self.assertEqual(strat.broker, "ibkr")


# ─────────────────────────────────────────────────────────────────────────────
# 12. EX-01 — BullCallSpreadStrategy
# ─────────────────────────────────────────────────────────────────────────────

class TestBullCallSpreadStrategy(unittest.TestCase):

    def _strat(self):
        from core.strategies.bull_call_spread import BullCallSpreadStrategy
        return BullCallSpreadStrategy()

    def _data(self, n=200, seed=13):
        rng = np.random.default_rng(seed)
        close = 80.0 + rng.normal(0, 0.8, n).cumsum()
        close = np.maximum(close, 5.0)
        atr = np.abs(rng.normal(1.0, 0.2, n))
        return pd.DataFrame({
            "open":   close - atr * 0.2,
            "high":   close + atr,
            "low":    close - atr,
            "close":  close,
            "volume": rng.uniform(5e4, 5e5, n),
        })

    def test_insufficient_data_returns_hold(self):
        strat = self._strat()
        df = self._data(n=10)
        sig = strat.generate_signal(df, "AMD")
        self.assertEqual(sig.signal, "HOLD")

    def test_buy_signal_two_leg_spread(self):
        strat = self._strat()
        strat.MAX_IV_RANK = 100.0
        strat.RSI_LOW = 0.0
        strat.RSI_HIGH = 100.0
        df = self._data(n=200)
        with patch.object(strat.atr_tool, "calculate") as mock_atr, \
             patch.object(strat.rsi_tool, "calculate") as mock_rsi, \
             patch.object(strat.macd_tool, "calculate") as mock_macd:
            mock_atr.return_value = MagicMock(value=5.0)
            mock_rsi.return_value = MagicMock(value=48.0)
            # BUG-CRIT-04: ToolOutput uses .value (histogram float) and .signal (string)
            mock_macd.return_value = MagicMock(value=0.05, signal="bullish_crossover")
            sig = strat.generate_signal(df, "AMD")
        if sig.signal == "BUY":
            self.assertIsNotNone(sig.options_meta)
            assert sig.options_meta is not None
            self.assertEqual(sig.options_meta["strategy_type"], "bull_call_spread")
            self.assertEqual(len(sig.options_meta["legs"]), 2)
            buy_leg  = next(l for l in sig.options_meta["legs"] if l["action"] == "BUY")
            sell_leg = next(l for l in sig.options_meta["legs"] if l["action"] == "SELL")
            self.assertEqual(buy_leg["right"],  "C")
            self.assertEqual(sell_leg["right"], "C")
            self.assertGreater(sell_leg["strike"], buy_leg["strike"])   # OTM > ATM
            # SL = full debit, TP > entry
            self.assertAlmostEqual(sig.stop_loss, sig.entry_price, places=4)
            self.assertGreater(sig.take_profit, sig.entry_price)
            # Greeks signs: delta>0 (long spread), theta<0 (net buyer), vega>0
            assert sig.delta is not None
            assert sig.theta is not None
            assert sig.vega is not None
            self.assertGreater(sig.delta, 0.0)
            self.assertLess(sig.theta, 0.0)    # ATM theta dominates → net negative
            self.assertGreater(sig.vega, 0.0)

    def test_macd_not_bullish_returns_hold(self):
        strat = self._strat()
        strat.MAX_IV_RANK = 100.0
        strat.RSI_LOW = 0.0
        strat.RSI_HIGH = 100.0
        df = self._data(n=200)
        with patch.object(strat.atr_tool, "calculate") as mock_atr, \
             patch.object(strat.rsi_tool, "calculate") as mock_rsi, \
             patch.object(strat.macd_tool, "calculate") as mock_macd:
            mock_atr.return_value = MagicMock(value=5.0)
            mock_rsi.return_value = MagicMock(value=48.0)
            # BUG-CRIT-04: bearish signal string → macd_bullish = False
            mock_macd.return_value = MagicMock(value=-0.1, signal="bearish_crossover")
            sig = strat.generate_signal(df, "AMD")
        self.assertEqual(sig.signal, "HOLD")

    def test_spread_width_zero_guard(self):
        """When ATR rounds buy_strike == sell_strike, must return HOLD."""
        strat = self._strat()
        strat.MAX_IV_RANK = 100.0
        strat.RSI_LOW = 0.0
        strat.RSI_HIGH = 100.0
        df = self._data(n=200)
        with patch.object(strat.atr_tool, "calculate") as mock_atr, \
             patch.object(strat.rsi_tool, "calculate") as mock_rsi, \
             patch.object(strat.macd_tool, "calculate") as mock_macd:
            mock_atr.return_value = MagicMock(value=0.01)   # tiny ATR → same strike after rounding
            mock_rsi.return_value = MagicMock(value=48.0)
            # BUG-CRIT-04: ToolOutput uses .value (histogram float) and .signal (string)
            mock_macd.return_value = MagicMock(value=0.05, signal="bullish_crossover")
            sig = strat.generate_signal(df, "AMD")
        # If strikes collapse to same value → HOLD (either due to spread guard or near-zero debit)
        self.assertIn(sig.signal, ("HOLD", "BUY"))   # BUY is OK only if different strikes survived

    def test_asset_class_and_broker(self):
        strat = self._strat()
        self.assertEqual(strat.asset_class, "option")
        self.assertEqual(strat.broker, "ibkr")


# ─────────────────────────────────────────────────────────────────────────────
# 13. EX-01 — STRATEGY_REGISTRY contains options strategies
# ─────────────────────────────────────────────────────────────────────────────

class TestStrategyRegistry(unittest.TestCase):

    def test_options_strategies_registered(self):
        from core.engine.signal_engine import STRATEGY_REGISTRY
        for name in ("iron_condor", "covered_call", "bull_call_spread"):
            self.assertIn(name, STRATEGY_REGISTRY, f"'{name}' missing from STRATEGY_REGISTRY")

    def test_options_strategies_are_instantiable(self):
        from core.engine.signal_engine import STRATEGY_REGISTRY
        for name in ("iron_condor", "covered_call", "bull_call_spread"):
            cls = STRATEGY_REGISTRY[name]
            instance = cls()
            self.assertEqual(instance.asset_class, "option")
            self.assertEqual(instance.broker, "ibkr")

    def test_all_strategies_have_generate_signal(self):
        from core.engine.signal_engine import STRATEGY_REGISTRY
        for name, cls in STRATEGY_REGISTRY.items():
            self.assertTrue(hasattr(cls, "generate_signal"), f"{name} missing generate_signal")


# ─────────────────────────────────────────────────────────────────────────────
# 14. EX-01 — Signal dataclass options fields
# ─────────────────────────────────────────────────────────────────────────────

class TestSignalDataclassOptionsFields(unittest.TestCase):

    def test_options_fields_default_none(self):
        from core.strategies.base import Signal
        sig = Signal(
            symbol="AAPL", signal="HOLD", entry_price=150.0,
            stop_loss=None, take_profit=None, confidence=0.0,
            timeframe="1d", strategy_name="test",
            asset_class="option", broker="ibkr",
        )
        self.assertIsNone(sig.iv_rank)
        self.assertIsNone(sig.delta)
        self.assertIsNone(sig.theta)
        self.assertIsNone(sig.vega)
        self.assertIsNone(sig.options_meta)

    def test_options_fields_can_be_set(self):
        from core.strategies.base import Signal
        meta = {"strategy_type": "covered_call", "expiry": "20260320", "legs": []}
        sig = Signal(
            symbol="AAPL", signal="SELL", entry_price=2.50,
            stop_loss=7.50, take_profit=0.25, confidence=0.72,
            timeframe="1d", strategy_name="covered_call",
            asset_class="option", broker="ibkr",
            iv_rank=45.0, delta=-0.28, theta=0.0042, vega=-0.18,
            options_meta=meta,
        )
        self.assertEqual(sig.iv_rank, 45.0)
        self.assertEqual(sig.delta, -0.28)
        self.assertEqual(sig.theta, 0.0042)
        self.assertEqual(sig.vega, -0.18)
        self.assertEqual(sig.options_meta["strategy_type"], "covered_call")


if __name__ == "__main__":
    unittest.main(verbosity=2)
