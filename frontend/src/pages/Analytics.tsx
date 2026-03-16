import { useEffect, useState } from 'react'
import axios from 'axios'
import {
  LineChart, Line, BarChart, Bar, XAxis, YAxis, CartesianGrid,
  Tooltip, ResponsiveContainer, ReferenceLine, Cell
} from 'recharts'
import { TrendingUp, TrendingDown, Activity, RefreshCw, Award, Zap, BarChart2 } from 'lucide-react'
import { SkeletonStat } from '../components/Skeleton'

// ── Types ─────────────────────────────────────────────────────────────────────
interface EquityPoint { date: string; cumulative_pnl: number; pnl_pct: number; trade_index: number }
interface MonthlyReturn { year: number; month: number; pnl_pct: number; label: string }
interface BrokerBreakdown { broker: string; total: number; wins: number; losses: number; win_rate: number; avg_pnl: number; total_pnl: number }
interface StratBreakdown { strategy_name: string; total: number; wins: number; losses: number; win_rate: number; avg_pnl: number }
interface SymBreakdown   { symbol:        string; total: number; wins: number; losses: number; win_rate: number; avg_pnl: number }
interface HourBreakdown  { hour:          number; total: number; wins: number; win_rate: number }
interface SharpePoint    { date: string; sharpe: number }
interface Summary {
  total_trades: number; win_rate: number; avg_pnl: number
  total_pnl: number; best_trade: number; worst_trade: number
}
interface AnalyticsData {
  equity_curve:    EquityPoint[]
  monthly_returns: MonthlyReturn[]
  by_strategy:     StratBreakdown[]
  by_symbol:       SymBreakdown[]
  by_broker:       BrokerBreakdown[]
  by_hour:         HourBreakdown[]
  rolling_sharpe:  SharpePoint[]
  summary:         Summary
}

// ── Helpers ───────────────────────────────────────────────────────────────────
const pct = (v: number) => `${v >= 0 ? '+' : ''}${v.toFixed(2)}%`
const pctColor = (v: number) => v >= 0 ? 'text-green-400' : 'text-red-400'
const monthColor = (v: number) => {
  if (v > 2) return '#22c55e'
  if (v > 0) return '#86efac'
  if (v > -2) return '#fca5a5'
  return '#ef4444'
}

const StatCard = ({ label, value, color = 'text-white', sub }: { label: string; value: string; color?: string; sub?: string }) => (
  <div className="bg-dark-800 border border-dark-600 rounded-xl p-4">
    <p className="text-xs text-gray-500 uppercase tracking-wider mb-1">{label}</p>
    <p className={`text-2xl font-bold ${color}`}>{value}</p>
    {sub && <p className="text-xs text-gray-600 mt-0.5">{sub}</p>}
  </div>
)

const CHART_TOOLTIP_STYLE = {
  contentStyle: { background: '#1a1a1a', border: '1px solid #333', borderRadius: 8, fontSize: 12 },
  labelStyle: { color: '#9ca3af' },
}

// ── Empty state ───────────────────────────────────────────────────────────────
function EmptyState() {
  return (
    <div className="flex flex-col items-center justify-center py-20 text-center">
      <Activity size={40} className="text-gray-700 mb-4" />
      <p className="text-gray-400 font-medium mb-1">No resolved trade outcomes yet</p>
      <p className="text-xs text-gray-600 max-w-xs">
        Analytics populate once the nightly outcome resolver has processed at least one signal.
        Run a forward test to get started.
      </p>
    </div>
  )
}

