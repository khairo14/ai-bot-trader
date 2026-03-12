import { useEffect, useState, useCallback, useRef } from 'react'
import { Link } from 'react-router-dom'
import { Play, StopCircle, Activity, RefreshCw, Zap, Download, Clock, Loader2, X, TrendingUp, BarChart2, ChevronLeft, ChevronRight } from 'lucide-react'
import { useWebSocket } from '../hooks/useWebSocket'
import toast from 'react-hot-toast'
import { SkeletonLine } from '../components/Skeleton'
import axios from 'axios'
import MarketClock from '../components/MarketClock'
import { parseUtc } from '../lib/dates'

const API = ''   // relative — proxied by Vite to http://localhost:8000
const TRADES_PAGE_SIZE = 10   // rows per page in the trade history table
const WS_URL = (() => {
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${proto}//${window.location.host}/ws`
})()

interface BrokerBreakdown {
  broker: string
  connected: boolean
  is_paper: boolean
  total: number
  available: number
  currency: string
  pnl: number
  open_positions: number
  strategies: string[]
  is_active: boolean
}

interface ForwardStatus {
  active_strategies: number
  strategy_names: string[]
  brokers: string[]
  broker_breakdown: BrokerBreakdown[]
  paper_balance: number
  initial_capital: number
  open_positions: number
  realized_pnl: number
  unrealized_pnl: number
  total_pnl: number
  total_closed_trades: number
  days_running: number
  is_running: boolean
  // Execution state
  is_executing: boolean
  executing_strategy: string | null
  executing_trigger: 'manual' | 'scheduler' | null
  execution_started_at: string | null
  // Scheduler
  next_scheduled: { strategy: string; timeframe: string; next_fire: string } | null
  schedule_details: { strategy: string; timeframe: string; next_fire: string }[]
}

interface PaperTrade {
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
  execution_mode: string
  broker: string
  is_paper: boolean
  strategy_name: string | null
  opened_at: string | null
  closed_at: string | null
}

interface WsMessage {
  type: string
  data: Record<string, unknown>
}

interface PendingSignal {
  id: number
  symbol: string
  signal: string
  entry_price: number
  stop_loss: number | null
  take_profit: number | null
  confidence: number | null
  timeframe: string
  strategy_name: string
  regime: string | null
  execution_mode: string
  is_paper: boolean
  reasons: string[]
  created_at: string | null
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

interface RecentSignal {
  id: number
  symbol: string
  signal: string
  entry_price: number | null
  confidence: number | null
  timeframe: string
  strategy_name: string | null
  execution_mode: string
  reasons: string[]
  acted_on: boolean
  created_at: string | null
}

const fmtUSD = (n: number) =>
  n >= 0 ? `$${n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}` : `-$${Math.abs(n).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`

const fmtPct = (n: number | null) =>
  n == null ? '—' : `${n >= 0 ? '+' : ''}${n.toFixed(2)}%`

/** Adaptive decimal formatter — shows enough places for the instrument scale. */
const fmtPrice = (v?: number | null): string => {
  if (v == null || v === 0) return '—'
  const abs = Math.abs(v)
  if (abs >= 1000) return v.toFixed(2)
  if (abs >= 10)   return v.toFixed(3)
  if (abs >= 0.1)  return v.toFixed(4)
  return v.toFixed(5)
}

export default function ForwardTest() {
  const [status, setStatus] = useState<ForwardStatus | null>(null)
  const [trades, setTrades] = useState<PaperTrade[]>([])
  const [tradeMode, setTradeMode] = useState<'paper' | 'live' | 'all'>('paper')
  const [tradeStatus, setTradeStatus] = useState<'all' | 'open' | 'filled' | 'closed'>('all')
  const [tradePage, setTradePage] = useState(0)
  const [pendingSignals, setPendingSignals] = useState<PendingSignal[]>([])
  const [executingSignal, setExecutingSignal] = useState<number | null>(null)
  const [loading, setLoading] = useState(true)
  const [runLoading, setRunLoading] = useState(false)
  const [stopLoading, setStopLoading] = useState(false)
  const [wsEvents, setWsEvents] = useState<WsMessage[]>([])
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null)
  const [openPositions, setOpenPositions] = useState<OpenPosition[]>([])
  const [recentSignals, setRecentSignals] = useState<RecentSignal[]>([])
  const [closingId, setClosingId] = useState<number | null>(null)
  const [feedTab, setFeedTab] = useState<'live' | 'signals'>('live')

  // After triggering a run, poll every 2 s for up to 60 s so the UI reflects
  // is_executing quickly without waiting for the 30-second background poll.
  const aggressivePollRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const runTimeoutRef    = useRef<ReturnType<typeof setTimeout>  | null>(null)

  const stopAggressivePoll = useCallback(() => {
    if (aggressivePollRef.current) { clearInterval(aggressivePollRef.current); aggressivePollRef.current = null }
    if (runTimeoutRef.current)    { clearTimeout(runTimeoutRef.current);     runTimeoutRef.current    = null }
  }, [])

  const startAggressivePoll = useCallback((fetchFn: () => void) => {
    stopAggressivePoll()
    let ticks = 0
    aggressivePollRef.current = setInterval(() => {
      fetchFn()
      ticks++
      if (ticks >= 30) stopAggressivePoll()  // stop after 60 s
    }, 2000)
    // Hard fallback — clear run loading after 90 s no matter what
    runTimeoutRef.current = setTimeout(() => {
      setRunLoading(false)
      stopAggressivePoll()
    }, 90_000)
  }, [stopAggressivePoll])

