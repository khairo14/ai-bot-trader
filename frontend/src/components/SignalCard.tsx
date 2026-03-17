import { useState } from 'react'
import { TrendingUp, TrendingDown, Minus, Clock, AlertTriangle, GitBranch } from 'lucide-react'
import axios from 'axios'
import toast from 'react-hot-toast'
import { parseUtc } from '../lib/dates'

interface OptionsLeg {
  action: string   // BUY | SELL
  right: string    // C | P
  strike: number
  premium: number
}

interface OptionsMeta {
  strategy_type?: string            // iron_condor | covered_call | bull_call_spread
  expiry?: string                   // YYYYMMDD
  legs?: OptionsLeg[]
  underlying_price?: number
  spread_width?: number
  max_profit?: number
  max_loss?: number
  strike?: number                   // single-leg
  right?: string
}

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
  strategy_type?: string
  asset_class?: string
  broker: string
  reasons: string[]
  created_at: string
  acted_on: boolean
  // Options fields (undefined for equity signals)
  iv_rank?: number
  delta?: number
  theta?: number
  vega?: number
  options_meta?: OptionsMeta
}

interface Props {
  signal: Signal
}

const SIGNAL_CONFIG: Record<string, { color: string; bg: string; border: string; Icon: React.ElementType }> = {
  BUY:   { color: 'text-green-400',  bg: 'bg-green-900/20',  border: 'border-green-900/40',  Icon: TrendingUp },
  SHORT: { color: 'text-red-400',    bg: 'bg-red-900/20',    border: 'border-red-900/40',    Icon: TrendingDown },
  SELL:  { color: 'text-orange-400', bg: 'bg-orange-900/20', border: 'border-orange-900/40', Icon: TrendingDown },
  COVER: { color: 'text-blue-400',   bg: 'bg-blue-900/20',   border: 'border-blue-900/40',   Icon: TrendingUp },
  HOLD:  { color: 'text-gray-400',   bg: 'bg-dark-700',      border: 'border-dark-600',      Icon: Minus },
}

const TF_DOT_COLORS: Record<string, string> = {
  BUY:   'bg-green-400',
  SELL:  'bg-orange-400',
  SHORT: 'bg-red-400',
  COVER: 'bg-blue-400',
  HOLD:  'bg-gray-600',
  MIXED: 'bg-yellow-400',
}

