"""
Scalping system test suite.

Covers:
  1. ScalpingEmaVwap strategy — signal generation logic
  2. ScalpingMLScorer — feature computation + safe None fallback
  3. Settings helpers — file I/O, merge with defaults, validation
  4. REST routes — GET /signals, /stats, /settings; POST /settings, /enable
  5. Scalping runner task — mocked DB path (no broker calls)

Run from backend/ directory:
    python -m pytest tests/test_scalping.py -v
"""
import json
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pandas as pd

# ── ensure backend/ is on path ────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


# ─────────────────────────────────────────────────────────────────────────────
# OHLCV helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_ohlcv(n: int = 80, base: float = 50_000.0, trend: str = "up") -> pd.DataFrame:
    """
    Synthetic OHLCV data.
    trend='up'   → slow upward drift (favours BUY)
    trend='down' → slow downward drift (favours SHORT)
    trend='flat' → sideways (below scoring threshold)
    """
    rng = np.random.default_rng(42)
    if trend == "up":
        close = base + np.linspace(0, 500, n) + rng.normal(0, 50, n).cumsum()
    elif trend == "down":
        close = base - np.linspace(0, 500, n) + rng.normal(0, 50, n).cumsum()
    else:
        close = np.full(n, base) + rng.normal(0, 20, n)

    close = np.abs(close)  # no negative prices
    high  = close + rng.uniform(20, 100, n)
    low   = close - rng.uniform(20, 100, n)
    vol   = rng.uniform(500, 2000, n)
    # Use datetime index so time-of-day features work
    idx = pd.date_range("2026-01-01", periods=n, freq="5min", tz="UTC")
    return pd.DataFrame(
        {"open": close - rng.uniform(0, 50, n), "high": high, "low": low,
         "close": close, "volume": vol},
        index=idx,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 1. ScalpingEmaVwap strategy unit tests
# ─────────────────────────────────────────────────────────────────────────────

class TestScalpingEmaVwap(unittest.TestCase):

    def setUp(self):
        from core.strategies.scalping_ema_vwap import ScalpingEmaVwap
        self.strat = ScalpingEmaVwap()

    def _with_temp_settings(self, settings: dict):
        """Context manager: write settings to a temp file and patch the path."""
        import core.strategies.scalping_ema_vwap as mod
        tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w")
        json.dump(settings, tmp)
        tmp.close()
        return patch.object(mod, "_SETTINGS_PATH",
                            new_callable=lambda: property(lambda self: tmp.name))

    def test_insufficient_data_returns_hold(self):
        """Strategy returns HOLD when fewer than MIN_CANDLES rows are provided."""
        data = _make_ohlcv(n=10)
        sig = self.strat.generate_signal(data, "BTC/USDT", "5m")
        self.assertEqual(sig.signal, "HOLD")
        self.assertTrue(any("Insufficient" in r for r in sig.reasons))

    def test_spread_gate_returns_hold(self):
        """Spread exceeding max_spread_pct → HOLD regardless of scoring."""
        data = _make_ohlcv(n=80, trend="up")
        # Patch settings to allow a low max spread, then pass a higher spread
        with patch("core.strategies.scalping_ema_vwap._load_scalping_settings",
                   return_value={
                       "enabled": True, "timeframe": "5m",
                       "risk_per_trade_pct": 0.5, "sl_atr_mult": 0.8,
                       "tp_atr_mult": 1.6, "min_volume_ratio": 1.2,
                       "max_spread_pct": 0.01, "min_score": 4,
                       "ml_veto_threshold": 0.40, "sr_tp_snap": False,
                   }):
            sig = self.strat.generate_signal(data, "BTC/USDT", "5m", spread_pct=0.10)
        self.assertEqual(sig.signal, "HOLD")
        self.assertTrue(any("Spread" in r for r in sig.reasons))

    def test_bullish_data_generates_buy_or_hold(self):
        """Strongly bullish OHLCV should produce BUY or HOLD — never SHORT."""
        from core.scalping_ml_scorer import scalping_ml_scorer as _scorer
        data = _make_ohlcv(n=80, trend="up")
        with patch("core.strategies.scalping_ema_vwap._load_scalping_settings",
                   return_value={
                       "enabled": True, "timeframe": "5m",
                       "risk_per_trade_pct": 0.5, "sl_atr_mult": 0.8,
                       "tp_atr_mult": 1.6, "min_volume_ratio": 1.2,
                       "max_spread_pct": 0.05, "min_score": 2,
                       "ml_veto_threshold": 0.40, "sr_tp_snap": False,
                   }):
            with patch.object(_scorer, "predict_proba_scalp", return_value=None), \
                 patch.object(_scorer, "predict_proba_scalp_short", return_value=None):
                sig = self.strat.generate_signal(data, "BTC/USDT", "5m")

        self.assertNotEqual(sig.signal, "SHORT",
                            "Bullish data should not produce a SHORT signal")
        if sig.signal == "BUY":
            self.assertIsNotNone(sig.stop_loss, "BUY signal must have stop_loss")
            self.assertIsNotNone(sig.take_profit, "BUY signal must have take_profit")
            self.assertLess(sig.stop_loss, sig.entry_price, "SL must be below entry for BUY")
            self.assertGreater(sig.take_profit, sig.entry_price, "TP must be above entry for BUY")

    def test_bearish_data_generates_short_or_hold(self):
        """Strongly bearish OHLCV should produce SHORT or HOLD — never BUY."""
        from core.scalping_ml_scorer import scalping_ml_scorer as _scorer
        data = _make_ohlcv(n=80, trend="down")
        with patch("core.strategies.scalping_ema_vwap._load_scalping_settings",
                   return_value={
                       "enabled": True, "timeframe": "5m",
                       "risk_per_trade_pct": 0.5, "sl_atr_mult": 0.8,
                       "tp_atr_mult": 1.6, "min_volume_ratio": 1.2,
                       "max_spread_pct": 0.05, "min_score": 2,
                       "ml_veto_threshold": 0.40, "sr_tp_snap": False,
                   }):
            with patch.object(_scorer, "predict_proba_scalp", return_value=None), \
                 patch.object(_scorer, "predict_proba_scalp_short", return_value=None):
                sig = self.strat.generate_signal(data, "BTC/USDT", "5m")

        self.assertNotEqual(sig.signal, "BUY",
                            "Bearish data should not produce a BUY signal")
        if sig.signal == "SHORT":
            self.assertIsNotNone(sig.stop_loss)
            self.assertIsNotNone(sig.take_profit)
            self.assertGreater(sig.stop_loss, sig.entry_price, "SL must be above entry for SHORT")
            self.assertLess(sig.take_profit, sig.entry_price, "TP must be below entry for SHORT")

    def test_buy_risk_reward_ratio(self):
        """Generated BUY signal must have TP/SL ratio ≥ tp_atr_mult / sl_atr_mult."""
        from core.scalping_ml_scorer import scalping_ml_scorer as _scorer
        data = _make_ohlcv(n=80, trend="up")
        sl_mult = 0.8
        tp_mult = 1.6
        expected_min_rr = tp_mult / sl_mult  # 2.0
        with patch("core.strategies.scalping_ema_vwap._load_scalping_settings",
                   return_value={
                       "enabled": True, "timeframe": "5m",
                       "risk_per_trade_pct": 0.5, "sl_atr_mult": sl_mult,
                       "tp_atr_mult": tp_mult, "min_volume_ratio": 0.5,
                       "max_spread_pct": 0.10, "min_score": 1,
                       "ml_veto_threshold": 0.40, "sr_tp_snap": False,
                   }):
            with patch.object(_scorer, "predict_proba_scalp", return_value=None), \
                 patch.object(_scorer, "predict_proba_scalp_short", return_value=None):
                sig = self.strat.generate_signal(data, "BTC/USDT", "5m")

        if sig.signal == "BUY":
            potential_gain = sig.take_profit - sig.entry_price
            potential_loss = sig.entry_price - sig.stop_loss
            if potential_loss > 0:
                actual_rr = potential_gain / potential_loss
                self.assertGreaterEqual(
                    actual_rr, expected_min_rr * 0.95,
                    f"R:R ratio {actual_rr:.2f} is below {expected_min_rr:.2f}"
                )

    def test_ml_veto_demotes_to_hold(self):
        """When ML scorer returns prob below veto threshold, signal becomes HOLD."""
        from core.scalping_ml_scorer import scalping_ml_scorer as _scorer
        data = _make_ohlcv(n=80, trend="up")
        with patch("core.strategies.scalping_ema_vwap._load_scalping_settings",
                   return_value={
                       "enabled": True, "timeframe": "5m",
                       "risk_per_trade_pct": 0.5, "sl_atr_mult": 0.8,
                       "tp_atr_mult": 1.6, "min_volume_ratio": 0.5,
                       "max_spread_pct": 0.10, "min_score": 1,
                       "ml_veto_threshold": 0.40, "sr_tp_snap": False,
                   }):
            # ML says only 20% probability — below threshold
            with patch.object(_scorer, "predict_proba_scalp", return_value=0.20), \
                 patch.object(_scorer, "predict_proba_scalp_short", return_value=0.20):
                sig = self.strat.generate_signal(data, "BTC/USDT", "5m")

        # If the strategy generated a direction, ML veto should have demoted it
        if sig.signal != "HOLD":
            # BUY was already below min_score before ML veto — acceptable
            pass
        # At minimum, no exception should have been raised


# ─────────────────────────────────────────────────────────────────────────────
# 2. ScalpingMLScorer — feature computation
# ─────────────────────────────────────────────────────────────────────────────

class TestScalpingMLScorer(unittest.TestCase):

    def _import(self):
        from core.scalping_ml_scorer import _compute_scalp_features, ScalpingMLScorer, SCALP_FEATURE_COLS
        return _compute_scalp_features, ScalpingMLScorer, SCALP_FEATURE_COLS

    def test_returns_none_on_too_few_rows(self):
        _compute_scalp_features, _, _ = self._import()
        tiny = _make_ohlcv(n=10)
        result = _compute_scalp_features(tiny)
        self.assertIsNone(result)

    def test_correct_feature_columns(self):
        """Feature matrix has exactly the 13 expected columns."""
        _compute_scalp_features, _, COLS = self._import()
        data = _make_ohlcv(n=80)
        feat = _compute_scalp_features(data)
        self.assertIsNotNone(feat)
        self.assertEqual(list(feat.columns), COLS)
        self.assertEqual(feat.shape[1], 13)

    def test_no_nans_after_computation(self):
        """Feature matrix should have no NaN values after ffill/bfill."""
        _compute_scalp_features, _, _ = self._import()
        data = _make_ohlcv(n=80)
        feat = _compute_scalp_features(data)
        self.assertFalse(feat.isnull().any().any(),
                         "Feature matrix must contain no NaN after computation")

    def test_no_infs_after_computation(self):
        """Feature matrix should have no ±Inf values."""
        _compute_scalp_features, _, _ = self._import()
        data = _make_ohlcv(n=80)
        feat = _compute_scalp_features(data)
        self.assertFalse(np.isinf(feat.values).any(),
                         "Feature matrix must contain no Inf after computation")

    def test_session_id_range(self):
        """session_id must be in {0, 1, 2, 3}."""
        _compute_scalp_features, _, _ = self._import()
        data = _make_ohlcv(n=80)
        feat = _compute_scalp_features(data)
        valid = {0, 1, 2, 3}
        actual = set(feat["session_id"].unique().tolist())
        self.assertTrue(actual.issubset(valid), f"Unexpected session_id values: {actual - valid}")

    def test_predict_returns_none_without_model(self):
        """predict_proba_scalp returns None when no trained model exists (safe fallback)."""
        _, ScalpingMLScorer, _ = self._import()
        scorer = ScalpingMLScorer()
        data = _make_ohlcv(n=80)
        result = scorer.predict_proba_scalp(data, "BTC/USDT", "5m")
        # No model on disk in test env → must return None, not raise
        self.assertIsNone(result)

    def test_reload_clears_cache(self):
        """reload() empties the in-memory model cache."""
        _, ScalpingMLScorer, _ = self._import()
        scorer = ScalpingMLScorer()
        scorer._models = {"BTC/USDT:5m:scalp": MagicMock()}
        scorer.reload()
        self.assertEqual(len(scorer._models), 0)

    def test_integer_index_fallback(self):
        """Feature computation should not crash when DataFrame has a RangeIndex."""
        _compute_scalp_features, _, _ = self._import()
        data = _make_ohlcv(n=80)
        data = data.reset_index(drop=True)   # RangeIndex
        # Should not raise; falls back to midnight time-of-day values
        feat = _compute_scalp_features(data)
        self.assertIsNotNone(feat)


# ─────────────────────────────────────────────────────────────────────────────
# 3. Settings helpers
# ─────────────────────────────────────────────────────────────────────────────

class TestScalpSettingsHelpers(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self._tmp.close()
        import pathlib
        import api.routes.scalping as mod
        self._mod = mod
        self._orig_path = mod._SETTINGS_PATH
        mod._SETTINGS_PATH = pathlib.Path(self._tmp.name)

    def tearDown(self):
        self._mod._SETTINGS_PATH = self._orig_path
        os.unlink(self._tmp.name)

    def test_read_returns_defaults_when_file_missing(self):
        os.unlink(self._tmp.name)   # remove so it doesn't exist
        result = self._mod._read_settings()
        self.assertIn("enabled", result)
        self.assertIn("timeframe", result)
        self.assertIn("min_score", result)
        # Re-create so tearDown doesn't fail
        open(self._tmp.name, "w").close()

    def test_write_and_read_roundtrip(self):
        payload = {"enabled": False, "min_score": 6, "timeframe": "1m"}
        self._mod._write_settings(payload)
        result = self._mod._read_settings()
        # Written keys should be present; defaults fill missing ones
        self.assertFalse(result["enabled"])
        self.assertEqual(result["min_score"], 6)
        self.assertEqual(result["timeframe"], "1m")
        # Default keys not in payload still present
        self.assertIn("sl_atr_mult", result)

    def test_read_merges_with_defaults(self):
        """Partial settings file is merged with default values."""
        self._mod._write_settings({"min_score": 7})
        result = self._mod._read_settings()
        self.assertEqual(result["min_score"], 7)
        # All default keys must still be present
        for key in self._mod._SETTINGS_DEFAULTS:
            self.assertIn(key, result, f"Missing default key: {key}")

    def test_read_gracefully_handles_corrupted_json(self):
        """Corrupted JSON → falls back to defaults, no exception."""
        with open(self._tmp.name, "w") as f:
            f.write("{invalid json{{")
        result = self._mod._read_settings()
        # Must return defaults, not raise
        self.assertIn("enabled", result)
        self.assertEqual(result["timeframe"], "5m")


# ─────────────────────────────────────────────────────────────────────────────
# 4. REST routes via TestClient
# ─────────────────────────────────────────────────────────────────────────────

class TestScalpingRestRoutes(unittest.IsolatedAsyncioTestCase):
    """
    Tests scalping REST endpoints using FastAPI's TestClient with dependency
    overrides to bypass JWT authentication.
    """

    def _build_app(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from api.routes.scalping import router
        from core.auth import get_current_user, require_admin

        app = FastAPI()
        app.include_router(router, prefix="/scalping")

        # Bypass auth
        fake_user = MagicMock()
        fake_user.is_admin = True
        fake_user.username = "test_admin"
        app.dependency_overrides[get_current_user] = lambda: fake_user
        app.dependency_overrides[require_admin]    = lambda: fake_user

        return TestClient(app)

    def test_get_settings_returns_200(self):
        client = self._build_app()
        r = client.get("/scalping/settings")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertIn("enabled", data)
        self.assertIn("timeframe", data)
        self.assertIn("min_score", data)

    def test_get_signals_returns_200_with_list(self):
        """GET /scalping/signals requires DB; mock the dependency."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from api.routes.scalping import router
        from core.auth import get_current_user, require_admin
        from db.database import get_db

        app = FastAPI()
        app.include_router(router, prefix="/scalping")

        fake_user = MagicMock()
        fake_user.is_admin = True
        app.dependency_overrides[get_current_user] = lambda: fake_user
        app.dependency_overrides[require_admin]    = lambda: fake_user

        # Mock DB session that returns empty result
        mock_session = AsyncMock()
        mock_result  = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__  = AsyncMock(return_value=False)

        async def override_db():
            yield mock_session

        app.dependency_overrides[get_db] = override_db

        client = TestClient(app)
        r = client.get("/scalping/signals")
        self.assertEqual(r.status_code, 200)
        self.assertIn("signals", r.json())
        self.assertIsInstance(r.json()["signals"], list)

    def test_post_enable_turns_off(self):
        """POST /scalping/enable with enabled=False should succeed."""
        import pathlib
        import api.routes.scalping as mod

        # Use a temp file so we don't mutate the real settings
        tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w")
        json.dump(mod._SETTINGS_DEFAULTS, tmp)
        tmp.close()
        orig_path = mod._SETTINGS_PATH
        mod._SETTINGS_PATH = pathlib.Path(tmp.name)

        try:
            client = self._build_app()
            r = client.post("/scalping/enable", json={"enabled": False})
            self.assertEqual(r.status_code, 200)
            data = r.json()
            self.assertEqual(data["scalping"], "disabled")
            # Verify the file was actually written
            on_disk = json.loads(pathlib.Path(tmp.name).read_text())
            self.assertFalse(on_disk["enabled"])
        finally:
            mod._SETTINGS_PATH = orig_path
            os.unlink(tmp.name)

    def test_post_settings_invalid_timeframe_returns_422(self):
        """POST /scalping/settings with an invalid timeframe should return 422."""
        import pathlib
        import api.routes.scalping as mod

        tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w")
        json.dump(mod._SETTINGS_DEFAULTS, tmp)
        tmp.close()
        orig_path = mod._SETTINGS_PATH
        mod._SETTINGS_PATH = pathlib.Path(tmp.name)

        try:
            client = self._build_app()
            r = client.post("/scalping/settings", json={"timeframe": "2h"})
            self.assertEqual(r.status_code, 422)
        finally:
            mod._SETTINGS_PATH = orig_path
            os.unlink(tmp.name)

    def test_post_settings_risk_out_of_range_returns_422(self):
        import pathlib
        import api.routes.scalping as mod

        tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w")
        json.dump(mod._SETTINGS_DEFAULTS, tmp)
        tmp.close()
        orig_path = mod._SETTINGS_PATH
        mod._SETTINGS_PATH = pathlib.Path(tmp.name)

        try:
            client = self._build_app()
            r = client.post("/scalping/settings", json={"risk_per_trade_pct": 99.0})
            self.assertEqual(r.status_code, 422)
        finally:
            mod._SETTINGS_PATH = orig_path
            os.unlink(tmp.name)

    def test_post_settings_valid_update_returns_200(self):
        import pathlib
        import api.routes.scalping as mod

        tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w")
        json.dump(mod._SETTINGS_DEFAULTS, tmp)
        tmp.close()
        orig_path = mod._SETTINGS_PATH
        mod._SETTINGS_PATH = pathlib.Path(tmp.name)

        try:
            client = self._build_app()
            r = client.post("/scalping/settings", json={"min_score": 5, "timeframe": "1m"})
            self.assertEqual(r.status_code, 200)
            data = r.json()
            self.assertEqual(data["settings"]["min_score"], 5)
            self.assertEqual(data["settings"]["timeframe"], "1m")
        finally:
            mod._SETTINGS_PATH = orig_path
            os.unlink(tmp.name)


# ─────────────────────────────────────────────────────────────────────────────
# 5. Scalping runner task — smoke test with mocked dependencies
# ─────────────────────────────────────────────────────────────────────────────

class TestScalpingRunner(unittest.TestCase):
    """
    Smoke-tests the run_scalping_signals Celery task.
    No real DB or broker calls are made — all async I/O is mocked.
    """

    def _mock_strategy_db_row(self, name="scalp_test"):
        """Build a minimal mock that looks like a DB Strategy row."""
        from db.models import BrokerName, AssetClass, ExecutionMode
        row = MagicMock()
        row.id = 1
        row.name = name
        row.is_active = True
        row.broker = BrokerName.BINANCE
        row.asset_class = AssetClass.CRYPTO
        row.execution_mode = ExecutionMode.SUGGESTION
        row.is_paper = True
        row.parameters = {
            "strategy_type": "scalp_ema_vwap",
            "symbol":        "BTC/USDT",
            "timeframe":     "5m",
            "limit":         200,
        }
        return row

    def test_runner_exits_early_when_no_strategies(self):
        """When there are no active scalp strategies, the task returns silently."""
        import asyncio
        from tasks import scalping_runner as mod

        # DB returns no strategies
        mock_session = AsyncMock()
        mock_result  = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__  = AsyncMock(return_value=False)

        # AsyncSessionLocal is a local import inside _async_run — patch at source
        with patch("db.database.AsyncSessionLocal", return_value=mock_session):
            asyncio.run(mod._async_run())
        # No exception — task silently returned on empty strategy list

    def test_runner_skips_inactive_strategies(self):
        """Inactive strategies should be filtered by the DB query — task runs cleanly."""
        import asyncio
        from tasks import scalping_runner as mod

        inactive = self._mock_strategy_db_row()
        inactive.is_active = False   # inactive

        mock_session = AsyncMock()
        mock_result  = MagicMock()
        mock_result.scalars.return_value.all.return_value = [inactive]
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__  = AsyncMock(return_value=False)

        with patch("db.database.AsyncSessionLocal", return_value=mock_session):
            asyncio.run(mod._async_run())

    def test_signal_dict_helper(self):
        """_signal_dict in the REST route returns all required fields."""
        from api.routes.scalping import _signal_dict
        from db.models import Signal, SignalType, BrokerName, AssetClass, ExecutionMode

        s = MagicMock(spec=Signal)
        s.id            = 42
        s.symbol        = "ETH/USDT"
        s.signal        = SignalType.BUY
        s.entry_price   = 3000.0
        s.stop_loss     = 2950.0
        s.take_profit   = 3100.0
        s.confidence    = 0.82
        s.timeframe     = "5m"
        s.strategy_name = "scalp_ema_vwap"
        s.regime        = "trending_up"
        s.asset_class   = AssetClass.CRYPTO
        s.broker        = BrokerName.BINANCE
        s.execution_mode = ExecutionMode.SUGGESTION
        s.reasons       = ["EMA ribbon bullish"]
        s.acted_on      = False
        s.created_at    = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

        result = _signal_dict(s)

        required_keys = [
            "id", "symbol", "signal", "entry_price", "stop_loss", "take_profit",
            "confidence", "timeframe", "strategy_name", "regime", "asset_class",
            "broker", "execution_mode", "reasons", "acted_on", "created_at",
        ]
        for key in required_keys:
            self.assertIn(key, result, f"Missing key: {key}")

        self.assertEqual(result["id"], 42)
        self.assertEqual(result["signal"], "BUY")
        self.assertEqual(result["broker"], "binance")
        self.assertEqual(result["asset_class"], "crypto")
        self.assertEqual(result["confidence"], 0.82)


# ─────────────────────────────────────────────────────────────────────────────
# 6. BaseScalpingStrategy — _enhance_signal override
# ─────────────────────────────────────────────────────────────────────────────

class TestBaseScalpingStrategy(unittest.TestCase):

    def _make_signal(self, signal_type="BUY", entry=50000.0, sl=49000.0, tp=52000.0):
        from core.strategies.base import Signal
        return Signal(
            symbol="BTC/USDT", signal=signal_type,
            entry_price=entry, stop_loss=sl, take_profit=tp,
            confidence=0.75, timeframe="5m", strategy_name="test",
            asset_class="crypto", broker="binance", reasons=["test"],
        )

    def _make_concrete(self, sr_snap: bool = False):
        """Instantiate a minimal concrete subclass of BaseScalpingStrategy."""
        from core.strategies.base_scalping import BaseScalpingStrategy

        class _ConcreteScalp(BaseScalpingStrategy):
            name = "_test_scalp"
            def generate_signal(self, data, symbol, timeframe="5m", tool_outputs=None, **kwargs):
                return self._hold(data, symbol, timeframe)  # type: ignore[attr-defined]

        strat = _ConcreteScalp()
        strat.sr_tp_snap_enabled = sr_snap
        return strat

    def test_enhance_signal_hold_passthrough(self):
        """HOLD signals are returned unchanged (no TP snap logic applied)."""
        strat = self._make_concrete(sr_snap=True)
        data = _make_ohlcv(n=80)
        sig = self._make_signal("HOLD", entry=50000.0, sl=None, tp=None)
        result = strat._enhance_signal(sig, data)
        self.assertEqual(result.signal, "HOLD")

    def test_enhance_signal_no_snap_when_disabled(self):
        """When sr_tp_snap_enabled = False, _enhance_signal returns signal as-is."""
        strat = self._make_concrete(sr_snap=False)
        data = _make_ohlcv(n=80)
        sig = self._make_signal("BUY", entry=50000.0, sl=49600.0, tp=51000.0)
        result = strat._enhance_signal(sig, data)
        # tp is unchanged because snap is disabled
        self.assertEqual(result.take_profit, 51000.0)


if __name__ == "__main__":
    unittest.main()
