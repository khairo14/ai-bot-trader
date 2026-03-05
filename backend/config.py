from pydantic_settings import BaseSettings
from pydantic import model_validator
from typing import List
import os
from loguru import logger

# Resolve .env relative to this file so it works regardless of CWD
_ENV_FILE = os.path.join(os.path.dirname(__file__), "..", ".env")

class Settings(BaseSettings):
    # Application
    secret_key: str = "change_this"
    debug: bool = False
    log_level: str = "INFO"

    # Database
    database_url: str = "postgresql://trader:password@db:5432/ai_trader"

    # Redis
    redis_url: str = "redis://redis:6379/0"

    # Binance
    binance_api_key: str = ""
    binance_api_secret: str = ""
    binance_testnet: bool = True
    # Binance testnet (paper) keys — only needed when binance_testnet=True
    binance_api_key_testnet: str = ""
    binance_api_secret_testnet: str = ""

    # HTTP proxy (e.g. for local VPN: http://127.0.0.1:port — leave blank on VPS)
    http_proxy: str = ""

    # Alpaca
    alpaca_api_key: str = ""
    alpaca_api_secret: str = ""
    alpaca_base_url: str = "https://paper-api.alpaca.markets"
    alpaca_data_feed: str = "iex"
    # Alpaca live credentials (only needed when switching to live mode)
    alpaca_api_key_live: str = ""
    alpaca_api_secret_live: str = ""
    alpaca_base_url_live: str = "https://api.alpaca.markets"

    # IBKR
    ibkr_host: str = "host.docker.internal"
    ibkr_port: int = 7497
    ibkr_port_live: int = 7496
    ibkr_client_id: int = 1
    ibkr_paper: bool = True

    # Paper trading
    paper_initial_balance: float = 10000.0

    # Risk management
    risk_per_trade_pct: float = 2.0
    max_open_positions: int = 5
    daily_circuit_breaker_pct: float = 5.0
    max_daily_loss_per_strategy_pct: float = 5.0
    max_consecutive_losses: int = 3
    max_exposure_per_asset_pct: float = 15.0
    max_exposure_per_class_pct: float = 40.0
    default_rr_ratio: float = 2.0
    atr_stop_multiplier: float = 2.0

    # Forward test auto-scheduler toggle
    # Set to 0 to disable (manual "Run Now" only); any non-zero value enables it.
    # The actual run interval per strategy is derived from its own timeframe setting
    # (1m strat → runs every 60 s, 1h strat → every 3600 s, 1d → every 86400 s).
    forward_test_interval_minutes: int = 1  # non-zero = enabled

    # Internal API — used by Celery workers to call back into the FastAPI server
    # (e.g. flush MLScorer cache after weekly retrain)
    api_internal_url: str = "http://127.0.0.1:8000"

    # CORS
    cors_origins: List[str] = ["http://localhost:3000", "http://localhost:5173"]

    # OpenAI (optional — not used by core trading engine; reserved for future LLM features)
    openai_api_key: str = ""

    # Gmail notifications
    gmail_user: str = ""                  # your Gmail address
    gmail_app_password: str = ""          # 16-char App Password from Google
    notify_email_to: str = ""             # recipient (defaults to gmail_user if blank)
    notify_email_enabled: bool = False    # set to true to actually send emails

    class Config:
        env_file = _ENV_FILE
        env_file_encoding = "utf-8"
        case_sensitive = False
        extra = "ignore"

    @model_validator(mode="after")
    def _warn_missing_secrets(self) -> "Settings":
        """Warn at startup if critical API keys are missing (I-03)."""
        warnings = []
        if not self.binance_api_key:
            warnings.append("BINANCE_API_KEY")
        if not self.alpaca_api_key:
            warnings.append("ALPACA_API_KEY")
        if not self.alpaca_api_secret:
            warnings.append("ALPACA_API_SECRET")
        if self.database_url in ("postgresql://trader:password@db:5432/ai_trader", ""):
            warnings.append("DATABASE_URL (still at default)")
        if self.secret_key == "change_this":
            warnings.append("SECRET_KEY (still at default 'change_this')")
        if warnings:
            logger.warning(
                f"[Config] Missing or default secrets detected: {', '.join(warnings)}. "
                "Update your .env file."
            )
        return self


settings = Settings()
