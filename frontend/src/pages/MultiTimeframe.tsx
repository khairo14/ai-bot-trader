import { useState, useEffect } from 'react'
import axios from 'axios'
import toast from 'react-hot-toast'
import { GitBranch, RefreshCw, TrendingUp, TrendingDown, Minus, AlertTriangle } from 'lucide-react'
import { useStrategyRegistry } from '../hooks/useStrategyRegistry'

// ── Types ─────────────────────────────────────────────────────────────────────
interface TFResult {
  timeframe: string
  signal: string
  confidence: number
  regime: string | null
  reasons: string[]
  error: string | null
  agrees_with_consensus: boolean
}

interface ConfluenceData {
  symbol: string
  broker: string
  strategy_type: string
  consensus: string
  confluence_score: number
  timeframes: TFResult[]
}

// ── Helpers ───────────────────────────────────────────────────────────────────
const SIGNAL_STYLE: Record<string, { color: string; bg: string; border: string; Icon: React.ElementType }> = {
  BUY:   { color: 'text-green-400',  bg: 'bg-green-900/20',  border: 'border-green-900/40',  Icon: TrendingUp },
  SELL:  { color: 'text-orange-400', bg: 'bg-orange-900/20', border: 'border-orange-900/40', Icon: TrendingDown },
  SHORT: { color: 'text-red-400',    bg: 'bg-red-900/20',    border: 'border-red-900/40',    Icon: TrendingDown },
  HOLD:  { color: 'text-gray-500',   bg: 'bg-dark-700',      border: 'border-dark-600',      Icon: Minus },
  MIXED: { color: 'text-yellow-400', bg: 'bg-yellow-900/20', border: 'border-yellow-900/40', Icon: AlertTriangle },
}

function ScoreBar({ score }: { score: number }) {
  const pct = Math.round(score * 100)
  const color = pct >= 80 ? 'bg-green-500' : pct >= 50 ? 'bg-yellow-500' : 'bg-red-500'
  return (
    <div className="flex items-center gap-2">
      <div className="flex-1 h-1.5 bg-dark-600 rounded-full overflow-hidden">
        <div className={`h-full rounded-full ${color} transition-all`} style={{ width: `${pct}%` }} />
      </div>
      <span className="text-xs font-mono text-gray-300 w-8 text-right">{pct}%</span>
    </div>
  )
}

// ── Confluence Card ─────────────────────────────────────────────────────────
function ConfluenceCard({ data }: { data: ConfluenceData }) {
  const cfg = SIGNAL_STYLE[data.consensus] ?? SIGNAL_STYLE.HOLD
  return (
    <div className={`bg-dark-800 border ${cfg.border} rounded-xl p-5 space-y-4`}>
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <div className={`${cfg.bg} ${cfg.color} p-1.5 rounded-lg`}>
            <cfg.Icon size={16} />
          </div>
          <div>
            <p className="text-sm font-bold text-white">{data.symbol}</p>
            <p className="text-xs text-gray-500">{data.broker} · {data.strategy_type}</p>
          </div>
        </div>
        <div className="text-right">
          <span className={`text-sm font-bold px-3 py-1 rounded-lg ${cfg.bg} ${cfg.color} border ${cfg.border}`}>
            {data.consensus}
          </span>
        </div>
      </div>

      {/* Confluence score */}
      <div>
        <div className="flex items-center justify-between mb-1">
          <span className="text-xs text-gray-500">Confluence</span>
          <span className="text-xs text-gray-400">{data.timeframes.filter(t => t.agrees_with_consensus).length}/{data.timeframes.length} timeframes agree</span>
        </div>
        <ScoreBar score={data.confluence_score} />
      </div>

      {/* Per-TF breakdown */}
      <div className="space-y-2">
        {data.timeframes.map(tf => {
          const tcfg = SIGNAL_STYLE[tf.signal] ?? SIGNAL_STYLE.HOLD
          return (
            <div key={tf.timeframe} className={`flex items-center justify-between p-2.5 rounded-lg ${tf.agrees_with_consensus ? `${tcfg.bg} border ${tcfg.border}` : 'bg-dark-700 border border-dark-600'}`}>
              <div className="flex items-center gap-2">
                <span className="text-xs font-mono text-gray-400 w-6">{tf.timeframe}</span>
                <span className={`text-xs font-bold ${tcfg.color}`}>{tf.signal}</span>
                {tf.regime && <span className="text-[10px] bg-dark-600 text-gray-500 px-1.5 py-0.5 rounded">{tf.regime}</span>}
                {tf.error && <span className="text-[10px] text-red-400">Error</span>}
              </div>
              <div className="flex items-center gap-2">
                <span className="text-xs text-gray-500">{Math.round(tf.confidence * 100)}%</span>
                {tf.agrees_with_consensus
                  ? <span className="text-[10px] text-green-400">✓ agree</span>
                  : <span className="text-[10px] text-gray-600">diverge</span>
                }
              </div>
            </div>
          )
        })}
      </div>

      {/* First agreeing TF reasons */}
      {(() => {
        const agreeing = data.timeframes.find(t => t.agrees_with_consensus && t.reasons?.length)
        if (!agreeing) return null
        return (
          <div>
            <p className="text-xs text-gray-600 mb-1">Reasons ({agreeing.timeframe})</p>
            <ul className="space-y-0.5">
              {agreeing.reasons.slice(0, 4).map((r, i) => (
                <li key={i} className="text-xs text-gray-500">· {r}</li>
              ))}
            </ul>
          </div>
        )
      })()}
    </div>
  )
}

