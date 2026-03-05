import { useState, useEffect } from 'react'
import { FlaskConical, Play, AlertTriangle, Download, BookOpen, TrendingUp } from 'lucide-react'
import axios from 'axios'
import toast from 'react-hot-toast'

interface SavedStrategy {
  id: number
  name: string
  broker: string
  parameters: {
    strategy_type?: string
    symbol?: string
    timeframe?: string
    limit?: number
  } | null
}

interface BacktestResult {
  strategy_name: string
  symbol: string
  total_return_pct: number
  annualized_return_pct: number
  max_drawdown_pct: number
  sharpe_ratio: number
  profit_factor: number
  win_rate_pct: number
  total_trades: number
  avg_win: number
  avg_loss: number
  rr_ratio: number
}

const MetricRow = ({ label, value, positive }: { label: string, value: string, positive?: boolean }) => (
  <div className="flex justify-between py-2 border-b border-dark-600">
    <span className="text-sm text-gray-400">{label}</span>
    <span className={`text-sm font-medium ${positive === undefined ? 'text-white' : positive ? 'text-green-400' : 'text-red-400'}`}>
      {value}
    </span>
  </div>
)

export default function Backtest() {
  const [savedStrategies, setSavedStrategies] = useState<SavedStrategy[]>([])
  const [form, setForm] = useState({
    strategy_name: 'hybrid_macd_rsi',
    symbol: 'SOL/USDT',
    timeframe: '1h',
    start_date: '2024-01-01',
    end_date: '2026-01-01',
    initial_capital: 10000,
    commission_pct: 0.1,
    slippage_pct: 0.05,
    broker: 'binance',
  })
  const [result, setResult] = useState<BacktestResult | null>(null)
  const [backtestId, setBacktestId] = useState<number | null>(null)
  const [loading, setLoading] = useState(false)
  const [errorDetail, setErrorDetail] = useState<string | null>(null)

  // Load saved strategies from DB on mount
  useEffect(() => {
    axios.get('/api/strategies/').then(res => {
      setSavedStrategies(res.data.strategies || [])
    }).catch(() => {})
  }, [])

  // Auto-fill form when a saved strategy is selected
  const loadFromSaved = (strategyId: string) => {
    if (!strategyId) return
    const s = savedStrategies.find(x => x.id === Number(strategyId))
    if (!s) return
    setForm(f => ({
      ...f,
      broker: s.broker,
      strategy_name: s.parameters?.strategy_type || f.strategy_name,
      symbol: s.parameters?.symbol || f.symbol,
      timeframe: s.parameters?.timeframe || f.timeframe,
    }))
    toast.success(`Loaded "${s.name}"`)
  }

  const runBacktest = async () => {
    setLoading(true)
    setResult(null)
    setBacktestId(null)
    setErrorDetail(null)
    try {
      const res = await axios.post('/api/backtest/run', form)
      setResult(res.data.result)
      if (res.data.backtest_id) setBacktestId(res.data.backtest_id)
      toast.success('Backtest completed!')
    } catch (e: any) {
      const detail = e?.response?.data?.detail || e?.message || 'Backtest failed'
      setErrorDetail(typeof detail === 'string' ? detail : JSON.stringify(detail))
      toast.error('Backtest failed — see error below')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="p-6 space-y-6">
      <div>
        <h1 className="text-xl font-bold text-white">Backtesting Engine</h1>
        <p className="text-sm text-gray-500 mt-0.5">Replay historical data through your strategy. Validate before risking any money.</p>
      </div>

      <div className="grid grid-cols-2 gap-6">
        {/* Config Form */}
        <div className="bg-dark-800 border border-dark-600 rounded-xl p-5 space-y-4">
          <h2 className="text-sm font-semibold text-gray-300">Configuration</h2>

          {/* Load from saved strategy */}
          <div className="bg-dark-700 border border-dark-500 rounded-lg p-3 space-y-2">
            <label className="text-xs text-brand-400 font-medium flex items-center gap-1.5">
              <BookOpen size={12} /> Load from Saved Strategy
            </label>
            <select
              defaultValue=""
              onChange={e => loadFromSaved(e.target.value)}
              className="w-full bg-dark-600 border border-dark-400 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-brand-500"
            >
              <option value="" disabled>— select to auto-fill —</option>
              {savedStrategies.map(s => (
                <option key={s.id} value={s.id}>
                  {s.name} ({s.broker} · {s.parameters?.symbol ?? '?'} · {s.parameters?.timeframe ?? '?'})
                </option>
              ))}
            </select>
          </div>

          {/* Algorithm type */}
          <div>
            <label className="text-xs text-gray-500 block mb-1">Algorithm</label>
            <select
              value={form.strategy_name}
              onChange={e => setForm(f => ({ ...f, strategy_name: e.target.value }))}
              className="w-full bg-dark-700 border border-dark-500 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-brand-500"
            >
              <option value="hybrid_macd_rsi">hybrid_macd_rsi</option>
              <option value="momentum_breakout">momentum_breakout</option>
              <option value="mean_reversion_bb">mean_reversion_bb</option>
            </select>
          </div>

          {/* Broker */}
          <div>
            <label className="text-xs text-gray-500 block mb-1">Broker</label>
            <select
              value={form.broker}
              onChange={e => setForm(f => ({ ...f, broker: e.target.value }))}
              className="w-full bg-dark-700 border border-dark-500 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-brand-500"
            >
              <option value="binance">binance</option>
              <option value="alpaca">alpaca</option>
              <option value="ibkr">ibkr</option>
            </select>
          </div>

          {/* Timeframe */}
          <div>
            <label className="text-xs text-gray-500 block mb-1">Timeframe</label>
            <select
              value={form.timeframe}
              onChange={e => setForm(f => ({ ...f, timeframe: e.target.value }))}
              className="w-full bg-dark-700 border border-dark-500 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-brand-500"
            >
              {['1m','5m','15m','30m','1h','2h','4h','6h','12h','1d'].map(tf => (
                <option key={tf} value={tf}>{tf}</option>
              ))}
            </select>
          </div>

          {[
            { label: 'Symbol', key: 'symbol', type: 'text', placeholder: 'SOL/USDT' },
            { label: 'Start Date', key: 'start_date', type: 'date' },
            { label: 'End Date', key: 'end_date', type: 'date' },
            { label: 'Initial Capital ($)', key: 'initial_capital', type: 'number' },
            { label: 'Commission (%)', key: 'commission_pct', type: 'number' },
            { label: 'Slippage (%)', key: 'slippage_pct', type: 'number' },
          ].map(({ label, key, type, placeholder }) => (
            <div key={key}>
              <label className="text-xs text-gray-500 block mb-1">{label}</label>
              <input
                type={type}
                value={(form as any)[key]}
                onChange={e => setForm(f => ({ ...f, [key]: type === 'number' ? Number(e.target.value) : e.target.value }))}
                placeholder={placeholder}
                className="w-full bg-dark-700 border border-dark-500 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-brand-500"
              />
            </div>
          ))}

          <button
            onClick={runBacktest}
            disabled={loading}
            className="w-full flex items-center justify-center gap-2 bg-brand-500 hover:bg-green-400 text-black font-semibold py-2.5 rounded-lg transition-all disabled:opacity-50"
          >
            {loading ? 'Running...' : <><Play size={16} /> Run Backtest</>}
          </button>
        </div>

        {/* Results */}
        <div className="bg-dark-800 border border-dark-600 rounded-xl p-5">
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-sm font-semibold text-gray-300">Results</h2>
            <div className="flex items-center gap-2">
              {backtestId && (
                <a
                  href={`/api/backtest/results/${backtestId}/export`}
                  className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg bg-dark-700 text-gray-400 hover:text-green-400 hover:bg-green-500/10 text-xs transition-all"
                  title="Download trade-by-trade CSV for this run"
                >
                  <Download size={12} /> Trades CSV
                </a>
              )}
              <a
                href="/api/backtest/results/export/all"
                className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg bg-dark-700 text-gray-400 hover:text-blue-400 hover:bg-blue-500/10 text-xs transition-all"
                title="Download summary of all backtest runs"
              >
                <Download size={12} /> All Runs CSV
              </a>
            </div>
          </div>
          {!result && !errorDetail ? (
            <div className="flex flex-col items-center justify-center h-64 text-gray-600">
              <FlaskConical size={40} className="mb-3 opacity-30" />
              <p className="text-sm">Run a backtest to see results here</p>
            </div>
          ) : errorDetail ? (
            <div className="flex flex-col gap-2">
              <div className="flex items-start gap-2 p-3 bg-red-900/20 border border-red-900/40 rounded-lg">
                <AlertTriangle size={16} className="text-red-400 mt-0.5 shrink-0" />
                <div>
                  <p className="text-xs font-semibold text-red-400 mb-1">Backtest failed</p>
                  <p className="text-xs text-red-300 break-words">{errorDetail}</p>
                </div>
              </div>
            </div>
          ) : (
            <div className="space-y-1">
              <MetricRow label="Total Return" value={`${result!.total_return_pct?.toFixed(2)}%`} positive={result!.total_return_pct > 0} />
              <MetricRow label="Annualized Return" value={`${result!.annualized_return_pct?.toFixed(2)}%`} positive={result!.annualized_return_pct > 0} />
              <MetricRow label="Max Drawdown" value={`${result!.max_drawdown_pct?.toFixed(2)}%`} positive={result!.max_drawdown_pct > -20} />
              <MetricRow label="Sharpe Ratio" value={result!.sharpe_ratio?.toFixed(2)} positive={result!.sharpe_ratio > 1} />
              <MetricRow label="Profit Factor" value={result!.profit_factor?.toFixed(2)} positive={result!.profit_factor > 1.5} />
              <MetricRow label="Win Rate" value={`${result!.win_rate_pct?.toFixed(1)}%`} positive={result!.win_rate_pct > 50} />
              <MetricRow label="Total Trades" value={String(result!.total_trades)} />
              <MetricRow label="Avg Win" value={`$${result!.avg_win?.toFixed(2)}`} positive />
              <MetricRow label="Avg Loss" value={`$${result!.avg_loss?.toFixed(2)}`} />
              <MetricRow label="R:R Ratio" value={result!.rr_ratio?.toFixed(2)} positive={result!.rr_ratio > 1.5} />

              {result!.max_drawdown_pct < -20 || result!.sharpe_ratio < 1 ? (
                <div className="flex items-start gap-2 mt-4 p-3 bg-yellow-900/20 border border-yellow-900/40 rounded-lg">
                  <AlertTriangle size={16} className="text-yellow-500 mt-0.5 shrink-0" />
                  <p className="text-xs text-yellow-400">Results below recommended thresholds. Review strategy before forward testing.</p>
                </div>
              ) : (
                <div className="flex items-start gap-2 mt-4 p-3 bg-green-900/20 border border-green-900/40 rounded-lg">
                  <TrendingUp size={16} className="text-green-400 mt-0.5 shrink-0" />
                  <p className="text-xs text-green-400">Strategy passes quality checks. Ready for forward testing.</p>
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
