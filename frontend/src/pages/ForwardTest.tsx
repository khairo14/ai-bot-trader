import { useEffect, useState, useCallback, useRef } from 'react'
import { Play, StopCircle, Activity, RefreshCw, Zap, Download, Clock, Loader2 } from 'lucide-react'
import { useWebSocket } from '../hooks/useWebSocket'
import toast from 'react-hot-toast'
import { SkeletonLine } from '../components/Skeleton'
import axios from 'axios'

const API = ''   // relative — proxied by Vite to http://localhost:8000
const WS_URL = (() => {
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${proto}//${window.location.host}/ws`
})()

interface ForwardStatus {
  active_strategies: number
  strategy_names: string[]
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

const fmtUSD = (n: number) =>
  n >= 0 ? `$${n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}` : `-$${Math.abs(n).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`

const fmtPct = (n: number | null) =>
  n == null ? '—' : `${n >= 0 ? '+' : ''}${n.toFixed(2)}%`

export default function ForwardTest() {
  const [status, setStatus] = useState<ForwardStatus | null>(null)
  const [trades, setTrades] = useState<PaperTrade[]>([])
  const [pendingSignals, setPendingSignals] = useState<PendingSignal[]>([])
  const [executingSignal, setExecutingSignal] = useState<number | null>(null)
  const [loading, setLoading] = useState(true)
  const [runLoading, setRunLoading] = useState(false)
  const [stopLoading, setStopLoading] = useState(false)
  const [wsEvents, setWsEvents] = useState<WsMessage[]>([])
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null)

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

  const fetchAll = useCallback(async () => {
    try {
      const [statusRes, tradesRes, pendingRes] = await Promise.allSettled([
        axios.get(`${API}/api/forward-test/status`),
        axios.get(`${API}/api/forward-test/trades?limit=50`),
        axios.get(`${API}/api/forward-test/pending-signals`),
      ])
      if (statusRes.status === 'fulfilled') setStatus(statusRes.value.data)
      else console.error('ForwardTest status error:', statusRes.reason)
      if (tradesRes.status === 'fulfilled') setTrades(tradesRes.value.data.trades ?? [])
      else console.error('ForwardTest trades error:', tradesRes.reason)
      if (pendingRes.status === 'fulfilled') setPendingSignals(pendingRes.value.data.pending_signals ?? [])
      else console.error('ForwardTest pending-signals error:', pendingRes.reason)
      setLastUpdated(new Date())
    } catch (e) {
      console.error('ForwardTest fetch error:', e)
    } finally {
      setLoading(false)
    }
  }, [])

  // Real-time WebSocket updates
  useWebSocket(WS_URL, {
    onMessage: (raw) => {
      const msg = raw as WsMessage
      if (['signal', 'trade', 'emergency_stop', 'run_started', 'run_finished'].includes(msg.type)) {
        setWsEvents((prev) => [msg, ...prev].slice(0, 20))
        fetchAll()
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
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-bold text-white">Forward Testing</h1>
          <p className="text-sm text-gray-500 mt-0.5">Paper trading on live market data. Validates strategy before real execution.</p>
        </div>
        <div className="flex items-center gap-2 text-xs text-gray-600">
          {lastUpdated && <span>Updated {lastUpdated.toLocaleTimeString()}</span>}
          <button onClick={fetchAll} className="p-1.5 rounded-lg hover:bg-dark-700 text-gray-500 hover:text-gray-300 transition-all">
            <RefreshCw size={14} />
          </button>
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

      {/* Stats Cards */}
      <div className="grid grid-cols-4 gap-4">
        {[
          {
            label: 'Paper Balance',
            value: status ? fmtUSD(status.paper_balance) : '—',
            sub: status ? `Started at ${fmtUSD(status.initial_capital)}` : '',
          },
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
                    <p className="text-white font-medium">${sig.entry_price.toFixed(2)}</p>
                    <p className="text-gray-600">entry</p>
                  </div>
                  {sig.stop_loss && (
                    <div className="text-right">
                      <p className="text-red-400">${sig.stop_loss.toFixed(2)}</p>
                      <p className="text-gray-600">stop</p>
                    </div>
                  )}
                  {sig.take_profit && (
                    <div className="text-right">
                      <p className="text-green-400">${sig.take_profit.toFixed(2)}</p>
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
          <div className="px-4 py-3 border-b border-dark-600 flex items-center justify-between">
            <span className="text-sm font-medium text-white">Paper Trades</span>
            <div className="flex items-center gap-3">
              <span className="text-xs text-gray-500">{trades.length} trade{trades.length !== 1 ? 's' : ''}</span>
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
          {trades.length === 0 ? (
            <div className="p-12 text-center">
              <Activity size={36} className="text-gray-600 mx-auto mb-3 opacity-30" />
              <p className="text-gray-500 text-sm">No paper trades yet.</p>
              <p className="text-gray-600 text-xs mt-1">Enable a strategy in paper mode and click "Run Now".</p>
            </div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="text-gray-500 border-b border-dark-600">
                    {['Symbol', 'Side', 'Qty', 'Entry', 'Exit', 'P&L', 'Status', 'Strategy', 'Opened'].map((h) => (
                      <th key={h} className="text-left px-3 py-2 font-medium">{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {trades.map((t) => (
                    <tr key={t.id} className="border-b border-dark-700 hover:bg-dark-750 transition-colors">
                      <td className="px-3 py-2 font-medium text-white">{t.symbol}</td>
                      <td className={`px-3 py-2 font-medium ${t.side === 'buy' ? 'text-green-400' : 'text-red-400'}`}>
                        {t.side.toUpperCase()}
                      </td>
                      <td className="px-3 py-2 text-gray-300">{t.quantity.toFixed(4)}</td>
                      <td className="px-3 py-2 text-gray-300">{t.entry_price?.toFixed(2) ?? '—'}</td>
                      <td className="px-3 py-2 text-gray-300">{t.exit_price?.toFixed(2) ?? '—'}</td>
                      <td className={`px-3 py-2 font-medium ${(t.pnl ?? 0) > 0 ? 'text-green-400' : (t.pnl ?? 0) < 0 ? 'text-red-400' : 'text-gray-500'}`}>
                        {t.pnl != null ? fmtUSD(t.pnl) : '—'}
                        {t.pnl_pct != null && <span className="text-gray-500 ml-1">({fmtPct(t.pnl_pct)})</span>}
                      </td>
                      <td className="px-3 py-2">
                        <span className={`px-1.5 py-0.5 rounded text-xs font-medium ${
                          t.status === 'open' ? 'bg-blue-500/15 text-blue-400' :
                          t.status === 'filled' ? 'bg-green-500/15 text-green-400' :
                          'bg-gray-700 text-gray-400'
                        }`}>
                          {t.status}
                        </span>
                      </td>
                      <td className="px-3 py-2 text-gray-400 max-w-[100px] truncate">{t.strategy_name ?? '—'}</td>
                      <td className="px-3 py-2 text-gray-500">
                        {t.opened_at ? new Date(t.opened_at).toLocaleString() : '—'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>

        {/* Live Feed (WebSocket Events) */}
        <div className="bg-dark-800 border border-dark-600 rounded-xl overflow-hidden">
          <div className="px-4 py-3 border-b border-dark-600 flex items-center gap-2">
            <Zap size={13} className="text-yellow-400" />
            <span className="text-sm font-medium text-white">Live Feed</span>
            <span className="ml-auto text-xs text-gray-600">WebSocket</span>
          </div>
          <div className="p-3 space-y-2 max-h-64 overflow-y-auto">
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
        </div>
      </div>
    </div>
  )
}

