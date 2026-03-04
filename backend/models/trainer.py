"""
ML Model Trainer — placeholder for Phase 2 implementation.

The trainer:
1. Pulls 90 days of OHLCV + computed features from DB / broker
2. Labels outcomes: 1 if price rose > 1.5x ATR within 24h else 0
3. Trains XGBoost classifier with cross-validation
4. Evaluates on 20% holdout (min AUC 0.55 required)
5. Serialises model to disk with joblib (models/saved/<symbol>_<date>.pkl)
6. Registers model in DB for use by HybridStrategy

Phase 1: stub that logs and returns a no-op report.
Phase 2: full implementation with feature engineering pipeline.
"""
import logging
import datetime

logger = logging.getLogger(__name__)


class ModelTrainer:
    """Trains and evaluates ML models for each active trading symbol."""

    MODEL_DIR = "models/saved"

    async def retrain_all(self) -> dict:
        """
        Main entry point called by the Celery retrain task.
        Returns a summary report dict.
        """
        logger.info("ModelTrainer.retrain_all() — Phase 1 stub, no models trained yet.")
        return {
            "status": "stub",
            "timestamp": datetime.datetime.utcnow().isoformat(),
            "models_trained": 0,
            "message": "Full ML training pipeline is Phase 2. See docs/strategies.md for feature list.",
        }

    async def train_symbol(self, symbol: str, broker: str) -> dict:
        """Train or update model for a single symbol."""
        logger.info(f"train_symbol({symbol}, {broker}) — stub")
        return {"symbol": symbol, "status": "stub", "auc": None}
