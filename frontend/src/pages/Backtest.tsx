import { useState } from 'react'
import { FlaskConical, Play, TrendingUp, AlertTriangle } from 'lucide-react'
import axios from 'axios'
import toast from 'react-hot-toast'

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
  const [form, setForm] = useState({
    strategy_name: 'hybrid_macd_rsi',
    symbol: 'BTC/USDT',
    timeframe: '1h',
    start_date: '2024-01-01',
    end_date: '2025-01-01',
    initial_capital: 10000,
    commission_pct: 0.1,
    slippage_pct: 0.05,
    broker: 'binance',
  })
  const [result, setResult] = useState<BacktestResult | null>(null)
  const [loading, setLoading] = useState(false)

  const runBacktest = async () => {
    setLoading(true)
    setResult(null)
    try {
      const res = await axios.post('/api/backtest/run', form)
      setResult(res.data.result)
      toast.success('Backtest completed!')
    } catch (e: any) {
      toast.error(e?.response?.data?.detail || 'Backtest failed')
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

          {[
            { label: 'Strategy', key: 'strategy_name', type: 'text' },
            { label: 'Symbol', key: 'symbol', type: 'text', placeholder: 'BTC/USDT' },
            { label: 'Timeframe', key: 'timeframe', type: 'text', placeholder: '1h' },
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
          <h2 className="text-sm font-semibold text-gray-300 mb-4">Results</h2>
          {!result ? (
            <div className="flex flex-col items-center justify-center h-64 text-gray-600">
              <FlaskConical size={40} className="mb-3 opacity-30" />
              <p className="text-sm">Run a backtest to see results here</p>
            </div>
          ) : (
            <div className="space-y-1">
              <MetricRow label="Total Return" value={`${result.total_return_pct?.toFixed(2)}%`} positive={result.total_return_pct > 0} />
              <MetricRow label="Annualized Return" value={`${result.annualized_return_pct?.toFixed(2)}%`} positive={result.annualized_return_pct > 0} />
              <MetricRow label="Max Drawdown" value={`${result.max_drawdown_pct?.toFixed(2)}%`} positive={result.max_drawdown_pct > -20} />
              <MetricRow label="Sharpe Ratio" value={result.sharpe_ratio?.toFixed(2)} positive={result.sharpe_ratio > 1} />
              <MetricRow label="Profit Factor" value={result.profit_factor?.toFixed(2)} positive={result.profit_factor > 1.5} />
              <MetricRow label="Win Rate" value={`${result.win_rate_pct?.toFixed(1)}%`} positive={result.win_rate_pct > 50} />
              <MetricRow label="Total Trades" value={String(result.total_trades)} />
              <MetricRow label="Avg Win" value={`$${result.avg_win?.toFixed(2)}`} positive />
              <MetricRow label="Avg Loss" value={`$${result.avg_loss?.toFixed(2)}`} />
              <MetricRow label="R:R Ratio" value={result.rr_ratio?.toFixed(2)} positive={result.rr_ratio > 1.5} />

              {result.max_drawdown_pct < -20 || result.sharpe_ratio < 1 ? (
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