// ── Page ─────────────────────────────────────────────────────────────────────
const BROKERS = ['binance', 'alpaca', 'ibkr']
const TF_OPTIONS = ['1m', '3m', '5m', '15m', '30m', '1h', '2h', '4h', '6h', '12h', '1d', '1w']
const DEFAULT_SYMBOLS: Record<string, string> = {
  binance: 'BTC/USDT',
  alpaca:  'SPY',
  ibkr:    'SPY',
}

export default function MultiTimeframe() {
  const { brokerStrategies, allStrategies } = useStrategyRegistry()
  const [broker, setBroker]             = useState('binance')
  const [symbol, setSymbol]             = useState('BTC/USDT')
  const [strategyType, setStrategyType] = useState('hybrid_macd_rsi')

  // When broker changes, ensure selected strategy is still valid for that broker
  useEffect(() => {
    const valid = brokerStrategies[broker] ?? allStrategies
    if (!valid.includes(strategyType)) setStrategyType(valid[0] ?? strategyType)
  }, [broker]) // eslint-disable-line react-hooks/exhaustive-deps
  const [timeframes, setTimeframes]     = useState<string[]>(['1h', '4h', '1d'])
  const [symbols, setSymbols]           = useState('BTC/USDT')   // batch input
  const [mode, setMode]                 = useState<'single' | 'batch'>('single')
  const [loading, setLoading]           = useState(false)
  const [results, setResults]           = useState<ConfluenceData[]>([])

  const toggleTf = (tf: string) => {
    setTimeframes(prev =>
      prev.includes(tf) ? prev.filter(t => t !== tf) : [...prev, tf]
    )
  }

  const runAnalysis = async () => {
    if (timeframes.length === 0) { toast.error('Select at least one timeframe'); return }
    setLoading(true)
    setResults([])
    try {
      if (mode === 'single') {
        const r = await axios.get('/api/confluence', {
          params: { symbol, broker, strategy_type: strategyType, timeframes: timeframes.join(',') },
        })
        setResults([r.data])
      } else {
        const symList = symbols.split(',').map(s => s.trim()).filter(Boolean)
        if (symList.length === 0) { toast.error('Enter at least one symbol'); setLoading(false); return }
        const r = await axios.get('/api/confluence/batch', {
          params: { symbols: symList.join(','), broker, strategy_type: strategyType, timeframes: timeframes.join(',') },
        })
        setResults(r.data.results || [])
      }
    } catch (e: any) {
      toast.error(e?.response?.data?.detail || 'Analysis failed')
    }
    setLoading(false)
  }

  return (
    <div className="p-6 space-y-6">
      {/* Header */}
      <div>
        <div className="flex items-center gap-2 mb-1">
          <GitBranch size={16} className="text-brand-400" />
          <h1 className="text-lg font-bold text-white">Multi-Timeframe Confluence</h1>
        </div>
        <p className="text-xs text-gray-500">
          Run the same strategy across multiple timeframes — only act when multiple timeframes agree.
        </p>
      </div>

      {/* Controls */}
      <div className="bg-dark-800 border border-dark-600 rounded-xl p-5 space-y-4">
        {/* Mode toggle */}
        <div className="flex items-center gap-3">
          <button
            onClick={() => setMode('single')}
            className={`text-xs px-3 py-1.5 rounded-lg transition-all ${mode === 'single' ? 'bg-brand-500 text-black font-semibold' : 'bg-dark-700 text-gray-400 hover:text-white'}`}
          >Single Symbol</button>
          <button
            onClick={() => setMode('batch')}
            className={`text-xs px-3 py-1.5 rounded-lg transition-all ${mode === 'batch' ? 'bg-brand-500 text-black font-semibold' : 'bg-dark-700 text-gray-400 hover:text-white'}`}
          >Batch (up to 10)</button>
        </div>

        <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
          {/* Broker */}
          <div>
            <label className="text-xs text-gray-500 block mb-1">Broker</label>
            <select
              value={broker}
              onChange={e => { setBroker(e.target.value); setSymbol(DEFAULT_SYMBOLS[e.target.value] || '') }}
              className="w-full bg-dark-700 border border-dark-500 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-brand-500"
            >
              {BROKERS.map(b => <option key={b} value={b}>{b}</option>)}
            </select>
          </div>

          {/* Strategy */}
          <div>
            <label className="text-xs text-gray-500 block mb-1">Strategy</label>
            <select
              value={strategyType}
              onChange={e => setStrategyType(e.target.value)}
              className="w-full bg-dark-700 border border-dark-500 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-brand-500"
            >
              {(brokerStrategies[broker] ?? allStrategies).map(t => <option key={t} value={t}>{t}</option>)}
            </select>
          </div>

          {/* Symbol or symbols */}
          <div className="col-span-2">
            <label className="text-xs text-gray-500 block mb-1">
              {mode === 'single' ? 'Symbol' : 'Symbols (comma-separated)'}
            </label>
            {mode === 'single' ? (
              <input
                value={symbol}
                onChange={e => setSymbol(e.target.value.toUpperCase())}
                placeholder="BTC/USDT"
                className="w-full bg-dark-700 border border-dark-500 rounded-lg px-3 py-2 text-sm text-white placeholder-gray-600 focus:outline-none focus:border-brand-500"
              />
            ) : (
              <input
                value={symbols}
                onChange={e => setSymbols(e.target.value.toUpperCase())}
                placeholder="BTC/USDT, ETH/USDT, SOL/USDT"
                className="w-full bg-dark-700 border border-dark-500 rounded-lg px-3 py-2 text-sm text-white placeholder-gray-600 focus:outline-none focus:border-brand-500"
              />
            )}
          </div>
        </div>

        {/* Timeframe selector */}
        <div>
          <label className="text-xs text-gray-500 block mb-2">Timeframes to compare</label>
          <div className="flex gap-2 flex-wrap">
            {TF_OPTIONS.map(tf => (
              <button
                key={tf}
                onClick={() => toggleTf(tf)}
                className={`text-xs px-3 py-1.5 rounded-lg border transition-all ${
                  timeframes.includes(tf)
                    ? 'border-brand-500 bg-brand-500/15 text-brand-400 font-semibold'
                    : 'border-dark-500 bg-dark-700 text-gray-500 hover:text-gray-300'
                }`}
              >
                {tf}
              </button>
            ))}
          </div>
        </div>

        <button
          onClick={runAnalysis}
          disabled={loading}
          className="flex items-center gap-2 px-5 py-2.5 bg-brand-500 hover:bg-green-400 text-black text-sm font-semibold rounded-lg transition-all disabled:opacity-50"
        >
          <RefreshCw size={13} className={loading ? 'animate-spin' : ''} />
          {loading ? 'Running…' : 'Run Confluence Analysis'}
        </button>
      </div>

      {/* Results */}
      {results.length > 0 && (
        <div>
          <p className="text-xs text-gray-500 mb-3">{results.length} result{results.length > 1 ? 's' : ''}</p>
          <div className="grid gap-4 lg:grid-cols-2 xl:grid-cols-3">
            {results.map((r, i) => <ConfluenceCard key={i} data={r} />)}
          </div>
        </div>
      )}

      {results.length === 0 && !loading && (
        <div className="bg-dark-800 border border-dark-600 rounded-xl p-8 text-center">
          <GitBranch size={32} className="text-gray-600 mx-auto mb-3" />
          <p className="text-sm text-gray-500">Configure the analysis above and click Run to see multi-timeframe confluence.</p>
          <p className="text-xs text-gray-700 mt-2">
            A BUY on 1h is stronger when 4h and 1d also show BUY. Act only on high-confluence signals.
          </p>
        </div>
      )}
    </div>
  )
}