export default function SignalCard({ signal }: Props) {
  const cfg = SIGNAL_CONFIG[signal.signal] ?? SIGNAL_CONFIG.HOLD
  const { Icon } = cfg

  // Confluence mini-check (on demand, to avoid auto-firing N broker API calls)
  const [confluenceLoading, setConfluenceLoading] = useState(false)
  const [confluence, setConfluence] = useState<{ consensus: string; score: number; tfs: { timeframe: string; signal: string; agrees: boolean }[] } | null>(null)

  // Mirror _HIGHER_TF from signal_runner.py so the UI check uses the same
  // higher timeframes as the execution gate (primary TF + its two higher TFs).
  const HIGHER_TF: Record<string, string[]> = {
    '1m':  ['5m',  '15m'],
    '3m':  ['15m', '1h'],
    '5m':  ['15m', '1h'],
    '15m': ['1h',  '4h'],
    '30m': ['4h',  '1d'],
    '1h':  ['4h',  '1d'],
    '2h':  ['4h',  '1d'],
    '4h':  ['1d',  '1w'],
    '6h':  ['1d',  '1w'],
    '12h': ['1d',  '1w'],
    '1d':  [],
    '1w':  [],
  }
  const higherTfs = HIGHER_TF[signal.timeframe] ?? ['1h', '4h', '1d']
  const tfParam = [signal.timeframe, ...higherTfs].join(',')

  const checkConfluence = async () => {
    setConfluenceLoading(true)
    try {
      const r = await axios.get('/api/confluence', {
        params: {
          symbol: signal.symbol,
          broker: signal.broker,
          strategy_type: signal.strategy_type || signal.strategy_name,
          timeframes: tfParam,
        },
      })
      const data = r.data
      setConfluence({
        consensus: data.consensus,
        score: data.confluence_score,
        tfs: (data.timeframes || []).map((t: any) => ({
          timeframe: t.timeframe,
          signal: t.signal,
          agrees: t.agrees_with_consensus,
        })),
      })
    } catch {
      toast.error('Confluence check failed')
    }
    setConfluenceLoading(false)
  }

  const rr = signal.stop_loss && signal.take_profit && signal.entry_price
    ? Math.abs((signal.take_profit - signal.entry_price) / (signal.entry_price - signal.stop_loss))
    : null

  const reasons: string[] = Array.isArray(signal.reasons) ? signal.reasons : []

  // Staleness: warn if signal is older than 5 minutes (price levels are no longer reliable)
  // Ensure the timestamp is parsed as UTC — the backend stores naive ISO strings
  // (no 'Z' suffix). Without the appended 'Z', JS treats them as local time,
  // causing an 8-hour skew (e.g. UTC+8 shows every signal as 480m old).
  const _createdUtc = parseUtc(signal.created_at)
  const ageMs = Date.now() - (_createdUtc?.getTime() ?? Date.now())
  const ageMin = Math.floor(ageMs / 60000)
  const isStale = ageMs > 5 * 60 * 1000

  // Suppress the Execute button when upstream gates (confluence, market-hours) blocked
  // auto-execution. The risk manager gate is intentionally NOT blocked here — the user
  // can still attempt manual override and will receive a clear rejection message.
  const suppressionReason = reasons.find(r => r.startsWith('execution suppressed'))
  const isButtonDisabled = signal.acted_on || isStale || !!suppressionReason

  const executeSignal = async () => {
    if (signal.acted_on) {
      toast.error('This signal was already executed.')
      return
    }
    if (isStale) {
      toast.error(`Signal is ${ageMin}m old — price levels may be invalid. Run Now to get a fresh signal.`)
      return
    }
    if (suppressionReason) {
      toast.error(suppressionReason)
      return
    }
    try {
      await axios.post(`/api/forward-test/execute-signal/${signal.id}`)
      toast.success(`Order submitted for ${signal.symbol}`)
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || 'Execution failed')
    }
  }

  return (
    <div className={`bg-dark-800 border ${cfg.border} rounded-xl p-4 space-y-3`}>
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <div className={`${cfg.bg} ${cfg.color} p-1.5 rounded-lg`}>
            <Icon size={16} />
          </div>
          <div>
            <p className="text-sm font-semibold text-white">{signal.symbol}</p>
            <p className="text-xs text-gray-500">{signal.broker} · {signal.timeframe} · {signal.strategy_name}</p>
          </div>
        </div>
        <div className="text-right">
          <span className={`text-xs font-bold px-2 py-0.5 rounded ${cfg.bg} ${cfg.color} border ${cfg.border}`}>
            {signal.signal}
          </span>
          <p className="flex items-center justify-end gap-1 mt-1 text-xs text-gray-600">
            <Clock size={10} />
            {_createdUtc?.toLocaleTimeString()}
          </p>
        </div>
      </div>

      {/* Price Levels */}
      <div className="grid grid-cols-3 gap-2 text-xs">
        <div className="p-2 text-center rounded-lg bg-dark-700">
          <p className="text-gray-500 mb-0.5">Entry</p>
          <p className="font-mono font-medium text-white">{signal.entry_price?.toFixed(4) ?? '—'}</p>
        </div>
        <div className="p-2 text-center rounded-lg bg-dark-700">
          <p className="text-gray-500 mb-0.5">Stop Loss</p>
          <p className="font-mono font-medium text-red-400">{signal.stop_loss?.toFixed(4) ?? '—'}</p>
        </div>
        <div className="p-2 text-center rounded-lg bg-dark-700">
          <p className="text-gray-500 mb-0.5">Take Profit</p>
          <p className="font-mono font-medium text-green-400">{signal.take_profit?.toFixed(4) ?? '—'}</p>
        </div>
      </div>

      {/* Confidence Bar */}
      <div>
        <div className="flex justify-between mb-1 text-xs text-gray-500">
          <span>Confidence</span>
          <span>{((signal.confidence ?? 0) * 100).toFixed(0)}%{rr ? ` · R:R ${rr.toFixed(2)}` : ''}</span>
        </div>
        <div className="h-1.5 bg-dark-700 rounded-full overflow-hidden">
          <div
            className="h-full transition-all duration-500 rounded-full"
            style={{
              width: `${(signal.confidence ?? 0) * 100}%`,
              background: (signal.confidence ?? 0) >= 0.75
                ? '#4ade80'
                : (signal.confidence ?? 0) >= 0.5 ? '#fbbf24' : '#f87171',
            }}
          />
        </div>
      </div>

      {/* Options Section — shown for option asset_class */}
      {signal.asset_class === 'option' && (signal.iv_rank != null || signal.options_meta) && (
        <div className="p-3 space-y-2 border rounded-lg bg-dark-700 border-purple-900/30">
          {/* Strategy type badge + IV Rank */}
          <div className="flex items-center justify-between">
            {signal.options_meta?.strategy_type && (
              <span className="text-[10px] font-bold uppercase tracking-wider text-purple-300 bg-purple-900/30 border border-purple-900/50 px-2 py-0.5 rounded">
                {
                  signal.options_meta.strategy_type === 'iron_condor'    ? '🦅 Iron Condor' :
                  signal.options_meta.strategy_type === 'covered_call'   ? '📞 Covered Call' :
                  signal.options_meta.strategy_type === 'bull_call_spread' ? '📈 Bull Call Spread' :
                  signal.options_meta.strategy_type
                }
              </span>
            )}
            {signal.iv_rank != null && (
              <div className="flex items-center gap-1.5 ml-auto">
                <span className="text-[10px] text-gray-500">IV Rank</span>
                <div className="w-20 h-1.5 bg-dark-600 rounded-full overflow-hidden">
                  <div
                    className="h-full transition-all duration-500 bg-purple-500 rounded-full"
                    style={{ width: `${signal.iv_rank}%` }}
                  />
                </div>
                <span className="text-[10px] font-mono text-purple-300">{signal.iv_rank.toFixed(0)}%</span>
              </div>
            )}
          </div>

          {/* Greeks chips */}
          {(signal.delta != null || signal.theta != null || signal.vega != null) && (
            <div className="flex gap-1.5 flex-wrap">
              {signal.delta != null && (
                <span className="text-[10px] font-mono bg-blue-900/20 text-blue-300 border border-blue-900/40 px-1.5 py-0.5 rounded">
                  Δ {signal.delta > 0 ? '+' : ''}{signal.delta.toFixed(2)}
                </span>
              )}
              {signal.theta != null && (
                <span className="text-[10px] font-mono bg-amber-900/20 text-amber-300 border border-amber-900/40 px-1.5 py-0.5 rounded">
                  Θ {signal.theta > 0 ? '+' : ''}{signal.theta.toFixed(4)}/d
                </span>
              )}
              {signal.vega != null && (
                <span className="text-[10px] font-mono bg-teal-900/20 text-teal-300 border border-teal-900/40 px-1.5 py-0.5 rounded">
                  V {signal.vega > 0 ? '+' : ''}{signal.vega.toFixed(4)}
                </span>
              )}
            </div>
          )}

          {/* Legs summary */}
          {signal.options_meta?.legs && signal.options_meta.legs.length > 0 && (
            <div className="space-y-0.5">
              {signal.options_meta.legs.map((leg, i) => {
                const expiry = signal.options_meta?.expiry
                const expFmt = expiry ? `${expiry.slice(0,4)}-${expiry.slice(4,6)}-${expiry.slice(6)}` : '—'
                return (
                  <p key={i} className="text-[10px] font-mono text-gray-400">
                    <span className={leg.action === 'BUY' ? 'text-green-400' : 'text-orange-400'}>
                      {leg.action}
                    </span>
                    {' '}{leg.strike.toFixed(1)} {leg.right}
                    {' '}@ <span className="text-white">${leg.premium.toFixed(2)}</span>
                    {i === 0 && expiry ? <span className="text-gray-600"> · Exp: {expFmt}</span> : null}
                  </p>
                )
              })}
            </div>
          )}
        </div>
      )}

      {/* Reasons */}
      {reasons.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {reasons.map((r, i) => (
            <span key={i} className="text-xs bg-dark-700 text-gray-400 px-1.5 py-0.5 rounded border border-dark-600">
              {r}
            </span>
          ))}
        </div>
      )}

      {/* Execute Button (shown only for non-HOLD signals) */}
      {signal.signal !== 'HOLD' && (
        <div className="space-y-1.5">
          {isStale && (
            <div className="flex items-center gap-1.5 text-xs text-yellow-400">
              <AlertTriangle size={11} />
              <span>Signal is {ageMin}m old — price levels may be stale. Click "Run Now" for a fresh signal.</span>
            </div>
          )}
          <button
            onClick={executeSignal}
            disabled={isButtonDisabled}
            title={suppressionReason ?? (isStale ? `Signal is ${ageMin}m old` : undefined)}
            className={`w-full py-1.5 text-xs font-semibold rounded-lg transition-all border ${
              isButtonDisabled
                ? 'bg-dark-700 text-gray-500 border-dark-500 cursor-not-allowed opacity-50'
                : `${cfg.bg} ${cfg.color} ${cfg.border} hover:opacity-80`
            }`}
          >
            {isStale
              ? `Stale (${ageMin}m ago) — Run Now first`
              : suppressionReason
              ? `Suppressed — ${suppressionReason.replace('execution suppressed: ', '')}`
              : signal.acted_on ? 'Signal Executed' : 'Execute Signal'}
          </button>
        </div>
      )}

      {/* Confluence mini-check */}
      {signal.signal !== 'HOLD' && (
        <div className="pt-3 border-t border-dark-600">
          {confluence ? (
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-1.5">
                <GitBranch size={11} className="text-gray-500" />
                {confluence.tfs.map(t => (
                  <span
                    key={t.timeframe}
                    title={`${t.timeframe}: ${t.signal}`}
                    className={`inline-block w-2.5 h-2.5 rounded-full ${TF_DOT_COLORS[t.signal] ?? 'bg-gray-600'} ${t.agrees ? 'opacity-100' : 'opacity-40'}`}
                  />
                ))}
                <span className="ml-1 text-xs text-gray-500">
                  {confluence.tfs.filter(t => t.agrees).length}/{confluence.tfs.length} aligned
                </span>
                <span className={`text-xs font-semibold ${TF_DOT_COLORS[confluence.consensus] ? '' : 'text-gray-400'} ${
                  confluence.consensus === 'BUY' ? 'text-green-400' :
                  confluence.consensus === 'SELL' || confluence.consensus === 'SHORT' ? 'text-red-400' :
                  confluence.consensus === 'MIXED' ? 'text-yellow-400' : 'text-gray-400'
                }`}>
                  {confluence.consensus}
                </span>
              </div>
              <button onClick={checkConfluence} className="text-[10px] text-gray-600 hover:text-gray-400 transition-colors">
                refresh
              </button>
            </div>
          ) : (
            <button
              onClick={checkConfluence}
              disabled={confluenceLoading}
              className="flex items-center gap-1.5 text-xs text-gray-600 hover:text-gray-400 transition-colors w-full"
            >
              <GitBranch size={11} className={confluenceLoading ? 'animate-pulse text-brand-400' : ''} />
              {confluenceLoading ? `Checking ${[signal.timeframe, ...higherTfs].join(' · ')}…` : 'Check multi-TF confluence'}
            </button>
          )}
        </div>
      )}
    </div>
  )
}
