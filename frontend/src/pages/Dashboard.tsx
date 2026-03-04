import { useEffect, useState } from 'react'
import { TrendingUp, TrendingDown, Minus, Activity, RefreshCw, Wifi, WifiOff } from 'lucide-react'
import axios from 'axios'
import SignalCard from '../components/SignalCard'

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
  reasons: string
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

export default function Dashboard() {
  const [signals, setSignals] = useState<Signal[]>([])
  const [portfolio, setPortfolio] = useState<PortfolioSummary | null>(null)
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)

  const fetchAll = async (showSpinner = false) => {
    if (showSpinner) setRefreshing(true)
    // Fetch independently — a slow broker never blocks signals from loading
    const [sigResult, portResult] = await Promise.allSettled([
      axios.get('/api/signals/?limit=20'),
      axios.get('/api/portfolio/summary'),
    ])
    if (sigResult.status === 'fulfilled') setSignals(sigResult.value.data.signals || [])
    if (portResult.status === 'fulfilled') setPortfolio(portResult.value.data)
    setLoading(false)
    setRefreshing(false)
  }

  useEffect(() => {
    fetchAll()
    const interval = setInterval(() => fetchAll(), 30000)
    return () => clearInterval(interval)
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

      {/* Recent Signals */}
      <div>
        <h2 className="text-sm font-semibold text-gray-300 mb-3">Recent Signals</h2>
        {loading ? (
          <div className="text-gray-500 text-sm">Loading signals...</div>
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
