from pydantic_settings import BaseSettings
from typing import List
import os

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

    # HTTP proxy (e.g. for local VPN: http://127.0.0.1:port — leave blank on VPS)
    http_proxy: str = ""

    # Alpaca
    alpaca_api_key: str = ""
    alpaca_api_secret: str = ""
    alpaca_base_url: str = "https://paper-api.alpaca.markets"
    alpaca_data_feed: str = "iex"

    # IBKR
    ibkr_host: str = "host.docker.internal"
    ibkr_port: int = 7497
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

    # CORS
    cors_origins: List[str] = ["http://localhost:3000", "http://localhost:5173"]

    class Config:
        env_file = _ENV_FILE
        env_file_encoding = "utf-8"
        case_sensitive = False
        extra = "ignore"


settings = Settings()
