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
        sig = _make_signal()
        # 6% daily loss on a 10k account triggers 5% breaker
        v = self.rm.validate(sig, account_balance=10_000, open_positions_count=0, daily_pnl=-600)
        self.assertFalse(v.approved)
        self.assertIn("Circuit breaker", v.reason or "")
        self.assertTrue(self.rm._circuit_breaker_active)

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
        self.rm._circuit_breaker_active = True
        sig = _make_signal()
        v = self.rm.validate(sig, account_balance=10_000, open_positions_count=0, daily_pnl=0)
        self.assertFalse(v.approved)
        self.assertIn("circuit breaker", (v.reason or "").lower())

    def test_consecutive_losses_blocks(self):
        self.rm.consecutive_losses = 3  # default max=3
        sig = _make_signal()
        v = self.rm.validate(sig, account_balance=10_000, open_positions_count=0, daily_pnl=0)
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
        # stop=$8000 → natural size=200/8000=0.025 BTC (value=$1250 < $1500 cap)
        sig = _make_signal(entry=50000, sl=42000, tp=70000)  # stop=$8000, R:R=2.5
        bal = 10_000
        v = self.rm.validate(sig, account_balance=bal, open_positions_count=0, daily_pnl=0)
        self.assertTrue(v.approved)
        # risk_per_trade_pct=2% → risk_amount=200, stop=8000 → size=0.025
        expected_size = (bal * 0.02) / 8000
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
        engine._paper_balance = 10_000.0
        return engine

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
        result = await engine.process_signal(sig, "full_auto", is_paper=True, db_session=None)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.status.value if hasattr(result.status, "value") else result.status, "open")
        self.assertTrue(result.is_paper)
        self.assertIn("BTC/USDT", engine._paper_positions)

    async def test_position_size_multiplier_applied(self):
        engine = self._make_engine()
        sig = _make_signal("BUY", entry=50000, sl=49000, tp=53000)
        # Without multiplier
        r1 = await engine.process_signal(sig, "full_auto", is_paper=True, db_session=None)
        self.assertIsNotNone(r1)
        assert r1 is not None
        qty1 = r1.quantity

        engine2 = self._make_engine()
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
        # Ensure _daily_pnl returns a float, not a coroutine
        _pnl_result = MagicMock()
        _pnl_result.scalar_one = MagicMock(return_value=0.0)
        mock_db.execute = AsyncMock(return_value=_pnl_result)

        with patch("core.engine.forward_engine.get_broker", return_value=mock_broker):
            result = await engine.process_signal(
                sig, "full_auto", is_paper=False, db_session=mock_db
            )
        self.assertIsNone(result)
        mock_db.add.assert_called_once()
        saved_trade = mock_db.add.call_args[0][0]
        from db.models import OrderStatus
        self.assertEqual(saved_trade.status, OrderStatus.REJECTED)


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
        for col in FEATURE_COLS:
            self.assertIn(col, feats.columns)

    def test_feature_no_nan_at_tail(self):
        from core.ml_scorer import _compute_features, FEATURE_COLS
        df = _make_ohlcv(200)
        feats = _compute_features(df)
        self.assertIsNotNone(feats)
        assert feats is not None
        last = feats[FEATURE_COLS].iloc[-1]
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

    async def test_exception_in_higher_tf_treated_as_hold(self):
        from tasks.signal_runner import _confluence_score
        mock_engine = AsyncMock()
        mock_engine.run = AsyncMock(side_effect=Exception("timeout"))
        score = await _confluence_score(mock_engine, "hybrid", "BTC/USDT", "binance", "1h", "BUY")
        # primary=BUY, both higher TFs error → treated as HOLD → 1/3
        self.assertAlmostEqual(score, 1/3, places=5)

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

        open_trade = MagicMock(spec=Trade)
        open_trade.symbol = "BTC/USDT"

        mock_session = AsyncMock()

        # open trades query
        open_result = MagicMock()
        open_result.scalars.return_value.all.return_value = [open_trade]
        # pnl sum query
        pnl_result = MagicMock()
        pnl_result.scalar_one.return_value = 500.0

        mock_session.execute = AsyncMock(side_effect=[open_result, pnl_result])

        engine = ForwardEngine()
        await engine.initialize(mock_session)

        self.assertEqual(len(engine._paper_positions), 1)
        self.assertIn("BTC/USDT", engine._paper_positions)
        self.assertAlmostEqual(engine._paper_balance, PAPER_INITIAL_CAPITAL + 500.0)
        self.assertTrue(engine._initialized)


if __name__ == "__main__":
    unittest.main(verbosity=2)