  /** Fetch unrealized P&L for open positions.
   *  Priority: 1) broker live-pnl endpoint (real-time, works outside market hours)
   *             2) latest 1m candle close (fallback for brokers not covered above) */
  const enrichPositionsWithLivePnl = useCallback(async (positions: OpenPosition[]): Promise<OpenPosition[]> => {
    if (positions.length === 0) return positions

    // Build a map: "SYMBOL|broker" → { unrealized_pnl, entry_price, current_price }
    const brokerPnlMap: Record<string, { unrealized_pnl: number; current_price: number }> = {}
    try {
      const res = await axios.get(`${API}/api/positions/live-pnl`)
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

    // For any position not covered by broker live-pnl, try the candles fallback
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
            const res = await axios.get(`${API}/api/charts/candles`, {
              params: { symbol, broker, timeframe: '1m', since: now - 10 * 60_000, until: now }
            })
            const candles: { close: number }[] = res.data.candles ?? []
            if (candles.length > 0) priceMap[key] = candles[candles.length - 1].close
          } catch { /* silently skip */ }
        })
      )
    }

    return positions.map(p => {
      if (p.pnl != null) return p   // already has a realised PnL — leave untouched

      // 1. Prefer broker live-pnl
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

      // 2. Fall back to candle-derived price
      const currentPrice = priceMap[`${p.symbol}|${p.broker}`]
      if (!currentPrice || !p.entry_price) return p
      const isBuy = p.side === 'buy' || p.side === 'long'
      const sign  = isBuy ? 1 : -1
      const pnl     = sign * (currentPrice - p.entry_price) * p.quantity
      const pnl_pct = sign * (currentPrice / p.entry_price - 1) * 100
      return { ...p, pnl: parseFloat(pnl.toFixed(4)), pnl_pct: parseFloat(pnl_pct.toFixed(4)) }
    })
  }, [])

  const fetchAll = useCallback(async (mode?: 'paper' | 'live' | 'all') => {
    try {
      const effectiveMode = mode ?? tradeMode
      const [statusRes, tradesRes, pendingRes, positionsRes, signalsRes] = await Promise.allSettled([
        axios.get(`${API}/api/forward-test/status`),
        axios.get(`${API}/api/forward-test/trades?limit=200&mode=${effectiveMode}`),
        axios.get(`${API}/api/forward-test/pending-signals`),
        axios.get(`${API}/api/positions/open`),
        axios.get(`${API}/api/signals?limit=25`),
      ])
      if (statusRes.status === 'fulfilled') setStatus(statusRes.value.data)
      else console.error('ForwardTest status error:', statusRes.reason)
      if (tradesRes.status === 'fulfilled') setTrades(tradesRes.value.data.trades ?? [])
      else console.error('ForwardTest trades error:', tradesRes.reason)
      if (pendingRes.status === 'fulfilled') setPendingSignals(pendingRes.value.data.pending_signals ?? [])
      else console.error('ForwardTest pending-signals error:', pendingRes.reason)
      if (positionsRes.status === 'fulfilled') {
        const enriched = await enrichPositionsWithLivePnl(positionsRes.value.data.positions ?? [])
        setOpenPositions(enriched)
      } else console.error('ForwardTest positions error:', positionsRes.reason)
      if (signalsRes.status === 'fulfilled') setRecentSignals(signalsRes.value.data.signals ?? [])
      else console.error('ForwardTest signals error:', signalsRes.reason)
      setLastUpdated(new Date())
    } catch (e) {
      console.error('ForwardTest fetch error:', e)
    } finally {
      setLoading(false)
    }
  }, [tradeMode, enrichPositionsWithLivePnl])

  // Real-time WebSocket updates — debounced to avoid back-to-back duplicate fetches
  const _wsFetchDebounce = useRef<ReturnType<typeof setTimeout> | null>(null)
  useWebSocket(WS_URL, {
    onMessage: (raw) => {
      const msg = raw as WsMessage
      if (['signal', 'trade', 'emergency_stop', 'run_started', 'run_finished'].includes(msg.type)) {
        setWsEvents((prev) => [msg, ...prev].slice(0, 20))
        // M-4 FIX: debounce WS-triggered fetches — cancel the pending refresh and
        // schedule a new one 500 ms out, so a burst of WS events causes only one fetch.
        if (_wsFetchDebounce.current) clearTimeout(_wsFetchDebounce.current)
        _wsFetchDebounce.current = setTimeout(() => { fetchAll() }, 500)
        if (msg.type === 'run_finished') {
          toast.success('Signal run complete.')
          setRunLoading(false)
          stopAggressivePoll()
          fetchAll()
        }
      }
    },
  })

  useEffect(() => {
    fetchAll()
    const t = setInterval(fetchAll, 30_000) // poll every 30s as backup
    return () => { clearInterval(t); stopAggressivePoll() }
  }, [fetchAll, stopAggressivePoll])

  const triggerRun = async () => {
    setRunLoading(true)
    try {
      const res = await axios.post(`${API}/api/forward-test/run`)
      const data = res.data
      toast.success(`Run triggered for ${data.strategies ?? 1} strategy — signals processing…`)
      // Keep runLoading=true; it will be cleared by run_finished WS event or 90s timeout
      startAggressivePoll(fetchAll)
      fetchAll()  // immediate poll
    } catch (e: any) {
      const status = e?.response?.status
      const detail = e?.response?.data?.detail
      if (status === 409) {
        toast.error(detail ?? 'A run is already in progress.')
      } else {
        toast.error(detail ?? 'Run failed')
      }
      setRunLoading(false)
    }
  }

  const emergencyStop = () => {
    toast.custom((t) => (
      <div className="flex flex-col gap-3 p-4 bg-dark-800 border border-red-900/50 rounded-xl text-sm shadow-xl">
        <p className="font-semibold text-red-400">Emergency Stop</p>
        <p className="text-gray-300 text-xs leading-relaxed">Close ALL open paper positions and deactivate all paper strategies?</p>
        <div className="flex gap-2">
          <button
            className="flex-1 py-1.5 rounded-lg bg-red-600 hover:bg-red-500 text-white text-xs font-medium transition-all"
            onClick={async () => {
              toast.dismiss(t.id)
              setStopLoading(true)
              try {
                const res = await axios.post(`${API}/api/forward-test/emergency-stop`)
                toast.success(res.data.message ?? 'Emergency stop executed.')
                await fetchAll()
              } catch (e: any) {
                toast.error(e?.response?.data?.detail ?? 'Emergency stop failed.')
              } finally { setStopLoading(false) }
            }}
          >Yes, stop all</button>
          <button
            className="flex-1 py-1.5 rounded-lg bg-dark-700 hover:bg-dark-600 text-gray-300 text-xs transition-all border border-dark-500"
            onClick={() => toast.dismiss(t.id)}
          >Cancel</button>
        </div>
      </div>
    ), { duration: Infinity })
  }

  const executeSignal = async (sig: PendingSignal) => {
    setExecutingSignal(sig.id)
    try {
      const res = await axios.post(`${API}/api/forward-test/execute-signal/${sig.id}`)
      toast.success(`${res.data.side.toUpperCase()} ${res.data.symbol} — trade placed${sig.is_paper ? ' (paper)' : ' (LIVE)'}!`)
      await fetchAll()
    } catch (e: any) {
      toast.error(e?.response?.data?.detail ?? 'Execution failed')
    } finally {
      setExecutingSignal(null)
    }
  }

  const closePosition = async (id: number) => {
    setClosingId(id)
    try {
      await axios.post(`${API}/api/positions/close`, { trade_id: id, reason: 'manual_override' })
      toast.success('Position closed at market price.')
      await fetchAll()
    } catch (e: any) {
      toast.error(e?.response?.data?.detail ?? 'Close failed')
    } finally {
      setClosingId(null)
    }
  }

  const isRunning = status?.is_running ?? false
  const isExecuting = status?.is_executing ?? false
  const totalPnl = status?.total_pnl ?? 0

  /** How many minutes until the next scheduled fire (rounded). */
  const nextRunLabel = (() => {
    if (!status?.next_scheduled) return null
    const diff = new Date(status.next_scheduled.next_fire).getTime() - Date.now()
    if (diff <= 0) return 'any moment'
    const mins = Math.ceil(diff / 60_000)
    return `${status.next_scheduled.timeframe} · ${mins < 60 ? `${mins}m` : `${Math.round(mins / 60)}h`} away`
  })()

  return (
    <div className="p-6 space-y-6">
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <h1 className="text-xl font-bold text-white">Forward Testing</h1>
          <p className="text-sm text-gray-500 mt-0.5">Paper trading on live market data. Validates strategy before real execution.</p>
        </div>
        <div className="flex items-center gap-2">
          <MarketClock />
          <div className="flex items-center gap-2 text-xs text-gray-600">
            {lastUpdated && <span>Updated {lastUpdated.toLocaleTimeString()}</span>}
            <button onClick={() => fetchAll()} className="p-1.5 rounded-lg hover:bg-dark-700 text-gray-500 hover:text-gray-300 transition-all">
              <RefreshCw size={14} />
            </button>
          </div>
        </div>
      </div>

      {/* Status Banner */}
      <div className="bg-dark-800 border border-dark-600 rounded-xl p-4 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div className={`w-2 h-2 rounded-full ${
            isExecuting || runLoading ? 'bg-yellow-400 animate-pulse' :
            isRunning ? 'bg-green-500 animate-pulse' : 'bg-gray-600'
          }`} />
          <div>
            <p className="text-sm font-medium text-white">
              {isRunning
                ? `Forward Test: Active (${status!.active_strategies} strateg${status!.active_strategies === 1 ? 'y' : 'ies'})`
                : 'Forward Test: Inactive'}
            </p>
            <p className="text-xs text-gray-500">
              {isRunning
                ? status!.strategy_names.join(', ')
                : 'No active strategies in paper mode'}
            </p>
          </div>
        </div>

        <div className="flex flex-col items-end gap-1.5">
          {/* Currently executing indicator */}
          {(isExecuting || runLoading) && (
            <div className="flex items-center gap-1.5 text-xs text-yellow-400">
              <Loader2 size={11} className="animate-spin" />
              <span>
                {isExecuting
                  ? (<>Running: <span className="font-medium">{status!.executing_strategy ?? '…'}</span>
                    {status!.executing_trigger && <span className="text-gray-500 ml-1">({status!.executing_trigger})</span>}</>)
                  : 'Starting signal run…'
                }
              </span>
            </div>
          )}
          {/* Next scheduled */}
          {isRunning && nextRunLabel && !isExecuting && !runLoading && (
            <div className="flex items-center gap-1 text-xs text-gray-500">
              <Clock size={10} />
              <span>Next: {nextRunLabel}</span>
            </div>
          )}

          <div className="flex gap-2">
            <button
              onClick={triggerRun}
              disabled={runLoading || isExecuting || !isRunning}
              className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-brand-500/10 text-brand-500 hover:bg-brand-500/20 text-sm font-medium transition-all disabled:opacity-40 disabled:cursor-not-allowed"
            >
              {runLoading || isExecuting ? <RefreshCw size={14} className="animate-spin" /> : <Play size={14} />}
              {isExecuting ? 'Running…' : runLoading ? 'Starting…' : 'Run Now'}
            </button>
            <button
              onClick={emergencyStop}
              disabled={stopLoading || !isRunning}
              className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-dark-700 text-gray-400 hover:text-red-400 hover:bg-red-500/10 text-sm transition-all disabled:opacity-40 disabled:cursor-not-allowed"
            >
              {stopLoading ? <RefreshCw size={14} className="animate-spin" /> : <StopCircle size={14} />}
              Emergency Stop
            </button>
          </div>
        </div>
      </div>

      {/* Broker Balance Cards — always show all 3, highlight active ones */}
      <div className="grid grid-cols-3 gap-4">
        {(status?.broker_breakdown ?? [
          { broker: 'binance', connected: false, is_paper: true, total: 0, available: 0, currency: 'USDT', pnl: 0, open_positions: 0, strategies: [], is_active: false },
          { broker: 'alpaca',  connected: false, is_paper: true, total: 0, available: 0, currency: 'USD',  pnl: 0, open_positions: 0, strategies: [], is_active: false },
          { broker: 'ibkr',    connected: false, is_paper: true, total: 0, available: 0, currency: 'USD',  pnl: 0, open_positions: 0, strategies: [], is_active: false },
        ] as BrokerBreakdown[]).map((b) => (
          <div key={b.broker} className={`bg-dark-800 border rounded-xl p-4 transition-opacity ${
            b.is_active ? 'border-brand-500/40' : b.connected ? 'border-dark-600' : 'border-dark-700 opacity-50'
          }`}>
            <div className="flex items-center justify-between mb-2">
              <p className="text-xs text-gray-500 uppercase tracking-wider">{b.broker}</p>
              <div className="flex items-center gap-1.5">
                {b.is_paper && <span className="text-xs text-yellow-500 bg-yellow-900/20 px-1.5 py-0.5 rounded">paper</span>}
                {b.is_active
                  ? <span className="w-2 h-2 rounded-full bg-brand-500 animate-pulse" title="Active in forward test" />
                  : b.connected
                    ? <span className="w-2 h-2 rounded-full bg-gray-600" title="Connected, not in use" />
                    : <span className="w-2 h-2 rounded-full bg-dark-500" title="Offline" />
                }
              </div>
            </div>
            <p className={`text-xl font-bold ${b.connected ? 'text-white' : 'text-gray-600'}`}>
              {loading ? <SkeletonLine className="h-6 w-28 mt-1" /> : b.connected ? fmtUSD(b.total) : '—'}
            </p>
            {b.connected && (
              <p className="text-xs text-gray-600 mt-0.5">
                {fmtUSD(b.available)} available
                {b.pnl !== 0 && (
                  <span className={`ml-1.5 ${b.pnl > 0 ? 'text-green-400' : 'text-red-400'}`}>
                    ({b.pnl > 0 ? '+' : ''}{fmtUSD(b.pnl)} P&L)
                  </span>
                )}
              </p>
            )}
            {b.is_active && b.strategies.length > 0 && (
              <div className="mt-2 flex flex-wrap gap-1">
                {b.strategies.map(s => (
                  <span key={s} className="text-xs bg-brand-500/10 text-brand-400 px-1.5 py-0.5 rounded truncate max-w-full">{s}</span>
                ))}
              </div>
            )}
            {!b.is_active && b.connected && (
              <p className="text-xs text-gray-700 mt-1.5">No active strategies</p>
            )}
            {!b.connected && (
              <p className="text-xs text-gray-700 mt-1.5">Offline — not configured</p>
            )}
          </div>
        ))}
      </div>

      {/* Stats Cards */}
      <div className="grid grid-cols-3 gap-4">
        {[
          {
            label: 'Open Positions',
            value: status?.open_positions ?? '—',
            sub: status ? `${status.active_strategies} active strateg${status.active_strategies === 1 ? 'y' : 'ies'}` : '',
          },
          {
            label: 'Total Paper P&L',
            value: status ? fmtUSD(status.total_pnl) : '—',
            sub: status ? `${fmtPct(status.total_pnl / (status.initial_capital || 1) * 100)} return` : '',
            color: totalPnl > 0 ? 'text-green-400' : totalPnl < 0 ? 'text-red-400' : 'text-white',
          },
          {
            label: 'Days Running',
            value: status?.days_running ?? '—',
            sub: status ? `${status.total_closed_trades} closed trade${status.total_closed_trades === 1 ? '' : 's'}` : '',
          },
        ].map(({ label, value, sub, color }) => (
          <div key={label} className="bg-dark-800 border border-dark-600 rounded-xl p-4">
            <p className="text-xs text-gray-500 uppercase tracking-wider mb-1">{label}</p>
            <p className={`text-xl font-bold ${color ?? 'text-white'}`}>{loading ? <SkeletonLine className="h-6 w-24 mt-1" /> : value}</p>
            {sub && <p className="text-xs text-gray-600 mt-0.5">{sub}</p>}
          </div>
        ))}
      </div>

      {/* Open Positions — live view with per-position force-close */}
      <div className="bg-dark-800 border border-dark-600 rounded-xl overflow-hidden">
        <div className="px-4 py-3 border-b border-dark-600 flex items-center justify-between">
          <div className="flex items-center gap-2">
            <TrendingUp size={14} className="text-brand-400" />
            <span className="text-sm font-medium text-white">Open Positions</span>
            {openPositions.length > 0 && (
              <span className="text-xs bg-brand-500/20 text-brand-400 px-1.5 py-0.5 rounded-full font-medium">
                {openPositions.length}
              </span>
            )}
          </div>
          {openPositions.length > 1 && (
            <button
              onClick={emergencyStop}
              title="Closes all paper positions only. Live positions must be closed individually."
              className="flex items-center gap-1 px-2.5 py-1 rounded-lg bg-red-500/10 text-red-400 hover:bg-red-500/20 text-xs font-medium transition-all"
            >
              <X size={12} /> Close All Paper
            </button>
          )}
        </div>
        {openPositions.length === 0 ? (
          <div className="px-4 py-8 text-center">
            <p className="text-gray-600 text-sm">No open positions</p>
          </div>
        ) : (
          <div className="overflow-hidden">
            <table className="w-full text-xs table-fixed">
              <thead>
                <tr className="text-gray-500 border-b border-dark-600">
                  <th className="text-left px-3 py-2 font-medium w-10">#</th>
                  <th className="text-left px-3 py-2 font-medium w-20">Symbol</th>
                  <th className="text-left px-3 py-2 font-medium w-14">Side</th>
                  <th className="text-left px-3 py-2 font-medium w-24">Entry</th>
                  <th className="text-left px-3 py-2 font-medium w-20">Stop Loss</th>
                  <th className="text-left px-3 py-2 font-medium w-20">Take Profit</th>
                  <th className="text-left px-3 py-2 font-medium w-32">Unrealized P&L</th>
                  <th className="text-left px-3 py-2 font-medium w-16">Broker</th>
                  <th className="text-left px-3 py-2 font-medium w-14">Mode</th>
                  <th className="text-left px-3 py-2 font-medium w-36">Strategy</th>
                  <th className="text-left px-3 py-2 font-medium w-24">Opened</th>
                  <th className="text-left px-3 py-2 font-medium w-32"></th>
                </tr>
              </thead>
              <tbody>
                {openPositions.map((pos) => (
                  <tr key={pos.id} className="border-b border-dark-700 hover:bg-dark-750 transition-colors">
                    <td className="px-3 py-2 text-gray-600 font-mono">#{pos.id}</td>
                    <td className="px-3 py-2 font-medium text-white truncate">{pos.symbol}</td>
                    <td className={`px-3 py-2 font-bold ${(pos.side === 'buy' || pos.side === 'long' || pos.side === 'cover') ? 'text-green-400' : 'text-red-400'}`}>
                      {pos.side === 'long' ? 'BUY' : pos.side === 'short' ? 'SELL' : pos.side.toUpperCase()}
                    </td>
                    <td className="px-3 py-2 text-gray-300">{fmtPrice(pos.entry_price)}</td>
                    <td className="px-3 py-2 text-red-400">{fmtPrice(pos.stop_loss)}</td>
                    <td className="px-3 py-2 text-green-400">{fmtPrice(pos.take_profit)}</td>
                    <td className={`px-3 py-2 font-medium ${(pos.pnl ?? 0) > 0 ? 'text-green-400' : (pos.pnl ?? 0) < 0 ? 'text-red-400' : 'text-gray-500'}`}>
                      {pos.pnl != null ? `${pos.pnl > 0 ? '+' : ''}${fmtUSD(pos.pnl)}` : '—'}
                      {pos.pnl_pct != null && <span className="text-gray-500 ml-1">({fmtPct(pos.pnl_pct)})</span>}
                    </td>
                    <td className="px-3 py-2 text-gray-400 capitalize truncate">{pos.broker}</td>
                    <td className="px-3 py-2">
                      <span className={`text-xs px-1.5 py-0.5 rounded font-medium ${
                        pos.is_paper ? 'bg-blue-900/20 text-blue-400' : 'bg-red-900/30 text-red-400'
                      }`}>{pos.is_paper ? 'paper' : 'LIVE'}</span>
                    </td>
                    <td className="px-3 py-2 text-gray-400 truncate">{pos.strategy_name ?? '—'}</td>
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
                          {closingId === pos.id ? <Loader2 size={11} className="animate-spin" /> : <X size={11} />}
                          {closingId === pos.id ? 'Closing…' : 'Force Close'}
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

      {/* Pending Signals — suggestion / semi-auto modes */}
      {pendingSignals.length > 0 && (
        <div className="bg-dark-800 border border-yellow-900/40 rounded-xl overflow-hidden">
          <div className="px-4 py-3 border-b border-dark-600 flex items-center gap-2">
            <div className="w-1.5 h-1.5 rounded-full bg-yellow-400 animate-pulse" />
            <span className="text-sm font-medium text-white">Pending Actions</span>
            <span className="text-xs text-gray-500 ml-1">{pendingSignals.length} signal{pendingSignals.length !== 1 ? 's' : ''} awaiting your decision</span>
          </div>
          <div className="divide-y divide-dark-700">
            {pendingSignals.map((sig) => (
              <div key={sig.id} className="px-4 py-3 flex items-center justify-between gap-4">
                <div className="flex items-center gap-3 min-w-0">
                  <span className={`text-xs font-bold px-2 py-0.5 rounded ${
                    sig.signal === 'BUY' || sig.signal === 'COVER' ? 'bg-green-500/15 text-green-400' : 'bg-red-500/15 text-red-400'
                  }`}>{sig.signal}</span>
                  <div className="min-w-0">
                    <p className="text-sm font-medium text-white">{sig.symbol}</p>
                    <p className="text-xs text-gray-500 truncate">{sig.strategy_name} · {sig.timeframe}{sig.regime ? ` · ${sig.regime}` : ''}</p>
                  </div>
                </div>
                <div className="flex items-center gap-6 text-xs text-gray-400 shrink-0">
                  <div className="text-right">
                    <p className="text-white font-medium">{fmtPrice(sig.entry_price)}</p>
                    <p className="text-gray-600">entry</p>
                  </div>
                  {sig.stop_loss && (
                    <div className="text-right">
                      <p className="text-red-400">{fmtPrice(sig.stop_loss)}</p>
                      <p className="text-gray-600">stop</p>
                    </div>
                  )}
                  {sig.take_profit && (
                    <div className="text-right">
                      <p className="text-green-400">{fmtPrice(sig.take_profit)}</p>
                      <p className="text-gray-600">target</p>
                    </div>
                  )}
                  {sig.confidence != null && (
                    <div className="text-right">
                      <p className="text-brand-400">{(sig.confidence * 100).toFixed(0)}%</p>
                      <p className="text-gray-600">conf</p>
                    </div>
                  )}
                  <span className={`text-xs px-1.5 py-0.5 rounded border ${
                    sig.is_paper
                      ? 'bg-blue-900/20 text-blue-400 border-blue-900/40'
                      : 'bg-yellow-900/20 text-yellow-400 border-yellow-900/40'
                  }`}>{sig.is_paper ? 'Paper' : 'LIVE'}</span>
                  <button
                    onClick={() => executeSignal(sig)}
                    disabled={executingSignal === sig.id}
                    className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-brand-500 hover:bg-brand-400 text-white text-xs font-medium transition-all disabled:opacity-50 disabled:cursor-not-allowed"
                  >
                    {executingSignal === sig.id
                      ? <><Loader2 size={12} className="animate-spin" /> Executing…</>
                      : <><Play size={12} /> {sig.execution_mode === 'semi-auto' || sig.execution_mode === 'SEMI_AUTO' ? 'Confirm' : 'Execute'}</>
                    }
                  </button>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Trades table + WS events */}
      <div className="grid grid-cols-3 gap-4">
        {/* Paper Trades Table */}
        <div className="col-span-2 bg-dark-800 border border-dark-600 rounded-xl overflow-hidden">
          <div className="px-4 py-3 border-b border-dark-600 flex items-center justify-between flex-wrap gap-2">
            <div className="flex items-center gap-2 flex-wrap">
              <span className="text-sm font-medium text-white">Trades</span>
              {/* Paper / Live / All filter */}
              <div className="flex text-xs rounded-lg overflow-hidden border border-dark-500">
                {(['paper', 'live', 'all'] as const).map((m) => (
                  <button
                    key={m}
                    onClick={() => { setTradeMode(m); setTradePage(0) }}
                    className={`px-2.5 py-1 capitalize transition-colors ${
                      tradeMode === m ? 'bg-dark-600 text-white' : 'text-gray-500 hover:text-gray-300'
                    }`}
                  >{m}</button>
                ))}
              </div>
              {/* Status filter */}
              <div className="flex text-xs rounded-lg overflow-hidden border border-dark-500">
                {([['all', 'All'], ['open', 'Open'], ['filled', 'Filled'], ['closed', 'Closed']] as const).map(([val, label]) => (
                  <button
                    key={val}
                    onClick={() => { setTradeStatus(val); setTradePage(0) }}
                    className={`px-2.5 py-1 transition-colors ${
                      tradeStatus === val ? 'bg-dark-600 text-white' : 'text-gray-500 hover:text-gray-300'
                    }`}
                  >{label}</button>
                ))}
              </div>
            </div>
            <div className="flex items-center gap-3">
              <span className="text-xs text-gray-500">{trades.filter(t => tradeStatus === 'all' || t.status === tradeStatus).length} trade{trades.filter(t => tradeStatus === 'all' || t.status === tradeStatus).length !== 1 ? 's' : ''}</span>
              <a
                href="/api/forward-test/trades/export"
                className="flex items-center gap-1.5 px-2.5 py-1 rounded-lg bg-dark-700 text-gray-400 hover:text-green-400 hover:bg-green-500/10 text-xs transition-all"
                title="Download all paper trades as CSV"
              >
                <Download size={12} /> Export CSV
              </a>
              <a
                href="/api/positions/history/export"
                className="flex items-center gap-1.5 px-2.5 py-1 rounded-lg bg-dark-700 text-gray-400 hover:text-blue-400 hover:bg-blue-500/10 text-xs transition-all"
                title="Download all completed live trades as CSV"
              >
                <Download size={12} /> Live History
              </a>
            </div>
          </div>
          {(() => {
            const filteredTrades = tradeStatus === 'all' ? trades : trades.filter(t => t.status === tradeStatus)
            if (filteredTrades.length === 0) return (
              <div className="p-12 text-center">
                <Activity size={36} className="text-gray-600 mx-auto mb-3 opacity-30" />
                <p className="text-gray-500 text-sm">
                  {tradeStatus !== 'all' ? `No ${tradeStatus} trades.` : tradeMode === 'paper' ? 'No paper trades yet.' : tradeMode === 'live' ? 'No live trades recorded.' : 'No trades recorded.'}
                </p>
                {tradeMode === 'paper' && tradeStatus === 'all' && <p className="text-gray-600 text-xs mt-1">Enable a strategy in paper mode and click "Run Now".</p>}
              </div>
            )
            return (() => {
            const totalPages = Math.ceil(filteredTrades.length / TRADES_PAGE_SIZE)
            const page = Math.min(tradePage, totalPages - 1)
            const pageRows = filteredTrades.slice(page * TRADES_PAGE_SIZE, (page + 1) * TRADES_PAGE_SIZE)
            return (
              <>
                <div className="overflow-hidden">
                  <table className="w-full text-xs table-fixed">
                    <thead>
                      <tr className="text-gray-500 border-b border-dark-600">
                        <th className="text-left px-2 py-2 font-medium w-10">#</th>
                        <th className="text-left px-2 py-2 font-medium w-20">Symbol</th>
                        <th className="text-left px-2 py-2 font-medium w-14">Side</th>
                        <th className="text-left px-2 py-2 font-medium w-20">Entry</th>
                        <th className="text-left px-2 py-2 font-medium w-20">Exit</th>
                        <th className="text-left px-2 py-2 font-medium w-20">SL</th>
                        <th className="text-left px-2 py-2 font-medium w-20">TP</th>
                        <th className="text-left px-2 py-2 font-medium w-28">P&L</th>
                        <th className="text-left px-2 py-2 font-medium w-20">Status</th>
                        <th className="text-left px-2 py-2 font-medium w-20">Broker</th>
                        <th className="text-left px-2 py-2 font-medium">Strategy</th>
                        <th className="text-left px-2 py-2 font-medium w-32">Opened</th>
                      </tr>
                    </thead>
                    <tbody>
                      {pageRows.map((t) => (
                        <tr key={t.id} className="border-b border-dark-700 hover:bg-dark-750 transition-colors">
                          <td className="px-2 py-2 text-gray-600 font-mono">#{t.id}</td>
                          <td className="px-2 py-2 font-medium text-white truncate">{t.symbol}</td>
                          <td className={`px-2 py-2 font-medium ${(t.side === 'buy' || t.side === 'long' || t.side === 'cover') ? 'text-green-400' : 'text-red-400'}`}>
                            {t.side === 'long' ? 'BUY' : t.side === 'short' ? 'SELL' : t.side.toUpperCase()}
                          </td>
                          <td className="px-2 py-2 text-gray-300">{fmtPrice(t.entry_price)}</td>
                          <td className="px-2 py-2 text-gray-300">{fmtPrice(t.exit_price)}</td>
                          <td className="px-2 py-2 text-red-400">{fmtPrice(t.stop_loss)}</td>
                          <td className="px-2 py-2 text-green-400">{fmtPrice(t.take_profit)}</td>
                          <td className={`px-2 py-2 font-medium ${(t.pnl ?? 0) > 0 ? 'text-green-400' : (t.pnl ?? 0) < 0 ? 'text-red-400' : 'text-gray-500'}`}>
                            {t.pnl != null ? fmtUSD(t.pnl) : '—'}
                            {t.pnl_pct != null && <span className="text-gray-500 ml-1">({fmtPct(t.pnl_pct)})</span>}
                          </td>
                          <td className="px-2 py-2">
                            <div className="flex flex-col gap-0.5">
                              <span className={`px-1.5 py-0.5 rounded text-xs font-medium ${
                                t.status === 'open'     ? 'bg-blue-500/15 text-blue-400' :
                                t.status === 'filled'   ? 'bg-green-500/15 text-green-400' :
                                t.status === 'pending'  ? 'bg-yellow-500/15 text-yellow-400' :
                                t.status === 'rejected' ? 'bg-red-500/15 text-red-400' :
                                'bg-gray-700 text-gray-400'
                              }`}>
                                {t.status}
                              </span>
                              <span className={`px-1 py-0.5 rounded text-xs ${
                                t.is_paper ? 'bg-blue-900/20 text-blue-400' : 'bg-red-900/30 text-red-400 font-bold'
                              }`}>{t.is_paper ? 'paper' : 'LIVE'}</span>
                            </div>
                          </td>
                          <td className="px-2 py-2 text-gray-400 capitalize">{t.broker}</td>
                          <td className="px-2 py-2 text-gray-400 truncate">{t.strategy_name ?? '—'}</td>
                          <td className="px-2 py-2 text-gray-500 whitespace-nowrap">
                            {t.opened_at ? parseUtc(t.opened_at)?.toLocaleString() : '—'}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                {/* Pagination footer */}
                <div className="px-4 py-2.5 border-t border-dark-600 flex items-center justify-between">
                  <span className="text-xs text-gray-500">
                    {page * TRADES_PAGE_SIZE + 1}–{Math.min((page + 1) * TRADES_PAGE_SIZE, filteredTrades.length)} of {filteredTrades.length} trade{filteredTrades.length !== 1 ? 's' : ''}
                  </span>
                  {totalPages > 1 && (
                    <div className="flex items-center gap-1">
                      <button
                        onClick={() => setTradePage(p => Math.max(0, p - 1))}
                        disabled={page === 0}
                        className="p-1 rounded hover:bg-dark-600 text-gray-400 hover:text-white disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
                      >
                        <ChevronLeft size={14} />
                      </button>
                      {Array.from({ length: totalPages }, (_, i) => (
                        <button
                          key={i}
                          onClick={() => setTradePage(i)}
                          className={`w-6 h-6 rounded text-xs font-medium transition-colors ${
                            i === page ? 'bg-brand-500 text-white' : 'text-gray-500 hover:text-white hover:bg-dark-600'
                          }`}
                        >
                          {i + 1}
                        </button>
                      ))}
                      <button
                        onClick={() => setTradePage(p => Math.min(totalPages - 1, p + 1))}
                        disabled={page === totalPages - 1}
                        className="p-1 rounded hover:bg-dark-600 text-gray-400 hover:text-white disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
                      >
                        <ChevronRight size={14} />
                      </button>
                    </div>
                  )}
                </div>
              </>
            )
          })()
          })()
          }
        </div>

        {/* Activity Feed — Live WS Events + Persistent Signal Log */}
        <div className="bg-dark-800 border border-dark-600 rounded-xl overflow-hidden">
          <div className="px-4 py-3 border-b border-dark-600 flex items-center gap-2">
            <Zap size={13} className="text-yellow-400" />
            <span className="text-sm font-medium text-white">Activity</span>
            <div className="ml-auto flex text-xs rounded-lg overflow-hidden border border-dark-500">
              <button
                onClick={() => setFeedTab('live')}
                className={`px-2.5 py-1 transition-colors ${feedTab === 'live' ? 'bg-dark-600 text-white' : 'text-gray-500 hover:text-gray-300'}`}
              >Live</button>
              <button
                onClick={() => setFeedTab('signals')}
                className={`px-2.5 py-1 transition-colors ${feedTab === 'signals' ? 'bg-dark-600 text-white' : 'text-gray-500 hover:text-gray-300'}`}
              >Signals{recentSignals.length > 0 && <span className="ml-1 text-brand-400">{recentSignals.length}</span>}</button>
            </div>
          </div>
          {feedTab === 'live' ? (
            <div className="p-3 space-y-2 max-h-[300px] overflow-y-auto">
              {wsEvents.length === 0 ? (
                <p className="text-xs text-gray-600 text-center py-4">Waiting for events…</p>
              ) : (
                wsEvents.map((evt, i) => (
                  <div key={i} className="text-xs bg-dark-700 rounded-lg p-2">
                    <div className="flex items-center gap-1.5 mb-0.5">
                      <span className={`font-medium ${
                        evt.type === 'signal' ? 'text-brand-400' :
                        evt.type === 'trade' ? 'text-green-400' :
                        evt.type === 'run_started' ? 'text-yellow-400' :
                        evt.type === 'run_finished' ? 'text-blue-400' :
                        'text-red-400'
                      }`}>{evt.type.toUpperCase().replace('_', ' ')}</span>
                      {evt.type === 'signal' && (
                        <span className="text-white">{String(evt.data.symbol)} → {String(evt.data.signal)}</span>
                      )}
                      {evt.type === 'trade' && (
                        <span className="text-white">{String(evt.data.symbol)} {String(evt.data.side).toUpperCase()}</span>
                      )}
                      {(evt.type === 'run_started' || evt.type === 'run_finished') && (
                        <span className="text-gray-400">{String(evt.data.trigger)} · {String(evt.data.strategies)} strateg{Number(evt.data.strategies) === 1 ? 'y' : 'ies'}</span>
                      )}
                    </div>
                    {evt.type === 'signal' && (
                      <p className="text-gray-500">@ {Number(evt.data.entry_price).toFixed(2)} conf={Number(evt.data.confidence).toFixed(2)}</p>
                    )}
                    {evt.type === 'trade' && (
                      <p className="text-gray-500">qty={Number(evt.data.quantity).toFixed(4)} @ {Number(evt.data.entry_price).toFixed(2)}</p>
                    )}
                  </div>
                ))
              )}
            </div>
          ) : (
            <div className="p-3 space-y-2 max-h-[300px] overflow-y-auto">
              {recentSignals.length === 0 ? (
                <p className="text-xs text-gray-600 text-center py-4">No signals yet</p>
              ) : (
                recentSignals.map((sig) => (
                  <div key={sig.id} className="text-xs bg-dark-700 rounded-lg p-2 space-y-1">
                    <div className="flex items-center gap-1.5 flex-wrap">
                      <span className={`font-bold ${sig.signal === 'BUY' || sig.signal === 'COVER' ? 'text-green-400' : 'text-red-400'}`}>
                        {sig.signal}
                      </span>
                      <span className="text-white font-medium">{sig.symbol}</span>
                      {sig.confidence != null && (
                        <span className="text-gray-500">{(sig.confidence * 100).toFixed(0)}%</span>
                      )}
                      <span className="text-gray-600">·</span>
                      <span className="text-gray-500">{sig.timeframe}</span>
                      <span className={`ml-auto px-1.5 py-0.5 rounded text-xs ${
                        sig.acted_on
                          ? 'bg-green-900/30 text-green-400'
                          : sig.execution_mode === 'suggestion'
                            ? 'bg-gray-700 text-gray-500'
                            : 'bg-yellow-900/20 text-yellow-400'
                      }`}>
                        {sig.acted_on ? 'traded' : sig.execution_mode}
                      </span>
                    </div>
                    <p className="text-gray-600">
                      {sig.strategy_name ?? 'unknown'}{sig.entry_price != null ? ` · @ ${sig.entry_price.toFixed(2)}` : ''}
                    </p>
                    {sig.reasons.length > 0 && (
                      <p className="text-gray-500 leading-relaxed line-clamp-2">
                        {sig.reasons.slice(0, 3).join(' · ')}
                      </p>
                    )}
                    {sig.created_at && (
                      <p className="text-gray-700">{parseUtc(sig.created_at)?.toLocaleTimeString()}</p>
                    )}
                  </div>
                ))
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

