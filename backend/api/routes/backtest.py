import csv
import io
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc
from pydantic import BaseModel
from typing import Optional
from datetime import datetime

from db.database import get_db
from db.models import BacktestResult

router = APIRouter()


def _backtest_dict(r: BacktestResult) -> dict:
    return {
        "id": r.id,
        "strategy_name": r.strategy_name,
        "symbol": r.symbol,
        "timeframe": r.timeframe,
        "start_date": r.start_date.isoformat() if r.start_date else None,
        "end_date": r.end_date.isoformat() if r.end_date else None,
        "initial_capital": r.initial_capital,
        "final_capital": r.final_capital,
        "total_return_pct": r.total_return_pct,
        "annualized_return_pct": r.annualized_return_pct,
        "max_drawdown_pct": r.max_drawdown_pct,
        "sharpe_ratio": r.sharpe_ratio,
        "sortino_ratio": r.sortino_ratio,
        "profit_factor": r.profit_factor,
        "win_rate_pct": r.win_rate_pct,
        "total_trades": r.total_trades,
        "avg_win": r.avg_win,
        "avg_loss": r.avg_loss,
        "rr_ratio": r.rr_ratio,
        "trades_detail": r.trades_detail,
        "parameters": r.parameters,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


class BacktestRequest(BaseModel):
    strategy_name: str
    symbol: str
    timeframe: str = "1h"
    start_date: str          # ISO format: "2024-01-01"
    end_date: str            # ISO format: "2025-01-01"
    initial_capital: float = 10000.0
    commission_pct: float = 0.1
    slippage_pct: float = 0.05
    risk_per_trade_pct: float = 2.0
    broker: str = "binance"
    parameters: Optional[dict] = None


# Broker capability rules:
#   Binance  — only X/Y crypto pairs (e.g. BTC/USDT)
#   Alpaca   — only plain stock tickers (e.g. AAPL)
#   IBKR     — plain tickers (stocks) OR X/Y forex pairs (e.g. EUR/USD) — both valid
_FX_CURRENCIES = {"USD","EUR","GBP","JPY","AUD","CAD","CHF","NZD","HKD","SGD"}

def _is_forex_pair(symbol: str) -> bool:
    """True if symbol looks like an FX pair (e.g. EUR/USD) rather than a crypto pair."""
    parts = symbol.upper().split("/")
    return len(parts) == 2 and all(p in _FX_CURRENCIES for p in parts)


def _validate_broker_symbol(broker: str, symbol: str):
    """Raise HTTPException 422 if broker/symbol combination is clearly wrong."""
    has_slash = "/" in symbol
    if has_slash:
        if broker == "alpaca":
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Symbol '{symbol}' contains '/' but broker 'alpaca' only supports "
                    f"stock tickers (e.g. AAPL, SPY). Switch the broker to 'binance' for "
                    f"crypto or 'ibkr' for forex pairs."
                ),
            )
        # IBKR accepts X/Y only for recognized FX pairs
        if broker == "ibkr" and not _is_forex_pair(symbol):
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Symbol '{symbol}' looks like a crypto pair. IBKR supports "
                    f"forex pairs (e.g. EUR/USD) and stock tickers — not crypto. "
                    f"Switch the broker to 'binance' for crypto."
                ),
            )
    else:
        # Plain ticker — only invalid on Binance
        if broker == "binance":
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Symbol '{symbol}' looks like a stock ticker but broker 'binance' "
                    f"only supports crypto pairs (e.g. BTC/USDT, ETH/USDT). "
                    f"Switch the broker to 'alpaca' or 'ibkr'."
                ),
            )


