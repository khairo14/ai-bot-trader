import { useState, useEffect } from 'react'
import { FlaskConical, Play, AlertTriangle, Download, BookOpen, TrendingUp, History, Eye, X, ChevronUp, ChevronDown, HelpCircle } from 'lucide-react'
import axios from 'axios'
import toast from 'react-hot-toast'
import { useStrategyRegistry } from '../hooks/useStrategyRegistry'

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
  id?: number
  strategy_name: string
  symbol: string
  timeframe?: string
  start_date?: string
  end_date?: string
  initial_capital?: number
  final_capital?: number
  total_return_pct: number
  annualized_return_pct: number
  max_drawdown_pct: number
  sharpe_ratio: number
  sortino_ratio: number | null
  profit_factor: number
  win_rate_pct: number
  total_trades: number
  avg_win: number
  avg_loss: number
  rr_ratio: number
  created_at?: string
}

const MetricRow = ({
  label, value, positive, tooltip,
}: {
  label: string
  value: string
  positive?: boolean
  tooltip?: { what: string; verdict: string }
}) => (
  <div className="flex justify-between items-center py-2 border-b border-dark-600">
    <div className="flex items-center gap-1.5">
      <span className="text-sm text-gray-400">{label}</span>
      {tooltip && (
        <div className="relative group/tip">
          <HelpCircle size={11} className="text-gray-600 group-hover/tip:text-gray-400 cursor-help transition-colors" />
          <div className="absolute bottom-full left-0 mb-2 w-60 p-3 bg-dark-700 border border-dark-500 rounded-xl text-xs text-gray-300 shadow-2xl z-50 opacity-0 group-hover/tip:opacity-100 pointer-events-none transition-opacity duration-150">
            <p className="leading-relaxed">{tooltip.what}</p>
            <p className="mt-1.5 pt-1.5 border-t border-dark-600 text-gray-500 leading-relaxed">{tooltip.verdict}</p>
          </div>
        </div>
      )}
    </div>
    <span className={`text-sm font-medium ${positive === undefined ? 'text-white' : positive ? 'text-green-400' : 'text-red-400'}`}>
      {value}
    </span>
  </div>
)

