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


# Symbols that contain '/' are crypto pairs — only valid on Binance
_CRYPTO_BROKERS = {"binance"}
_STOCK_BROKERS  = {"alpaca", "ibkr"}


def _validate_broker_symbol(broker: str, symbol: str):
    """Raise HTTPException 422 if broker/symbol combination is clearly wrong."""
    is_crypto_symbol = "/" in symbol
    if is_crypto_symbol and broker in _STOCK_BROKERS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Symbol '{symbol}' looks like a crypto pair but broker '{broker}' "
                f"only supports stock tickers (e.g. AAPL, SHOP, SPY). "
                f"Either change the symbol to a stock ticker or switch the broker to 'binance'."
            ),
        )
    if not is_crypto_symbol and broker in _CRYPTO_BROKERS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Symbol '{symbol}' looks like a stock ticker but broker '{broker}' "
                f"only supports crypto pairs (e.g. BTC/USDT, ETH/USDT). "
                f"Either use a crypto pair symbol or switch the broker to 'alpaca' or 'ibkr'."
            ),
        )


@router.post("/run")
async def run_backtest(request: BacktestRequest, db: AsyncSession = Depends(get_db)):
    """
    Trigger a backtest run for a strategy.
    Returns backtest result ID. Results are stored in DB.
    """
    _validate_broker_symbol(request.broker, request.symbol)

    from core.engine.backtest_engine import BacktestEngine

    engine = BacktestEngine()
    result = await engine.run(
        strategy_name=request.strategy_name,
        symbol=request.symbol,
        timeframe=request.timeframe,
        start_date=datetime.fromisoformat(request.start_date),
        end_date=datetime.fromisoformat(request.end_date),
        initial_capital=request.initial_capital,
        commission_pct=request.commission_pct,
        slippage_pct=request.slippage_pct,
        risk_per_trade_pct=request.risk_per_trade_pct,
        broker=request.broker,
        parameters=request.parameters or {},
    )

    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])

    db_result = BacktestResult(**result)
    db.add(db_result)
    await db.commit()
    await db.refresh(db_result)

    return {"backtest_id": db_result.id, "result": result}


@router.get("/results")
async def list_results(db: AsyncSession = Depends(get_db)):
    """List all backtest results."""
    query = select(BacktestResult).order_by(desc(BacktestResult.created_at)).limit(100)
    result = await db.execute(query)
    return {"results": result.scalars().all()}


@router.get("/results/{result_id}")
async def get_result(result_id: int, db: AsyncSession = Depends(get_db)):
    """Get a single backtest result."""
    result = await db.execute(
        select(BacktestResult).where(BacktestResult.id == result_id)
    )
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Backtest result not found")
    return item


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
    return StreamingResponse(
        buf,
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
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
