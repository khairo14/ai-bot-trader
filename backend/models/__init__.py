"""
ML models package.

Phase 1: ModelTrainer stub (logs only, no real training)
Phase 2: Full XGBoost training pipeline with feature engineering

See docs/strategies.md for the full feature set and ML architecture.
"""
from .trainer import ModelTrainer

__all__ = ["ModelTrainer"]
