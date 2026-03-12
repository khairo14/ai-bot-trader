import { useEffect, useState, useCallback } from 'react'
import { Link } from 'react-router-dom'
import { TrendingUp, TrendingDown, Minus, Activity, RefreshCw, Wifi, WifiOff, CheckCircle, XCircle, Clock, Trash2, Brain, BarChart2, Layers, X, Loader2, History } from 'lucide-react'
import axios from 'axios'
import toast from 'react-hot-toast'
import SignalCard from '../components/SignalCard'
import { SkeletonStat, SkeletonList } from '../components/Skeleton'
import MarketClock from '../components/MarketClock'
import { parseUtc } from '../lib/dates'

interface Signal {
  id: number
  symbol: string
  signal: string
  entry_price: number
  stop_loss: number
  take_profit: number
  confidence: number
  timeframe: string
  strategy_name: string
  regime: string
  asset_class: string
  broker: string
  execution_mode: string | null
  reasons: string[]
  acted_on: boolean
  dismissed: boolean
  created_at: string
}

interface BrokerBalance {
  broker: string
  total: number
  available: number
  currency: string
  connected: boolean
  is_paper: boolean
}

interface PortfolioSummary {
  brokers: BrokerBalance[]
  open_positions: number
  open_positions_by_broker: Record<string, number>
  max_positions: number
  today_pnl: number
  circuit_breaker_pct: number
  circuit_breaker_active: boolean
  per_broker_circuit_breaker: Record<string, { active: boolean; consecutive_losses: number }>
  today_pnl_by_broker: Record<string, number>
}

interface Trade {
  id: number
  symbol: string
  side: string
  quantity: number
  entry_price: number | null
  exit_price: number | null
  stop_loss: number | null
  take_profit: number | null
  pnl: number | null
  pnl_pct: number | null
  status: string
  broker: string
  strategy_name: string | null
  asset_class: string
  opened_at: string | null
  closed_at: string | null
}

function fmtPrice(price: number | null): string {
  if (price == null) return '—'
  const abs = Math.abs(price)
  if (abs >= 1000) return price.toFixed(2)
  if (abs >= 10)   return price.toFixed(3)
  if (abs >= 0.1)  return price.toFixed(5)  // 5 dp for forex range (1.33720 vs 1.33718 = 1 pip)
  return price.toFixed(6)                    // 6 dp for sub-penny crypto
}

function formatBalance(total: number, currency: string): string {
  if (currency === 'USD' || currency === 'USDT') {
    return '$' + total.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
  }
  return total.toFixed(4) + ' ' + currency
}

