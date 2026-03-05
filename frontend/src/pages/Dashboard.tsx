import { useEffect, useState } from 'react'
import { TrendingUp, TrendingDown, Minus, Activity, RefreshCw, Wifi, WifiOff, CheckCircle, XCircle, Clock, Trash2, Brain } from 'lucide-react'
import axios from 'axios'
import toast from 'react-hot-toast'
import SignalCard from '../components/SignalCard'
import { SkeletonStat, SkeletonList } from '../components/Skeleton'

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
  reasons: string
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
  max_positions: number
  today_pnl: number
  circuit_breaker_pct: number
}

function formatBalance(total: number, currency: string): string {
  if (currency === 'USD' || currency === 'USDT') {
    return '$' + total.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
  }
  return total.toFixed(4) + ' ' + currency
}

function formatPnl(pnl: number): string {
  const sign = pnl >= 0 ? '+' : ''
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


export default function Dashboard() {
  const [signals, setSignals] = useState<Signal[]>([])
  const [pendingSignals, setPendingSignals] = useState<Signal[]>([])
  const [portfolio, setPortfolio] = useState<PortfolioSummary | null>(null)
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [actioning, setActioning] = useState<number | null>(null)
  const [mlStatus, setMlStatus] = useState<MLStatus | null>(null)

  const fetchAll = async (showSpinner = false) => {
    if (showSpinner) setRefreshing(true)
    // Fetch independently — a slow broker never blocks signals from loading
    const [sigResult, portResult, pendingResult, mlResult] = await Promise.allSettled([
      axios.get('/api/signals/?limit=20'),
      axios.get('/api/portfolio/summary'),
      axios.get('/api/signals/pending'),
      axios.get('/api/ml/status'),
    ])
    if (sigResult.status === 'fulfilled') setSignals(sigResult.value.data.signals || [])
    if (portResult.status === 'fulfilled') setPortfolio(portResult.value.data)
    if (pendingResult.status === 'fulfilled') setPendingSignals(pendingResult.value.data.signals || [])
    if (mlResult.status === 'fulfilled') setMlStatus(mlResult.value.data)
    setLoading(false)
    setRefreshing(false)
  }

  const handleApprove = async (id: number) => {
    setActioning(id)
    try {
      await axios.post(`/api/signals/${id}/approve`)
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
        <button
          onClick={() => fetchAll(true)}
          disabled={refreshing}
          className="flex items-center gap-1.5 text-xs text-gray-400 hover:text-white transition-colors px-3 py-1.5 bg-dark-700 border border-dark-600 rounded-lg"
        >
          <RefreshCw size={12} className={refreshing ? 'animate-spin' : ''} />
          Refresh
        </button>
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
                {b.connected ? `${formatBalance(b.available, b.currency)} available` : 'Offline — not configured'}
              </p>
            </div>
          )
        })}
      </div>

      {/* Stats Row */}
      <div className="grid grid-cols-3 gap-4">
        <StatCard
          label="Open Positions"
          value={portfolio ? String(portfolio.open_positions) : '—'}
          sub={portfolio ? `of ${portfolio.max_positions} max` : undefined}
        />
        <StatCard
          label="Today's P&L"
          value={portfolio ? formatPnl(portfolio.today_pnl) : '—'}
          color={pnlColor}
        />
        <StatCard
          label="Circuit Breaker"
          value={portfolio ? `${portfolio.circuit_breaker_pct}% limit` : '—'}
          sub="Daily max loss"
          color="text-brand-500"
        />
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
          {mlStatus?.last_retrain && (
            <span className="text-xs text-gray-500">
              Last retrain: {new Date(mlStatus.last_retrain).toLocaleDateString()}
            </span>
          )}
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