// ── Main page ─────────────────────────────────────────────────────────────────
export default function Analytics() {
  const [data, setData] = useState<AnalyticsData | null>(null)
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [mode, setMode] = useState<'all' | 'paper' | 'live'>('all')

  const fetchAnalytics = async (spinner = false, m = mode) => {
    if (spinner) setRefreshing(true)
    try {
      const r = await axios.get('/api/analytics/summary', { params: { mode: m } })
      setData(r.data)
    } catch (_) {}
    setLoading(false)
    setRefreshing(false)
  }

  useEffect(() => { fetchAnalytics() }, [])

  const handleMode = (m: 'all' | 'paper' | 'live') => {
    setMode(m)
    setLoading(true)
    fetchAnalytics(false, m)
  }

  const s = data?.summary

  return (
    <div className="p-6 space-y-6 max-w-7xl mx-auto">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-lg font-bold text-white">Performance Analytics</h1>
          <p className="text-xs text-gray-500 mt-0.5">Historical signal outcomes · rolling statistics</p>
        </div>
        <div className="flex items-center gap-3">
          {/* Paper / Live / All tabs */}
          <div className="flex items-center bg-dark-800 border border-dark-600 rounded-lg p-0.5 text-xs">
            {(['all', 'paper', 'live'] as const).map(m => (
              <button
                key={m}
                onClick={() => handleMode(m)}
                className={`px-3 py-1 rounded-md capitalize transition-colors ${
                  mode === m
                    ? 'bg-blue-600 text-white font-medium'
                    : 'text-gray-400 hover:text-white'
                }`}
              >
                {m}
              </button>
            ))}
          </div>
          <button
            onClick={() => fetchAnalytics(true)}
            disabled={refreshing}
            className="flex items-center gap-1.5 text-xs text-gray-400 hover:text-white transition-colors"
          >
            <RefreshCw size={13} className={refreshing ? 'animate-spin' : ''} />
            Refresh
          </button>
        </div>
      </div>

      {loading ? (
        <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-4">
          {Array(6).fill(0).map((_, i) => <SkeletonStat key={i} />)}
        </div>
      ) : !data || data.summary.total_trades === 0 ? (
        <EmptyState />
      ) : (
        <>
          {/* Summary stats row */}
          <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-4">
            <StatCard label="Total Trades"   value={String(s!.total_trades)} />
            <StatCard label="Win Rate"        value={`${s!.win_rate}%`}          color={s!.win_rate >= 50 ? 'text-green-400' : 'text-red-400'} />
            <StatCard label="Avg P&L / Signal" value={pct(s!.avg_pnl)}           color={pctColor(s!.avg_pnl)} />
            <StatCard label="Total P&L"        value={pct(s!.total_pnl)}          color={pctColor(s!.total_pnl)} />
            <StatCard label="Best Trade"       value={pct(s!.best_trade)}         color="text-green-400" />
            <StatCard label="Worst Trade"      value={pct(s!.worst_trade)}        color="text-red-400" />
          </div>

          {/* Equity curve */}
          <div className="bg-dark-800 border border-dark-600 rounded-xl p-5">
            <div className="flex items-center gap-2 mb-4">
              <TrendingUp size={14} className="text-brand-400" />
              <span className="text-sm font-semibold text-gray-300">Equity Curve (Cumulative P&L %)</span>
            </div>
            <ResponsiveContainer width="100%" height={220}>
              <LineChart data={data.equity_curve}>
                <CartesianGrid strokeDasharray="3 3" stroke="#222" />
                <XAxis dataKey="trade_index" tick={{ fontSize: 10, fill: '#6b7280' }} label={{ value: 'Trade #', position: 'insideBottomRight', offset: -10, fontSize: 10, fill: '#6b7280' }} />
                <YAxis tick={{ fontSize: 10, fill: '#6b7280' }} tickFormatter={v => `${v}%`} />
                <Tooltip {...CHART_TOOLTIP_STYLE} formatter={(v: number) => [`${v.toFixed(2)}%`, 'Cumulative P&L']} />
                <ReferenceLine y={0} stroke="#444" strokeDasharray="4 4" />
                <Line type="monotone" dataKey="cumulative_pnl" stroke="#6366f1" strokeWidth={2} dot={false} />
              </LineChart>
            </ResponsiveContainer>
          </div>

          {/* Monthly returns heatmap */}
          {data.monthly_returns.length > 0 && (
            <div className="bg-dark-800 border border-dark-600 rounded-xl p-5">
              <div className="flex items-center gap-2 mb-4">
                <BarChart2 size={14} className="text-brand-400" />
                <span className="text-sm font-semibold text-gray-300">Monthly Returns</span>
              </div>
              <ResponsiveContainer width="100%" height={180}>
                <BarChart data={data.monthly_returns}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#222" />
                  <XAxis dataKey="label" tick={{ fontSize: 10, fill: '#6b7280' }} />
                  <YAxis tick={{ fontSize: 10, fill: '#6b7280' }} tickFormatter={v => `${v}%`} />
                  <Tooltip {...CHART_TOOLTIP_STYLE} formatter={(v: number) => [`${v.toFixed(2)}%`, 'P&L']} />
                  <ReferenceLine y={0} stroke="#444" />
                  <Bar dataKey="pnl_pct" radius={[3, 3, 0, 0]}>
                    {data.monthly_returns.map((entry, i) => (
                      <Cell key={i} fill={monthColor(entry.pnl_pct)} />
                    ))}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            </div>
          )}

          {/* Win rate by strategy + symbol */}
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
            {/* By strategy */}
            <div className="bg-dark-800 border border-dark-600 rounded-xl p-5">
              <div className="flex items-center gap-2 mb-4">
                <Zap size={14} className="text-brand-400" />
                <span className="text-sm font-semibold text-gray-300">Win Rate by Strategy</span>
              </div>
              {data.by_strategy.length === 0 ? (
                <p className="text-xs text-gray-600">No data</p>
              ) : (
                <div className="space-y-3">
                  {data.by_strategy.map(s => (
                    <div key={s.strategy_name}>
                      <div className="flex items-center justify-between text-xs mb-1">
                        <span className="text-gray-400 truncate max-w-[60%]">{s.strategy_name}</span>
                        <span className={s.win_rate >= 50 ? 'text-green-400' : 'text-red-400'}>{s.win_rate}% — {s.total} trades</span>
                      </div>
                      <div className="h-1.5 bg-dark-700 rounded-full overflow-hidden">
                        <div className={`h-full rounded-full ${s.win_rate >= 50 ? 'bg-green-500' : 'bg-red-500'}`} style={{ width: `${s.win_rate}%` }} />
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>

            {/* By symbol */}
            <div className="bg-dark-800 border border-dark-600 rounded-xl p-5">
              <div className="flex items-center gap-2 mb-4">
                <Award size={14} className="text-brand-400" />
                <span className="text-sm font-semibold text-gray-300">Win Rate by Symbol</span>
              </div>
              {data.by_symbol.length === 0 ? (
                <p className="text-xs text-gray-600">No data</p>
              ) : (
                <div className="space-y-3">
                  {data.by_symbol.map(s => (
                    <div key={s.symbol}>
                      <div className="flex items-center justify-between text-xs mb-1">
                        <span className="text-gray-400">{s.symbol}</span>
                        <span className={s.win_rate >= 50 ? 'text-green-400' : 'text-red-400'}>{s.win_rate}% — {s.total} trades</span>
                      </div>
                      <div className="h-1.5 bg-dark-700 rounded-full overflow-hidden">
                        <div className={`h-full rounded-full ${s.win_rate >= 50 ? 'bg-green-500' : 'bg-red-500'}`} style={{ width: `${s.win_rate}%` }} />
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>

          {/* Performance by Broker */}
          {data.by_broker && data.by_broker.length > 0 && (
            <div className="bg-dark-800 border border-dark-600 rounded-xl p-5">
              <div className="flex items-center gap-2 mb-4">
                <BarChart2 size={14} className="text-brand-400" />
                <span className="text-sm font-semibold text-gray-300">Performance by Broker</span>
              </div>
              <div className="overflow-x-auto">
                <table className="w-full text-xs">
                  <thead>
                    <tr className="text-gray-500 border-b border-dark-600">
                      <th className="text-left py-2 pr-4 font-medium">Broker</th>
                      <th className="text-right py-2 px-3 font-medium">Trades</th>
                      <th className="text-right py-2 px-3 font-medium">Wins</th>
                      <th className="text-right py-2 px-3 font-medium">Losses</th>
                      <th className="text-right py-2 px-3 font-medium">Win Rate</th>
                      <th className="text-right py-2 px-3 font-medium">Avg P&L</th>
                      <th className="text-right py-2 pl-3 font-medium">Total P&L</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.by_broker.map(b => (
                      <tr key={b.broker} className="border-b border-dark-700 hover:bg-dark-750 transition-colors">
                        <td className="py-2 pr-4 font-medium text-white capitalize">{b.broker}</td>
                        <td className="py-2 px-3 text-gray-300 text-right">{b.total}</td>
                        <td className="py-2 px-3 text-green-400 text-right">{b.wins}</td>
                        <td className="py-2 px-3 text-red-400 text-right">{b.losses}</td>
                        <td className={`py-2 px-3 font-medium text-right ${b.win_rate >= 50 ? 'text-green-400' : 'text-red-400'}`}>{b.win_rate}%</td>
                        <td className={`py-2 px-3 font-mono text-right ${b.avg_pnl >= 0 ? 'text-green-400' : 'text-red-400'}`}>{b.avg_pnl >= 0 ? '+' : ''}{b.avg_pnl.toFixed(3)}%</td>
                        <td className={`py-2 pl-3 font-mono text-right ${b.total_pnl >= 0 ? 'text-green-400' : 'text-red-400'}`}>{b.total_pnl >= 0 ? '+' : ''}{b.total_pnl.toFixed(2)}%</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <div className="mt-4 space-y-2">
                {data.by_broker.map(b => (
                  <div key={b.broker}>
                    <div className="flex items-center justify-between text-xs mb-1">
                      <span className="text-gray-400 capitalize">{b.broker}</span>
                      <span className={b.win_rate >= 50 ? 'text-green-400' : 'text-red-400'}>{b.win_rate}% win rate</span>
                    </div>
                    <div className="h-1.5 bg-dark-700 rounded-full overflow-hidden">
                      <div className={`h-full rounded-full ${b.win_rate >= 50 ? 'bg-green-500' : 'bg-red-500'}`} style={{ width: `${b.win_rate}%` }} />
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Win rate by hour + rolling Sharpe */}
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
            {/* By hour */}
            {data.by_hour.length > 0 && (
              <div className="bg-dark-800 border border-dark-600 rounded-xl p-5">
                <div className="flex items-center gap-2 mb-4">
                  <Activity size={14} className="text-brand-400" />
                  <span className="text-sm font-semibold text-gray-300">Win Rate by Hour (UTC)</span>
                </div>
                <ResponsiveContainer width="100%" height={160}>
                  <BarChart data={data.by_hour}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#222" />
                    <XAxis dataKey="hour" tick={{ fontSize: 10, fill: '#6b7280' }} tickFormatter={h => `${h}:00`} />
                    <YAxis tick={{ fontSize: 10, fill: '#6b7280' }} tickFormatter={v => `${v}%`} domain={[0, 100]} />
                    <Tooltip {...CHART_TOOLTIP_STYLE} formatter={(v: number) => [`${v}%`, 'Win Rate']} labelFormatter={h => `${h}:00 UTC`} />
                    <ReferenceLine y={50} stroke="#444" strokeDasharray="4 4" />
                    <Bar dataKey="win_rate" radius={[3, 3, 0, 0]}>
                      {data.by_hour.map((entry, i) => (
                        <Cell key={i} fill={entry.win_rate >= 50 ? '#22c55e' : '#ef4444'} />
                      ))}
                    </Bar>
                  </BarChart>
                </ResponsiveContainer>
              </div>
            )}

            {/* Rolling Sharpe */}
            {data.rolling_sharpe.length > 0 && (
              <div className="bg-dark-800 border border-dark-600 rounded-xl p-5">
                <div className="flex items-center gap-2 mb-4">
                  <TrendingDown size={14} className="text-brand-400" />
                  <span className="text-sm font-semibold text-gray-300">Rolling Sharpe Ratio (30 trades)</span>
                </div>
                <ResponsiveContainer width="100%" height={160}>
                  <LineChart data={data.rolling_sharpe}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#222" />
                    <XAxis dataKey="date" hide />
                    <YAxis tick={{ fontSize: 10, fill: '#6b7280' }} />
                    <Tooltip {...CHART_TOOLTIP_STYLE} formatter={(v: number) => [v.toFixed(3), 'Sharpe']} />
                    <ReferenceLine y={0} stroke="#444" />
                    <ReferenceLine y={1} stroke="#22c55e" strokeDasharray="4 4" strokeOpacity={0.5} />
                    <Line type="monotone" dataKey="sharpe" stroke="#f59e0b" strokeWidth={2} dot={false} />
                  </LineChart>
                </ResponsiveContainer>
              </div>
            )}
          </div>
        </>
      )}
    </div>
  )
}