export default function Backtest() {
  const { brokerStrategies } = useStrategyRegistry()
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
    min_rr_ratio: 2.0,
    max_consecutive_losses: 3,
  })

  // Strategies available for the currently selected broker
  const availableStrategies = brokerStrategies[form.broker] ?? brokerStrategies['binance'] ?? []
  const [result, setResult] = useState<BacktestResult | null>(null)
  const [backtestId, setBacktestId] = useState<number | null>(null)
  const [loading, setLoading] = useState(false)
  const [errorDetail, setErrorDetail] = useState<string | null>(null)
  const [history, setHistory] = useState<BacktestResult[]>([])
  const [historyLoading, setHistoryLoading] = useState(false)
  const [modalResult, setModalResult] = useState<BacktestResult | null>(null)
  const [sortKey, setSortKey] = useState<string>('id')
  const [sortDir, setSortDir] = useState<'asc' | 'desc'>('desc')
  const [page, setPage] = useState(1)
  const PAGE_SIZE = 10

  const toggleSort = (key: string) => {
    if (sortKey === key) {
      setSortDir(d => d === 'asc' ? 'desc' : 'asc')
    } else {
      setSortKey(key)
      setSortDir('desc')
    }
    setPage(1)
  }

  const SortIcon = ({ col }: { col: string }) =>
    sortKey !== col
      ? <span className="text-gray-600 ml-0.5 text-[10px]">↕</span>
      : sortDir === 'asc'
        ? <ChevronUp size={10} className="inline ml-0.5" />
        : <ChevronDown size={10} className="inline ml-0.5" />

  const sortedHistory = [...history].sort((a, b) => {
    const av = (a as any)[sortKey] ?? 0
    const bv = (b as any)[sortKey] ?? 0
    if (av < bv) return sortDir === 'asc' ? -1 : 1
    if (av > bv) return sortDir === 'asc' ? 1 : -1
    return 0
  })
  const totalPages = Math.max(1, Math.ceil(sortedHistory.length / PAGE_SIZE))
  const pagedHistory = sortedHistory.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE)

  const fetchHistory = async () => {
    setHistoryLoading(true)
    try {
      const res = await axios.get('/api/backtest/results')
      const list: BacktestResult[] = res.data.results || []
      setHistory(list)
      // Restore the most recent result if nothing is currently shown
      if (list.length > 0) {
        setResult(prev => prev ?? list[0])
        setBacktestId(prev => prev ?? (list[0].id ?? null))
      }
    } catch {
      // silently ignore — history is non-critical
    } finally {
      setHistoryLoading(false)
    }
  }

  // Load saved strategies + history on mount
  useEffect(() => {
    axios.get('/api/strategies/').then(res => {
      setSavedStrategies(res.data.strategies || [])
    }).catch(() => {})
    fetchHistory()
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
      fetchHistory()
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

          {/* Broker */}
          <div>
            <label className="text-xs text-gray-500 block mb-1">Broker</label>
            <select
              value={form.broker}
              onChange={e => {
                const broker = e.target.value
                const strats = brokerStrategies[broker] ?? brokerStrategies['binance'] ?? []
                setForm(f => ({
                  ...f,
                  broker,
                  // Reset strategy to first valid one for new broker
                  strategy_name: strats.includes(f.strategy_name) ? f.strategy_name : strats[0],
                }))
              }}
              className="w-full bg-dark-700 border border-dark-500 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-brand-500"
            >
              <option value="binance">binance</option>
              <option value="alpaca">alpaca</option>
              <option value="ibkr">ibkr</option>
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
              {availableStrategies.map(s => (
                <option key={s} value={s}>{s}</option>
              ))}
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
              {['1m','3m','5m','15m','30m','1h','2h','4h','6h','12h','1d','1w'].map(tf => (
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
            { label: 'Min R:R Ratio', key: 'min_rr_ratio', type: 'number' },
            { label: 'Max Consecutive Losses', key: 'max_consecutive_losses', type: 'number' },
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
            <div>
              <p className="text-xs text-gray-500 uppercase tracking-wide font-medium">Results</p>
              {result ? (
                <>
                  <h2 className="text-base font-bold text-white mt-0.5">
                    {result.symbol}
                    <span className="ml-2 text-xs font-normal text-gray-500">{result.timeframe ?? ''} · {result.strategy_name}</span>
                  </h2>
                  {result.created_at && (
                    <p className="text-xs text-gray-600 mt-0.5">
                      Run {new Date(result.created_at).toLocaleString(undefined, { dateStyle: 'short', timeStyle: 'short' })}
                      {backtestId ? ` · #${backtestId}` : ''}
                    </p>
                  )}
                </>
              ) : (
                <h2 className="text-sm font-semibold text-gray-400 mt-0.5">—</h2>
              )}
            </div>
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
              <MetricRow label="Total Return" value={`${result!.total_return_pct?.toFixed(2)}%`} positive={result!.total_return_pct > 0}
                tooltip={{ what: 'Total percentage gain or loss over the entire test period.', verdict: 'Above 0% = profitable. Always compare against drawdown — a high return with large losses may not be worth it.' }} />
              <MetricRow label="Annualized Return" value={`${result!.annualized_return_pct?.toFixed(2)}%`} positive={result!.annualized_return_pct > 0}
                tooltip={{ what: 'Total return scaled to a 1-year rate, accounting for the test duration.', verdict: 'Above 20–30% is strong. Very high values on short tests can be misleading — longer test periods are more reliable.' }} />
              <MetricRow label="Max Drawdown" value={`${result!.max_drawdown_pct?.toFixed(2)}%`} positive={result!.max_drawdown_pct > -20}
                tooltip={{ what: 'The largest peak-to-trough drop in portfolio value during the test. Measures worst-case capital loss.', verdict: 'Keep above −20% for safer strategies. Below −30% means the strategy could blow your account in a bad streak.' }} />
              <MetricRow label="Sharpe Ratio" value={result!.sharpe_ratio?.toFixed(2)} positive={result!.sharpe_ratio > 1}
                tooltip={{ what: 'Risk-adjusted return: reward per unit of total volatility (both up and down swings). Annualised.', verdict: 'Below 1.0 = poor reward for risk. 1–2 = acceptable. Above 2 = excellent. Your value means returns don\'t justify the volatility.' }} />
              <MetricRow label="Sortino Ratio" value={result!.sortino_ratio != null ? result!.sortino_ratio.toFixed(2) : '—'} positive={(result!.sortino_ratio ?? 0) > 1}
                tooltip={{ what: 'Like Sharpe, but only penalises downside volatility (losing periods). More trader-relevant than Sharpe.', verdict: 'Above 1.0 = good. Above 2.0 = strong. A higher Sortino vs Sharpe means your losses are relatively controlled.' }} />
              <MetricRow label="Profit Factor" value={result!.profit_factor?.toFixed(2)} positive={result!.profit_factor > 1.5}
                tooltip={{ what: 'Gross profits ÷ gross losses. How much you earn for every dollar lost across all trades.', verdict: '1.0 = breakeven. Above 1.5 = solid edge. Above 2.0 = strong. Your value means each $1 lost generated $' + result!.profit_factor?.toFixed(2) + ' in gross profit.' }} />
              <MetricRow label="Win Rate" value={`${result!.win_rate_pct?.toFixed(1)}%`} positive={result!.win_rate_pct > 50}
                tooltip={{ what: 'Percentage of closed trades that finished in profit.', verdict: 'Below 50% is fine if your R:R ratio is high — trend-following strategies typically win 35–45% but profit because winners are much larger than losers.' }} />
              <MetricRow label="Total Trades" value={String(result!.total_trades)}
                tooltip={{ what: 'Number of completed trades in the test period.', verdict: 'More trades = more statistically meaningful results. Under 30 trades makes metrics unreliable.' }} />
              <MetricRow label="Avg Win" value={`$${result!.avg_win?.toFixed(2)}`} positive
                tooltip={{ what: 'Average dollar profit per winning trade.', verdict: 'Should ideally be significantly larger than Avg Loss to offset a sub-50% win rate.' }} />
              <MetricRow label="Avg Loss" value={`$${result!.avg_loss?.toFixed(2)}`}
                tooltip={{ what: 'Average dollar loss per losing trade (shown as positive number).', verdict: 'The lower relative to Avg Win, the better. Avg Win ÷ Avg Loss = your R:R ratio.' }} />
              <MetricRow label="R:R Ratio" value={result!.rr_ratio?.toFixed(2)} positive={result!.rr_ratio > 1.5}
                tooltip={{ what: 'Average Win ÷ Average Loss. How many dollars you make on winners vs how many you lose on losers.', verdict: 'Above 1.5 = good. Your ' + result!.rr_ratio?.toFixed(2) + 'x means winners are ' + result!.rr_ratio?.toFixed(2) + '× larger than losers — this is why the strategy stays profitable even with a sub-50% win rate.' }} />

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

      {/* Backtest History Table */}
      <div className="bg-dark-800 border border-dark-600 rounded-xl p-5">
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-center gap-2">
            <History size={16} className="text-brand-400" />
            <h2 className="text-sm font-semibold text-gray-300">Backtest History</h2>
            {historyLoading && <span className="text-xs text-gray-500">loading…</span>}
            {!historyLoading && <span className="text-xs text-gray-600">({history.length} run{history.length !== 1 ? 's' : ''})</span>}
          </div>
          <a
            href="/api/backtest/results/export/all"
            className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg bg-dark-700 text-gray-400 hover:text-blue-400 hover:bg-blue-500/10 text-xs transition-all"
          >
            <Download size={12} /> Export All CSV
          </a>
        </div>

        {history.length === 0 && !historyLoading ? (
          <p className="text-sm text-gray-600 text-center py-8">No backtest runs recorded yet.</p>
        ) : (
          <>
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="text-gray-500 border-b border-dark-600">
                    {([
                      { key: 'id', label: '#', align: 'left' },
                      { key: 'strategy_name', label: 'Strategy', align: 'left' },
                      { key: 'symbol', label: 'Symbol', align: 'left' },
                      { key: 'timeframe', label: 'TF', align: 'left' },
                      { key: 'total_return_pct', label: 'Return', align: 'right' },
                      { key: 'max_drawdown_pct', label: 'Drawdown', align: 'right' },
                      { key: 'sharpe_ratio', label: 'Sharpe', align: 'right' },
                      { key: 'win_rate_pct', label: 'Win %', align: 'right' },
                      { key: 'total_trades', label: 'Trades', align: 'right' },
                      { key: 'profit_factor', label: 'P.Factor', align: 'right' },
                      { key: 'created_at', label: 'Date', align: 'left' },
                    ] as { key: string; label: string; align: string }[]).map(col => (
                      <th
                        key={col.key}
                        onClick={() => toggleSort(col.key)}
                        className={`py-2 pr-3 font-medium cursor-pointer select-none hover:text-gray-300 transition-colors text-${col.align}`}
                      >
                        {col.label}<SortIcon col={col.key} />
                      </th>
                    ))}
                    <th className="text-right py-2 font-medium">Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {pagedHistory.map((h, idx) => {
                    const isActive = h.id != null && h.id === backtestId
                    return (
                      <tr
                        key={h.id ?? idx}
                        className={`border-b border-dark-700 hover:bg-dark-700/40 transition-colors ${isActive ? 'bg-brand-500/5 border-brand-500/20' : ''}`}
                      >
                        <td className="py-2 pr-3 text-gray-500">{h.id ?? '—'}</td>
                        <td className="py-2 pr-3 text-gray-300 font-medium">{h.strategy_name}</td>
                        <td className="py-2 pr-3 text-gray-300">{h.symbol}</td>
                        <td className="py-2 pr-3 text-gray-400">{h.timeframe ?? '—'}</td>
                        <td className={`py-2 pr-3 text-right font-medium ${(h.total_return_pct ?? 0) >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                          {h.total_return_pct?.toFixed(1)}%
                        </td>
                        <td className={`py-2 pr-3 text-right ${(h.max_drawdown_pct ?? 0) < -20 ? 'text-red-400' : 'text-yellow-400'}`}>
                          {h.max_drawdown_pct?.toFixed(1)}%
                        </td>
                        <td className={`py-2 pr-3 text-right ${(h.sharpe_ratio ?? 0) >= 1 ? 'text-green-400' : 'text-gray-400'}`}>
                          {h.sharpe_ratio?.toFixed(2)}
                        </td>
                        <td className={`py-2 pr-3 text-right ${(h.win_rate_pct ?? 0) >= 50 ? 'text-green-400' : 'text-red-400'}`}>
                          {h.win_rate_pct?.toFixed(1)}%
                        </td>
                        <td className="py-2 pr-3 text-right text-gray-300">{h.total_trades}</td>
                        <td className={`py-2 pr-3 text-right ${(h.profit_factor ?? 0) >= 1.5 ? 'text-green-400' : 'text-gray-400'}`}>
                          {h.profit_factor?.toFixed(2)}
                        </td>
                        <td className="py-2 pr-3 text-gray-500">
                          {h.created_at ? new Date(h.created_at).toLocaleString(undefined, { dateStyle: 'short', timeStyle: 'short' }) : '—'}
                        </td>
                        <td className="py-2 text-right">
                          <div className="flex items-center justify-end gap-1.5">
                            <button
                              onClick={() => setModalResult(h)}
                              className="flex items-center gap-1 px-2 py-1 rounded bg-dark-600 hover:bg-brand-500/20 hover:text-brand-400 text-gray-400 transition-all"
                              title="View this result"
                            >
                              <Eye size={11} /> View
                            </button>
                            {h.id && (
                              <a
                                href={`/api/backtest/results/${h.id}/export`}
                                className="flex items-center gap-1 px-2 py-1 rounded bg-dark-600 hover:bg-green-500/20 hover:text-green-400 text-gray-400 transition-all"
                                title="Download trades CSV"
                              >
                                <Download size={11} /> CSV
                              </a>
                            )}
                          </div>
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>

            {/* Pagination */}
            {totalPages > 1 && (
              <div className="flex items-center justify-between mt-4 pt-3 border-t border-dark-700">
                <span className="text-xs text-gray-500">
                  Page {page} of {totalPages} &middot; {history.length} total runs
                </span>
                <div className="flex items-center gap-1">
                  <button
                    onClick={() => setPage(1)}
                    disabled={page === 1}
                    className="px-2 py-1 rounded text-xs bg-dark-700 text-gray-400 hover:bg-dark-600 disabled:opacity-30 disabled:cursor-not-allowed transition-all"
                  >«</button>
                  <button
                    onClick={() => setPage(p => Math.max(1, p - 1))}
                    disabled={page === 1}
                    className="px-2 py-1 rounded text-xs bg-dark-700 text-gray-400 hover:bg-dark-600 disabled:opacity-30 disabled:cursor-not-allowed transition-all"
                  >‹</button>
                  {Array.from({ length: Math.min(5, totalPages) }, (_, i) => {
                    const start = Math.max(1, Math.min(page - 2, totalPages - 4))
                    const p = start + i
                    return (
                      <button
                        key={p}
                        onClick={() => setPage(p)}
                        className={`px-2.5 py-1 rounded text-xs transition-all ${
                          p === page
                            ? 'bg-brand-500 text-black font-semibold'
                            : 'bg-dark-700 text-gray-400 hover:bg-dark-600'
                        }`}
                      >{p}</button>
                    )
                  })}
                  <button
                    onClick={() => setPage(p => Math.min(totalPages, p + 1))}
                    disabled={page === totalPages}
                    className="px-2 py-1 rounded text-xs bg-dark-700 text-gray-400 hover:bg-dark-600 disabled:opacity-30 disabled:cursor-not-allowed transition-all"
                  >›</button>
                  <button
                    onClick={() => setPage(totalPages)}
                    disabled={page === totalPages}
                    className="px-2 py-1 rounded text-xs bg-dark-700 text-gray-400 hover:bg-dark-600 disabled:opacity-30 disabled:cursor-not-allowed transition-all"
                  >»</button>
                </div>
              </div>
            )}
          </>
        )}
      </div>

      {/* Result Detail Modal */}
      {modalResult && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm"
          onClick={() => setModalResult(null)}
        >
          <div
            className="bg-dark-800 border border-dark-600 rounded-xl p-6 w-full max-w-md mx-4 shadow-2xl"
            onClick={e => e.stopPropagation()}
          >
            {/* Modal header */}
            <div className="flex items-start justify-between mb-4">
              <div>
                <h3 className="text-sm font-semibold text-white">
                  {modalResult.strategy_name} · {modalResult.symbol}
                </h3>
                <p className="text-xs text-gray-500 mt-0.5">
                  {modalResult.timeframe ?? '—'}
                  {modalResult.start_date ? ` · ${modalResult.start_date}` : ''}
                  {modalResult.end_date ? ` → ${modalResult.end_date}` : ''}
                  {modalResult.id ? ` · #${modalResult.id}` : ''}
                </p>
                {modalResult.created_at && (
                  <p className="text-xs text-gray-600 mt-0.5">
                    Run {new Date(modalResult.created_at).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })}
                  </p>
                )}
              </div>
              <button
                onClick={() => setModalResult(null)}
                className="text-gray-500 hover:text-white transition-colors ml-4 shrink-0"
              >
                <X size={18} />
              </button>
            </div>

            {/* Metrics */}
            <div className="space-y-0.5">
              <MetricRow label="Total Return" value={`${modalResult.total_return_pct?.toFixed(2)}%`} positive={modalResult.total_return_pct > 0} />
              <MetricRow label="Annualized Return" value={`${modalResult.annualized_return_pct?.toFixed(2)}%`} positive={modalResult.annualized_return_pct > 0} />
              <MetricRow label="Max Drawdown" value={`${modalResult.max_drawdown_pct?.toFixed(2)}%`} positive={modalResult.max_drawdown_pct > -20} />
              <MetricRow label="Sharpe Ratio" value={modalResult.sharpe_ratio?.toFixed(2)} positive={modalResult.sharpe_ratio > 1} />
              <MetricRow label="Profit Factor" value={modalResult.profit_factor?.toFixed(2)} positive={modalResult.profit_factor > 1.5} />
              <MetricRow label="Win Rate" value={`${modalResult.win_rate_pct?.toFixed(1)}%`} positive={modalResult.win_rate_pct > 50} />
              <MetricRow label="Total Trades" value={String(modalResult.total_trades)} />
              <MetricRow label="Avg Win" value={`$${modalResult.avg_win?.toFixed(2)}`} positive />
              <MetricRow label="Avg Loss" value={`$${modalResult.avg_loss?.toFixed(2)}`} />
              <MetricRow label="R:R Ratio" value={modalResult.rr_ratio?.toFixed(2)} positive={modalResult.rr_ratio > 1.5} />
              {modalResult.initial_capital != null && (
                <MetricRow label="Initial Capital" value={`$${modalResult.initial_capital?.toLocaleString()}`} />
              )}
              {modalResult.final_capital != null && (
                <MetricRow label="Final Capital" value={`$${modalResult.final_capital?.toFixed(2)}`} positive={modalResult.final_capital > (modalResult.initial_capital ?? 0)} />
              )}
            </div>

            {/* Warning / pass */}
            {modalResult.max_drawdown_pct < -20 || modalResult.sharpe_ratio < 1 ? (
              <div className="flex items-start gap-2 mt-4 p-3 bg-yellow-900/20 border border-yellow-900/40 rounded-lg">
                <AlertTriangle size={14} className="text-yellow-500 mt-0.5 shrink-0" />
                <p className="text-xs text-yellow-400">Results below recommended thresholds.</p>
              </div>
            ) : (
              <div className="flex items-start gap-2 mt-4 p-3 bg-green-900/20 border border-green-900/40 rounded-lg">
                <TrendingUp size={14} className="text-green-400 mt-0.5 shrink-0" />
                <p className="text-xs text-green-400">Strategy passes quality checks.</p>
              </div>
            )}

            {/* Actions */}
            <div className="flex gap-2 mt-4">
              {modalResult.id && (
                <a
                  href={`/api/backtest/results/${modalResult.id}/export`}
                  className="flex-1 flex items-center justify-center gap-1.5 py-2 rounded-lg bg-dark-700 text-gray-400 hover:text-green-400 hover:bg-green-500/10 text-xs transition-all"
                >
                  <Download size={12} /> Download Trades CSV
                </a>
              )}
              <button
                onClick={() => setModalResult(null)}
                className="flex-1 py-2 rounded-lg bg-dark-700 text-gray-400 hover:text-white text-xs transition-all"
              >
                Close
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