@router.post("/run")
async def run_backtest(request: BacktestRequest, db: AsyncSession = Depends(get_db)):
    """
    Trigger a backtest run for a strategy.
    Returns backtest result ID. Results are stored in DB.
    """
    _validate_broker_symbol(request.broker, request.symbol)

    try:
        start_dt = datetime.fromisoformat(request.start_date)
        end_dt   = datetime.fromisoformat(request.end_date)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid date format: {exc}. Use ISO format e.g. '2024-01-01'.")

    from core.engine.backtest_engine import BacktestEngine

    engine = BacktestEngine()
    result = await engine.run(
        strategy_name=request.strategy_name,
        symbol=request.symbol,
        timeframe=request.timeframe,
        start_date=start_dt,
        end_date=end_dt,
        initial_capital=request.initial_capital,
        commission_pct=request.commission_pct,
        slippage_pct=request.slippage_pct,
        risk_per_trade_pct=request.risk_per_trade_pct,
        broker=request.broker,
        parameters=request.parameters or {},
    )

    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])

    # F-020: whitelist known model fields to avoid spreading unknown/extra keys
    # from the engine directly into the ORM constructor.
    _ALLOWED = {
        "strategy_name", "symbol", "timeframe", "start_date", "end_date",
        "initial_capital", "final_capital", "total_return_pct",
        "annualized_return_pct", "max_drawdown_pct", "sharpe_ratio",
        "sortino_ratio", "profit_factor", "win_rate_pct", "total_trades",
        "avg_win", "avg_loss", "rr_ratio", "trades_detail", "parameters",
    }
    db_result = BacktestResult(**{k: v for k, v in result.items() if k in _ALLOWED})
    db.add(db_result)
    await db.commit()
    await db.refresh(db_result)

    return {"backtest_id": db_result.id, "result": result}


@router.get("/results")
async def list_results(db: AsyncSession = Depends(get_db)):
    """List all backtest results."""
    query = select(BacktestResult).order_by(desc(BacktestResult.created_at)).limit(100)
    result = await db.execute(query)
    return {"results": [_backtest_dict(r) for r in result.scalars().all()]}


@router.get("/results/{result_id}")
async def get_result(result_id: int, db: AsyncSession = Depends(get_db)):
    """Get a single backtest result."""
    result = await db.execute(
        select(BacktestResult).where(BacktestResult.id == result_id)
    )
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Backtest result not found")
    return _backtest_dict(item)


@router.get("/results/{result_id}/export")
async def export_backtest_trades(result_id: int, db: AsyncSession = Depends(get_db)):
    """Download trade-by-trade detail for one backtest run as CSV."""
    result = await db.execute(
        select(BacktestResult).where(BacktestResult.id == result_id)
    )
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Backtest result not found")

    trades = item.trades_detail or []
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["entry_time", "exit_time", "symbol", "side",
                     "entry_price", "exit_price", "quantity", "pnl", "pnl_pct", "exit_reason"])
    for t in trades:
        writer.writerow([
            t.get("entry_time", ""), t.get("exit_time", ""),
            t.get("symbol", ""), t.get("side", ""),
            t.get("entry_price", ""), t.get("exit_price", ""),
            t.get("quantity", ""), t.get("pnl", ""),
            t.get("pnl_pct", ""), t.get("exit_reason", ""),
        ])
    buf.seek(0)
    filename = f"backtest_{result_id}_{item.symbol.replace('/', '')}_{item.strategy_name}.csv"
    # Sanitize filename to prevent HTTP header injection
    import re
    filename = re.sub(r'[^\w\-.]', '_', filename)
    return StreamingResponse(
        buf,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/results/export/all")
async def export_all_backtest_summary(db: AsyncSession = Depends(get_db)):
    """Download a summary CSV of all backtest runs."""
    query = select(BacktestResult).order_by(desc(BacktestResult.created_at))
    result = await db.execute(query)
    items = result.scalars().all()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["id", "strategy_name", "symbol", "timeframe", "start_date", "end_date",
                     "initial_capital", "final_capital", "total_return_pct", "annualized_return_pct",
                     "max_drawdown_pct", "sharpe_ratio", "profit_factor", "win_rate_pct",
                     "total_trades", "avg_win", "avg_loss", "rr_ratio", "created_at"])
    for r in items:
        writer.writerow([
            r.id, r.strategy_name, r.symbol, r.timeframe,
            r.start_date.date() if r.start_date else "",
            r.end_date.date() if r.end_date else "",
            r.initial_capital, r.final_capital,
            r.total_return_pct, r.annualized_return_pct,
            r.max_drawdown_pct, r.sharpe_ratio, r.profit_factor,
            r.win_rate_pct, r.total_trades, r.avg_win, r.avg_loss, r.rr_ratio,
            r.created_at.isoformat() if r.created_at else "",
        ])
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=backtest_summary.csv"},
    )