function formatPnl(pnl: number): string {
  const sign = pnl >= 0 ? '+' : '-'
  return sign + '$' + Math.abs(pnl).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

const StatCard = ({ label, value, sub, color = 'text-white' }: { label: string; value: string; sub?: string; color?: string }) => (
  <div className="bg-dark-800 border border-dark-600 rounded-xl p-4">
    <p className="text-xs text-gray-500 uppercase tracking-wider mb-1">{label}</p>
    <p className={`text-2xl font-bold ${color}`}>{value}</p>
    {sub && <p className="text-xs text-gray-500 mt-0.5">{sub}</p>}
  </div>
)

interface MLStatus {
  model_count: number
  last_retrain: string | null
  outcomes_total: number
  outcomes_resolved: number
  outcomes_pending: number
  win_rate_pct: number | null
  avg_pnl_pct: number | null
  feedback_loop_active: boolean
  models: { symbol: string; trained_date: string | null }[]
}

interface OpenPosition {
  id: number
  symbol: string
  side: string
  quantity: number
  entry_price: number | null
  stop_loss: number | null
  take_profit: number | null
  pnl: number | null
  pnl_pct: number | null
  broker: string
  strategy_name: string | null
  is_paper: boolean
  opened_at: string | null
}

interface RegimeInfo {
  regime: string
  symbol: string
  timeframe: string
  features: { adx: number; atr_norm: number; bb_width: number; ema_slope: number }
  score_adjustment: { long_delta: number; short_delta: number }
}

interface WeightEntry {
  strategy_name: string
  weight: number
  symbol?: string
  timeframe?: string
}

interface WeightsData {
  weighted_strategies: WeightEntry[]
  unweighted_strategies: WeightEntry[]
  total_weight: number
  optimized: boolean
}

// Per-broker label for the "available" balance field
const AVAILABLE_LABEL: Record<string, string> = {
  binance: 'free',
  alpaca:  'buying power',
  ibkr:    'available funds',
}

const REGIME_STYLES: Record<string, { label: string; color: string; bg: string; border: string }> = {
  trending_up:    { label: 'Trending Up',    color: 'text-green-400',  bg: 'bg-green-900/20',  border: 'border-green-700/40' },
  trending_down:  { label: 'Trending Down',  color: 'text-red-400',    bg: 'bg-red-900/20',    border: 'border-red-700/40' },
  ranging:        { label: 'Ranging',        color: 'text-yellow-400', bg: 'bg-yellow-900/20', border: 'border-yellow-700/40' },
  high_volatility:{ label: 'High Volatility',color: 'text-orange-400', bg: 'bg-orange-900/20', border: 'border-orange-700/40' },
  low_volatility: { label: 'Low Volatility', color: 'text-blue-400',   bg: 'bg-blue-900/20',   border: 'border-blue-700/40' },
}


export default function Dashboard() {
  const [signals, setSignals] = useState<Signal[]>([])
  const [pendingSignals, setPendingSignals] = useState<Signal[]>([])
  const [portfolio, setPortfolio] = useState<PortfolioSummary | null>(null)
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [actioning, setActioning] = useState<number | null>(null)
  const [mlStatus, setMlStatus] = useState<MLStatus | null>(null)
  const [regime, setRegime] = useState<RegimeInfo | null>(null)
  const [regimeError, setRegimeError] = useState(false)
  const [portfolioWeights, setPortfolioWeights] = useState<WeightsData | null>(null)
  const [optimizing, setOptimizing] = useState(false)
  const [openPositions, setOpenPositions] = useState<OpenPosition[]>([])
  const [trades, setTrades] = useState<Trade[]>([])
  const [tradePage, setTradePage] = useState(1)
  const TRADES_PER_PAGE = 8
  const [closingId, setClosingId] = useState<number | null>(null)

  const enrichPositionsWithLivePnl = useCallback(async (positions: OpenPosition[]): Promise<OpenPosition[]> => {
    if (positions.length === 0) return positions

    // Priority 1: broker live-pnl (works outside market hours, no SIP restriction)
    const brokerPnlMap: Record<string, { unrealized_pnl: number; current_price: number }> = {}
    try {
      const res = await axios.get('/api/positions/live-pnl')
      const data: Record<string, { symbol: string; unrealized_pnl: number; current_price: number }[]> = res.data
      for (const [broker, bPositions] of Object.entries(data)) {
        for (const bp of bPositions) {
          brokerPnlMap[`${bp.symbol}|${broker}`] = {
            unrealized_pnl: bp.unrealized_pnl,
            current_price: bp.current_price,
          }
        }
      }
    } catch { /* fall through to candle enrichment */ }

    // Priority 2: candle fallback for positions not covered by broker API
    const uncoveredPairs = [...new Set(
      positions
        .filter(p => p.pnl == null && !brokerPnlMap[`${p.symbol}|${p.broker}`])
        .map(p => `${p.symbol}|${p.broker}`)
    )]
    const now = Date.now()
    const priceMap: Record<string, number> = {}
    if (uncoveredPairs.length > 0) {
      await Promise.allSettled(
        uncoveredPairs.map(async (key) => {
          const [symbol, broker] = key.split('|')
          try {
            const res = await axios.get('/api/charts/candles', {
              params: { symbol, broker, timeframe: '1m', since: now - 10 * 60_000, until: now }
            })
            const candles: { close: number }[] = res.data.candles ?? []
            if (candles.length > 0) priceMap[key] = candles[candles.length - 1].close
          } catch { /* silently skip */ }
        })
      )
    }

    return positions.map(p => {
      if (p.pnl != null) return p

      // Use broker live-pnl if available
      const brokerData = brokerPnlMap[`${p.symbol}|${p.broker}`]
      if (brokerData && p.entry_price) {
        const isBuy = p.side === 'buy' || p.side === 'long'
        const sign  = isBuy ? 1 : -1
        const pnl_pct = p.entry_price !== 0
          ? sign * (brokerData.current_price / p.entry_price - 1) * 100
          : 0
        return {
          ...p,
          pnl: parseFloat(brokerData.unrealized_pnl.toFixed(4)),
          pnl_pct: parseFloat(pnl_pct.toFixed(4)),
        }
      }

      // Fall back to candle-derived price
      const currentPrice = priceMap[`${p.symbol}|${p.broker}`]
      if (!currentPrice || !p.entry_price) return p
      const isBuy = p.side === 'buy' || p.side === 'long'
      const sign  = isBuy ? 1 : -1
      const pnl     = sign * (currentPrice - p.entry_price) * p.quantity
      // M-5 FIX: guard against zero entry_price to avoid NaN/Infinity in pct
      const pnl_pct = p.entry_price !== 0
        ? sign * (currentPrice / p.entry_price - 1) * 100
        : 0
      return { ...p, pnl: parseFloat(pnl.toFixed(4)), pnl_pct: parseFloat(pnl_pct.toFixed(4)) }
    })
  }, [])

  const fetchAll = async (showSpinner = false) => {
    if (showSpinner) setRefreshing(true)
    // Fetch independently — a slow broker never blocks signals from loading
    const [sigResult, portResult, pendingResult, mlResult, weightsResult, posResult, tradesResult] = await Promise.allSettled([
      axios.get('/api/signals/?limit=20'),
      axios.get('/api/portfolio/summary'),
      axios.get('/api/signals/pending'),
      axios.get('/api/ml/status'),
      axios.get('/api/portfolio-optimizer/weights'),
      axios.get('/api/positions/open'),
      axios.get('/api/forward-test/trades?limit=100&status=filled'),
    ])
    if (sigResult.status === 'fulfilled') setSignals(sigResult.value.data.signals || [])
    if (portResult.status === 'fulfilled') setPortfolio(portResult.value.data)
    if (pendingResult.status === 'fulfilled') setPendingSignals(pendingResult.value.data.signals || [])
    if (mlResult.status === 'fulfilled') setMlStatus(mlResult.value.data)
    if (weightsResult.status === 'fulfilled') setPortfolioWeights(weightsResult.value.data)
    if (posResult.status === 'fulfilled') {
      const enriched = await enrichPositionsWithLivePnl(posResult.value.data.positions || [])
      setOpenPositions(enriched)
    }
    if (tradesResult.status === 'fulfilled') setTrades(tradesResult.value.data.trades || [])

    // Unblock the loading skeleton immediately after core data arrives.
    // The regime badge fetches separately below and updates when ready.
    setLoading(false)
    setRefreshing(false)

    // Derive regime from the most recent non-HOLD signal so the badge reflects
    // what the bot is actually trading, not a hardcoded BTC/USDT default.
    const latestSig = sigResult.status === 'fulfilled'
      ? (sigResult.value.data.signals || []).find((s: Signal) => s.signal !== 'HOLD')
      : null
    const regimeSymbol    = latestSig?.symbol    ?? 'BTC/USDT'
    const regimeTimeframe = latestSig?.timeframe  ?? '1h'
    const regimeBroker    = latestSig?.broker     ?? 'binance'
    try {
      const regimeRes = await axios.get(
        `/api/regime?symbol=${encodeURIComponent(regimeSymbol)}&timeframe=${regimeTimeframe}&broker=${regimeBroker}`
      )
      setRegime(regimeRes.data)
      setRegimeError(false)
    } catch {
      setRegime(null)
      setRegimeError(true)
    }
  }

  const closePosition = async (id: number) => {
    setClosingId(id)
    try {
      await axios.post('/api/positions/close', { trade_id: id, reason: 'manual_override' })
      toast.success('Position closed at market price.')
      await fetchAll()
    } catch (e: any) {
      toast.error(e?.response?.data?.detail ?? 'Close failed')
    } finally {
      setClosingId(null)
    }
  }

  const handleApprove = async (id: number) => {
    setActioning(id)
    try {
      await axios.post(`/api/forward-test/execute-signal/${id}`)
      toast.success('Signal approved and executed.')
      fetchAll()
    } catch (e: any) {
      toast.error(e?.response?.data?.detail || 'Approval failed')
    } finally {
      setActioning(null)
    }
  }

  const handleReject = async (id: number) => {
    setActioning(id)
    try {
      await axios.post(`/api/signals/${id}/reject`)
      toast.success('Signal dismissed.')
      fetchAll()
    } catch (e: any) {
      toast.error(e?.response?.data?.detail || 'Rejection failed')
    } finally {
      setActioning(null)
    }
  }

  useEffect(() => {
    fetchAll()
    const interval = setInterval(() => fetchAll(), 30000)

    // Re-fetch immediately when the tab becomes visible again (U-02)
    const onVisibility = () => { if (document.visibilityState === 'visible') fetchAll() }
    document.addEventListener('visibilitychange', onVisibility)

    return () => {
      clearInterval(interval)
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [])

  const buySignals = signals.filter(s => s.signal === 'BUY').length
  const sellSignals = signals.filter(s => s.signal === 'SELL' || s.signal === 'SHORT').length
  const pnlColor = !portfolio ? 'text-white' : portfolio.today_pnl >= 0 ? 'text-green-400' : 'text-red-400'

  return (
    <div className="p-6 space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-bold text-white">Dashboard</h1>
          <p className="text-sm text-gray-500 mt-0.5">Live signals, positions, and portfolio overview</p>
        </div>
        <div className="flex items-center gap-2">
          <MarketClock />
        </div>
      </div>

      {/* Broker Balances */}
      {/* Always show all 3 broker cards; merge live API data when available */}
      <div className="grid grid-cols-3 gap-4">
        {(
          [
            { broker: 'binance', connected: false, total: 0, available: 0, currency: 'USDT', is_paper: false },
            { broker: 'alpaca',  connected: false, total: 0, available: 0, currency: 'USD',  is_paper: true  },
            { broker: 'ibkr',   connected: false, total: 0, available: 0, currency: 'USD',  is_paper: true  },
          ] as BrokerBalance[]
        ).map(d => {
          const b = portfolio?.brokers.find(x => x.broker === d.broker) ?? d
          return (
            <div key={b.broker} className={`bg-dark-800 border rounded-xl p-4 ${b.connected ? 'border-dark-600' : 'border-dark-700 opacity-60'}`}>
              <div className="flex items-center justify-between mb-2">
                <p className="text-xs text-gray-500 uppercase tracking-wider">{b.broker}</p>
                <div className="flex items-center gap-1.5">
                  {b.connected && !b.is_paper && <span className="text-xs text-red-400 bg-red-900/20 px-1.5 py-0.5 rounded">live</span>}
                  {b.is_paper && <span className="text-xs text-yellow-500 bg-yellow-900/20 px-1.5 py-0.5 rounded">paper</span>}
                  {b.connected
                    ? <Wifi size={12} className="text-green-400" />
                    : <WifiOff size={12} className="text-gray-600" />}
                </div>
              </div>
              <p className={`text-2xl font-bold ${b.connected ? 'text-white' : 'text-gray-600'}`}>
                {b.connected ? formatBalance(b.total, b.currency) : '—'}
              </p>
              <p className="text-xs text-gray-500 mt-0.5">
                {b.connected
                  ? `${formatBalance(b.available, b.currency)} ${AVAILABLE_LABEL[b.broker] ?? 'available'}`
                  : 'Offline — not configured'}
              </p>
              {b.connected && portfolio?.today_pnl_by_broker && (() => {
                const bpnl = portfolio.today_pnl_by_broker[b.broker] ?? 0
                return (
                  <p className={`text-xs font-medium mt-1 ${bpnl >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                    {formatPnl(bpnl)} today
                  </p>
                )
              })()}
            </div>
          )
        })}
      </div>

      {/* Stats Row */}
      <div className="grid grid-cols-3 gap-4">
        {/* Open Positions */}
        <div className="bg-dark-800 border border-dark-600 rounded-xl p-4">
          <p className="text-xs text-gray-500 uppercase tracking-wider mb-1">Open Positions</p>
          <p className="text-2xl font-bold text-white">{portfolio ? String(portfolio.open_positions) : '—'}</p>
          {portfolio && (
            <p className="text-xs text-gray-500 mt-0.5">
              {`of ${portfolio.max_positions} max`}
              {Object.keys(portfolio.open_positions_by_broker ?? {}).length > 0 && (
                <span className="ml-1 text-gray-600">
                  ({Object.entries(portfolio.open_positions_by_broker).map(([b, c]) => `${b} ${c}`).join(', ')})
                </span>
              )}
            </p>
          )}
        </div>

        {/* Today's P&L */}
        <StatCard
          label="Today's P&L"
          value={portfolio ? formatPnl(portfolio.today_pnl) : '—'}
          color={pnlColor}
        />

        {/* Circuit Breaker */}
        <div className="bg-dark-800 border border-dark-600 rounded-xl p-4">
          <p className="text-xs text-gray-500 uppercase tracking-wider mb-1">Circuit Breaker</p>
          <p className={`text-2xl font-bold ${
            !portfolio ? 'text-white'
            : portfolio.circuit_breaker_active ? 'text-red-400'
            : 'text-brand-500'
          }`}>
            {!portfolio ? '—'
              : portfolio.circuit_breaker_active ? 'TRIPPED'
              : `${portfolio.circuit_breaker_pct}% limit`}
          </p>
          {portfolio && (() => {
            const tripped = Object.entries(portfolio.per_broker_circuit_breaker ?? {})
              .filter(([, v]) => v.active)
              .map(([b]) => b)
            return (
              <p className="text-xs mt-0.5">
                {tripped.length > 0
                  ? <span className="text-red-400">{tripped.join(', ')} tripped</span>
                  : <span className="text-gray-500">Daily max loss — all clear</span>}
              </p>
            )
          })()}
        </div>
      </div>

      {/* Signal summary */}
      {loading ? (
        <div className="grid grid-cols-3 gap-4">
          <SkeletonStat /><SkeletonStat /><SkeletonStat />
        </div>
      ) : (
        <div className="grid grid-cols-3 gap-4">
          <div className="bg-dark-800 border border-dark-600 rounded-xl p-4 flex items-center gap-3">
            <div className="p-2 rounded-lg bg-green-900/30">
              <TrendingUp size={18} className="text-green-400" />
            </div>
            <div>
              <p className="text-xs text-gray-500">Buy Signals</p>
              <p className="text-lg font-bold text-green-400">{buySignals}</p>
            </div>
          </div>
          <div className="bg-dark-800 border border-dark-600 rounded-xl p-4 flex items-center gap-3">
            <div className="p-2 rounded-lg bg-red-900/30">
              <TrendingDown size={18} className="text-red-400" />
            </div>
            <div>
              <p className="text-xs text-gray-500">Short / Sell Signals</p>
              <p className="text-lg font-bold text-red-400">{sellSignals}</p>
            </div>
          </div>
          <div className="bg-dark-800 border border-dark-600 rounded-xl p-4 flex items-center gap-3">
            <div className="p-2 rounded-lg bg-gray-800">
              <Minus size={18} className="text-gray-400" />
            </div>
            <div>
              <p className="text-xs text-gray-500">Hold</p>
              <p className="text-lg font-bold text-gray-400">{signals.filter(s => s.signal === 'HOLD').length}</p>
            </div>
          </div>
        </div>
      )}

      {/* ML Feedback Loop Status */}
      <div className={`bg-dark-800 border rounded-xl p-4 ${mlStatus?.feedback_loop_active ? 'border-brand-500/40' : 'border-dark-600'}`}>
        <div className="flex items-center justify-between mb-3">
          <div className="flex items-center gap-2">
            <Brain size={15} className={mlStatus?.feedback_loop_active ? 'text-brand-400' : 'text-gray-500'} />
            <span className="text-sm font-semibold text-gray-300">ML Feedback Loop</span>
            {mlStatus?.feedback_loop_active
              ? <span className="text-xs bg-brand-500/15 text-brand-400 px-2 py-0.5 rounded-full">Active</span>
              : <span className="text-xs bg-dark-700 text-gray-500 px-2 py-0.5 rounded-full">Warming up</span>
            }
          </div>
          <div className="flex items-center gap-3">
            {/* Regime Badge */}
            {regime && (() => {
              const rs = REGIME_STYLES[regime.regime] ?? { label: regime.regime, color: 'text-gray-400', bg: 'bg-dark-700', border: 'border-dark-600' }
              return (
                <div className={`flex items-center gap-1.5 px-2.5 py-1 rounded-lg border ${rs.bg} ${rs.border}`}>
                  <BarChart2 size={11} className={rs.color} />
                  <span className={`text-xs font-semibold ${rs.color}`}>{rs.label}</span>
                  <span className="text-xs text-gray-500">{regime.symbol} {regime.timeframe}</span>
                </div>
              )
            })()}
            {/* L-6 FIX: show error badge when regime fetch fails instead of blank */}
            {!regime && regimeError && (
              <div className="flex items-center gap-1.5 px-2.5 py-1 rounded-lg border bg-dark-700 border-red-800">
                <BarChart2 size={11} className="text-red-500" />
                <span className="text-xs font-semibold text-red-400">Regime unavailable</span>
              </div>
            )}
            {mlStatus?.last_retrain && (
              <span className="text-xs text-gray-500">
                Last retrain: {parseUtc(mlStatus.last_retrain)?.toLocaleDateString()}
              </span>
            )}
          </div>
        </div>
        <div className="grid grid-cols-4 gap-3">
          <div className="bg-dark-700 rounded-lg p-3">
            <p className="text-xs text-gray-500 mb-1">Models Trained</p>
            <p className="text-lg font-bold text-white">{mlStatus?.model_count ?? '—'}</p>
            <p className="text-xs text-gray-600 mt-0.5 truncate">
              {mlStatus?.models.map(m => m.symbol).join(', ') || 'none yet'}
            </p>
          </div>
          <div className="bg-dark-700 rounded-lg p-3">
            <p className="text-xs text-gray-500 mb-1">Outcomes Tracked</p>
            <p className="text-lg font-bold text-white">{mlStatus?.outcomes_total ?? '—'}</p>
            <p className="text-xs text-gray-600 mt-0.5">{mlStatus?.outcomes_pending ?? 0} pending resolution</p>
          </div>
          <div className="bg-dark-700 rounded-lg p-3">
            <p className="text-xs text-gray-500 mb-1">Win Rate</p>
            <p className={`text-lg font-bold ${mlStatus?.win_rate_pct != null ? (mlStatus.win_rate_pct >= 50 ? 'text-green-400' : 'text-red-400') : 'text-gray-500'}`}>
              {mlStatus?.win_rate_pct != null ? `${mlStatus.win_rate_pct}%` : '—'}
            </p>
            <p className="text-xs text-gray-600 mt-0.5">{mlStatus?.outcomes_resolved ?? 0} resolved</p>
          </div>
          <div className="bg-dark-700 rounded-lg p-3">
            <p className="text-xs text-gray-500 mb-1">Avg Signal P&L</p>
            <p className={`text-lg font-bold ${mlStatus?.avg_pnl_pct != null ? (mlStatus.avg_pnl_pct >= 0 ? 'text-green-400' : 'text-red-400') : 'text-gray-500'}`}>
              {mlStatus?.avg_pnl_pct != null ? `${mlStatus.avg_pnl_pct > 0 ? '+' : ''}${mlStatus.avg_pnl_pct}%` : '—'}
            </p>
            <p className="text-xs text-gray-600 mt-0.5">per signal</p>
          </div>
        </div>
        {!mlStatus?.feedback_loop_active && (
          <p className="text-xs text-gray-600 mt-3">
            Outcomes are collected as the bot fires signals. The resolver runs nightly to measure win/loss. After the first batch resolves, win rate and model accuracy will appear here.
          </p>
        )}
      </div>

      {/* Portfolio Optimizer Weights (ML-03) */}
      <div className="bg-dark-800 border border-dark-600 rounded-xl p-4">
        <div className="flex items-center justify-between mb-3">
          <div className="flex items-center gap-2">
            <Layers size={14} className="text-brand-400" />
            <span className="text-sm font-semibold text-gray-300">Strategy Allocation</span>
            {portfolioWeights?.optimized && (
              <span className="text-xs bg-brand-500/15 text-brand-400 px-2 py-0.5 rounded-full">Sharpe-optimized</span>
            )}
          </div>
          <button
            disabled={optimizing}
            onClick={async () => {
              setOptimizing(true)
              try {
                await axios.post('/api/portfolio-optimizer/run')
                toast.success('Portfolio weights updated')
                const r = await axios.get('/api/portfolio-optimizer/weights')
                setPortfolioWeights(r.data)
              } catch (e: any) {
                toast.error(e?.response?.data?.detail || 'Optimization failed')
              }
              setOptimizing(false)
            }}
            className="flex items-center gap-1.5 text-xs text-gray-400 hover:text-white px-3 py-1.5 bg-dark-700 border border-dark-600 rounded-lg transition-colors disabled:opacity-50"
          >
            <RefreshCw size={11} className={optimizing ? 'animate-spin' : ''} />
            {optimizing ? 'Optimizing…' : 'Optimize Now'}
          </button>
        </div>
        {portfolioWeights && portfolioWeights.weighted_strategies.length > 0 ? (
          <div className="space-y-2">
            {portfolioWeights.weighted_strategies.map(s => {
              const pct = Math.round(s.weight * 100)
              return (
                <div key={s.strategy_name} className="flex items-center gap-3">
                  <span className="text-xs text-gray-400 w-44 truncate" title={s.strategy_name}>{s.strategy_name}</span>
                  <div className="flex-1 h-1.5 bg-dark-700 rounded-full overflow-hidden">
                    <div
                      className="h-full rounded-full bg-brand-500 transition-all duration-700"
                      style={{ width: `${pct}%` }}
                    />
                  </div>
                  <span className="text-xs font-mono text-gray-300 w-8 text-right">{pct}%</span>
                </div>
              )
            })}
            {portfolioWeights.unweighted_strategies.length > 0 && (
              <p className="text-xs text-gray-600 pt-1">
                {portfolioWeights.unweighted_strategies.length} strateg{portfolioWeights.unweighted_strategies.length > 1 ? 'ies' : 'y'} without enough history for optimization
              </p>
            )}
          </div>
        ) : (
          <p className="text-xs text-gray-600">
            No optimization data yet — click "Optimize Now" to compute Sharpe-weighted allocations from trade history.
          </p>
        )}
      </div>

      {/* Pending Approvals (semi-auto signals awaiting confirmation) */}
      {pendingSignals.length > 0 && (
        <div>
          <div className="flex items-center gap-2 mb-3">
            <Clock size={14} className="text-yellow-400" />
            <h2 className="text-sm font-semibold text-yellow-400">Pending Approvals</h2>
            <span className="text-xs bg-yellow-500/15 text-yellow-400 px-2 py-0.5 rounded-full">{pendingSignals.length}</span>
          </div>
          <div className="space-y-2">
            {pendingSignals.map(s => (
              <div key={s.id} className="bg-dark-800 border border-yellow-900/40 rounded-xl p-4 flex items-center gap-4">
                <div className="flex-1">
                  <div className="flex items-center gap-2 mb-1">
                    <span className={`text-xs font-bold px-2 py-0.5 rounded ${s.signal === 'BUY' ? 'bg-green-900/30 text-green-400' : 'bg-red-900/30 text-red-400'}`}>
                      {s.signal}
                    </span>
                    <span className="text-sm font-semibold text-white">{s.symbol}</span>
                    <span className="text-xs text-gray-500">{s.strategy_name}</span>
                  </div>
                  <div className="flex items-center gap-4 text-xs text-gray-400">
                    <span>Entry <span className="text-white">${s.entry_price?.toFixed(2)}</span></span>
                    {s.stop_loss && <span>SL <span className="text-red-400">${s.stop_loss?.toFixed(2)}</span></span>}
                    {s.take_profit && <span>TP <span className="text-green-400">${s.take_profit?.toFixed(2)}</span></span>}
                    <span>Confidence <span className="text-white">{((s.confidence || 0) * 100).toFixed(0)}%</span></span>
                    <span className="text-yellow-600">{s.broker}</span>
                  </div>
                </div>
                <div className="flex items-center gap-2">
                  <button
                    disabled={actioning === s.id}
                    onClick={() => handleApprove(s.id)}
                    className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-green-900/20 text-green-400 hover:bg-green-900/40 text-xs font-medium transition-all disabled:opacity-50"
                  >
                    <CheckCircle size={13} /> Approve
                  </button>
                  <button
                    disabled={actioning === s.id}
                    onClick={() => handleReject(s.id)}
                    className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-dark-700 text-gray-400 hover:text-red-400 hover:bg-red-900/20 text-xs transition-all disabled:opacity-50"
                  >
                    <XCircle size={13} /> Dismiss
                  </button>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Open Positions — all brokers, paper + live */}
      <div className="bg-dark-800 border border-dark-600 rounded-xl overflow-hidden">
        <div className="px-4 py-3 border-b border-dark-600 flex items-center justify-between">
          <div className="flex items-center gap-2">
            <TrendingUp size={14} className="text-brand-400" />
            <span className="text-sm font-semibold text-gray-300">Open Positions</span>
            {openPositions.length > 0 && (
              <span className="text-xs bg-brand-500/20 text-brand-400 px-1.5 py-0.5 rounded-full font-medium">
                {openPositions.length}
              </span>
            )}
          </div>
          {openPositions.filter(p => !p.is_paper).length > 0 && (
            <span className="text-xs text-red-400 bg-red-900/20 px-2 py-0.5 rounded border border-red-900/40">
              {openPositions.filter(p => !p.is_paper).length} LIVE
            </span>
          )}
        </div>
        {openPositions.length === 0 ? (
          <div className="px-4 py-8 text-center">
            <p className="text-gray-600 text-sm">No open positions across any broker</p>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-gray-500 border-b border-dark-600">
                  {['Symbol', 'Side', 'Qty', 'Entry', 'Stop Loss', 'Take Profit', 'Unrealized P&L', 'Broker', 'Strategy', 'Mode', 'Opened', ''].map(h => (
                    <th key={h} className="text-left px-3 py-2 font-medium">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {openPositions.map(pos => (
                  <tr key={pos.id} className="border-b border-dark-700 hover:bg-dark-750 transition-colors">
                    <td className="px-3 py-2 font-medium text-white">{pos.symbol}</td>
                    <td className={`px-3 py-2 font-bold ${
                      pos.side === 'buy' || pos.side === 'long' || pos.side === 'cover' ? 'text-green-400' : 'text-red-400'
                    }`}>
                      {pos.side === 'long' ? 'BUY' : pos.side === 'short' ? 'SELL' : pos.side.toUpperCase()}
                    </td>
                    <td className="px-3 py-2 text-gray-300">{pos.quantity.toFixed(4)}</td>
                    <td className="px-3 py-2 text-gray-300">{fmtPrice(pos.entry_price)}</td>
                    <td className="px-3 py-2 text-red-400">{fmtPrice(pos.stop_loss)}</td>
                    <td className="px-3 py-2 text-green-400">{fmtPrice(pos.take_profit)}</td>
                    <td className={`px-3 py-2 font-medium ${
                      (pos.pnl ?? 0) > 0 ? 'text-green-400' : (pos.pnl ?? 0) < 0 ? 'text-red-400' : 'text-gray-500'
                    }`}>
                      {pos.pnl != null ? formatPnl(pos.pnl) : '—'}
                      {pos.pnl_pct != null && (
                        <span className="text-gray-500 ml-1">({pos.pnl_pct > 0 ? '+' : ''}{pos.pnl_pct.toFixed(2)}%)</span>
                      )}
                    </td>
                    <td className="px-3 py-2 text-gray-400 capitalize">{pos.broker}</td>
                    <td className="px-3 py-2 text-gray-400 max-w-[90px] truncate">{pos.strategy_name ?? '—'}</td>
                    <td className="px-3 py-2">
                      <span className={`px-1.5 py-0.5 rounded text-xs ${
                        pos.is_paper ? 'bg-yellow-900/20 text-yellow-400' : 'bg-red-900/20 text-red-300 font-bold'
                      }`}>
                        {pos.is_paper ? 'paper' : 'LIVE'}
                      </span>
                    </td>
                    <td className="px-3 py-2 text-gray-500 whitespace-nowrap">
                      {pos.opened_at ? parseUtc(pos.opened_at)?.toLocaleTimeString() : '—'}
                    </td>
                    <td className="px-3 py-2">
                      <div className="flex items-center gap-1.5">
                        <Link
                          to={`/chart?broker=${encodeURIComponent(pos.broker)}&symbol=${encodeURIComponent(pos.symbol)}${pos.strategy_name ? `&strategy=${encodeURIComponent(pos.strategy_name)}` : ''}&is_paper=${pos.is_paper}`}
                          className="flex items-center gap-1 px-2 py-1 rounded-lg bg-brand-500/10 text-brand-400 hover:bg-brand-500/20 text-xs font-medium transition-all"
                          title="View chart for this position"
                        >
                          <BarChart2 size={11} /> Chart
                        </Link>
                        <button
                          onClick={() => closePosition(pos.id)}
                          disabled={closingId === pos.id}
                          title="Force close this position at market price"
                          className="flex items-center gap-1 px-2.5 py-1 rounded-lg bg-red-500/10 text-red-400 hover:bg-red-500/20 text-xs font-medium transition-all disabled:opacity-40 disabled:cursor-not-allowed"
                        >
                          {closingId === pos.id
                            ? <><Loader2 size={11} className="animate-spin" /> Closing…</>
                            : <><X size={11} /> Force Close</>
                          }
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Recent Trades */}
      <div className="bg-dark-800 border border-dark-600 rounded-xl overflow-hidden">
        <div className="px-4 py-3 border-b border-dark-600 flex items-center gap-2">
          <History size={14} className="text-gray-400" />
          <span className="text-sm font-semibold text-gray-300">Recent Trades</span>
          {trades.length > 0 && (
            <span className="text-xs bg-dark-600 text-gray-400 px-1.5 py-0.5 rounded-full font-medium">
              {trades.length}
            </span>
          )}
          <Link to="/forward-test" className="ml-auto text-xs text-brand-400 hover:text-brand-300 transition-colors">View all →</Link>
        </div>
        {trades.length === 0 ? (
          <div className="px-4 py-8 text-center">
            <p className="text-gray-600 text-sm">No trades yet</p>
          </div>
        ) : (() => {
          const totalPages = Math.ceil(trades.length / TRADES_PER_PAGE)
          const pageTrades = trades.slice((tradePage - 1) * TRADES_PER_PAGE, tradePage * TRADES_PER_PAGE)
          return (
            <>
              <div className="overflow-x-auto overflow-y-auto max-h-[340px]">
                <table className="w-full text-xs">
                  <thead className="sticky top-0 bg-dark-800 z-10">
                    <tr className="text-gray-500 border-b border-dark-600">
                      {['Symbol', 'Side', 'Entry', 'Exit', 'SL', 'TP', 'P&L', 'Status', 'Broker', 'Strategy', 'Opened'].map(h => (
                        <th key={h} className="text-left px-3 py-2 font-medium">{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {pageTrades.map(t => (
                      <tr key={t.id} className="border-b border-dark-700 hover:bg-dark-750 transition-colors">
                        <td className="px-3 py-2 font-medium text-white">{t.symbol}</td>
                        <td className={`px-3 py-2 font-bold ${
                          t.side === 'buy' || t.side === 'long' || t.side === 'cover' ? 'text-green-400' : 'text-red-400'
                        }`}>{t.side === 'long' ? 'BUY' : t.side === 'short' ? 'SELL' : t.side.toUpperCase()}</td>
                        <td className="px-3 py-2 text-gray-300">{fmtPrice(t.entry_price)}</td>
                        <td className="px-3 py-2 text-gray-300">{fmtPrice(t.exit_price)}</td>
                        <td className="px-3 py-2 text-red-400">{fmtPrice(t.stop_loss)}</td>
                        <td className="px-3 py-2 text-green-400">{fmtPrice(t.take_profit)}</td>
                        <td className={`px-3 py-2 font-medium ${
                          (t.pnl ?? 0) > 0 ? 'text-green-400' : (t.pnl ?? 0) < 0 ? 'text-red-400' : 'text-gray-500'
                        }`}>
                          {t.pnl != null ? formatPnl(t.pnl) : '—'}
                          {t.pnl_pct != null && (
                            <span className="text-gray-500 ml-1">({t.pnl_pct > 0 ? '+' : ''}{t.pnl_pct.toFixed(2)}%)</span>
                          )}
                        </td>
                        <td className="px-3 py-2">
                          <span className={`px-1.5 py-0.5 rounded text-xs font-medium ${
                            t.status === 'open'     ? 'bg-blue-500/15 text-blue-400' :
                            t.status === 'filled'   ? 'bg-green-500/15 text-green-400' :
                            t.status === 'pending'  ? 'bg-yellow-500/15 text-yellow-400' :
                            t.status === 'rejected' ? 'bg-red-500/15 text-red-400' :
                            'bg-gray-700 text-gray-400'
                          }`}>{t.status}</span>
                        </td>
                        <td className="px-3 py-2 text-gray-400 capitalize">{t.broker}</td>
                        <td className="px-3 py-2 text-gray-400 max-w-[90px] truncate">{t.strategy_name ?? '—'}</td>
                        <td className="px-3 py-2 text-gray-500 whitespace-nowrap">
                          {t.opened_at ? parseUtc(t.opened_at)?.toLocaleString() : '—'}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {totalPages > 1 && (
                <div className="px-4 py-2.5 border-t border-dark-600 flex items-center justify-between">
                  <span className="text-xs text-gray-500">
                    {(tradePage - 1) * TRADES_PER_PAGE + 1}–{Math.min(tradePage * TRADES_PER_PAGE, trades.length)} of {trades.length}
                  </span>
                  <div className="flex items-center gap-1">
                    <button
                      onClick={() => setTradePage(p => Math.max(1, p - 1))}
                      disabled={tradePage === 1}
                      className="px-2 py-1 text-xs rounded text-gray-400 hover:text-white hover:bg-dark-600 disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
                    >← Prev</button>
                    {Array.from({ length: totalPages }, (_, i) => i + 1)
                      .filter(p => p === 1 || p === totalPages || Math.abs(p - tradePage) <= 1)
                      .reduce<(number | '...')[]>((acc, p, idx, arr) => {
                        if (idx > 0 && typeof arr[idx - 1] === 'number' && (p as number) - (arr[idx - 1] as number) > 1) acc.push('...')
                        acc.push(p)
                        return acc
                      }, [])
                      .map((p, i) => p === '...' ? (
                        <span key={`ellipsis-${i}`} className="px-1 text-xs text-gray-600">…</span>
                      ) : (
                        <button
                          key={p}
                          onClick={() => setTradePage(p as number)}
                          className={`w-6 h-6 text-xs rounded transition-colors ${
                            tradePage === p ? 'bg-brand-500 text-white' : 'text-gray-400 hover:text-white hover:bg-dark-600'
                          }`}
                        >{p}</button>
                      ))
                    }
                    <button
                      onClick={() => setTradePage(p => Math.min(totalPages, p + 1))}
                      disabled={tradePage === totalPages}
                      className="px-2 py-1 text-xs rounded text-gray-400 hover:text-white hover:bg-dark-600 disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
                    >Next →</button>
                  </div>
                </div>
              )}
            </>
          )
        })()}
      </div>

      {/* Recent Signals */}
      <div>
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-sm font-semibold text-gray-300">Recent Signals</h2>
          {signals.length > 0 && (
            <button
              onClick={async () => {
                try {
                  const res = await axios.post('/api/signals/dismiss-expired')
                  toast.success(res.data.message || 'Expired signals cleared')
                  const updated = await axios.get('/api/signals/')
                  setSignals(updated.data.signals || [])
                } catch {
                  toast.error('Failed to clear expired signals')
                }
              }}
              className="flex items-center gap-1.5 text-xs text-gray-400 hover:text-red-400 transition-colors"
              title="Clear HOLD signals and signals older than 24 h"
            >
              <Trash2 size={13} />
              Clear Expired
            </button>
          )}
        </div>
        {loading ? (
          <SkeletonList rows={3} />
        ) : signals.length === 0 ? (
          <div className="bg-dark-800 border border-dark-600 rounded-xl p-8 text-center">
            <Activity size={32} className="text-gray-600 mx-auto mb-3" />
            <p className="text-gray-500 text-sm">No signals yet. Start a strategy to see signals here.</p>
          </div>
        ) : (
          <div className="space-y-3">
            {signals.map(s => <SignalCard key={s.id} signal={s} />)}
          </div>
        )}
      </div>
    </div>
  )
}
