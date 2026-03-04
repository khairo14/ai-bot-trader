import { TrendingUp, TrendingDown, Minus, List, Clock } from 'lucide-react'
import axios from 'axios'
import toast from 'react-hot-toast'

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
  broker: string
  reasons: string
  created_at: string
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

export default function SignalCard({ signal }: Props) {
  const cfg = SIGNAL_CONFIG[signal.signal] ?? SIGNAL_CONFIG.HOLD
  const { Icon } = cfg

  const rr = signal.stop_loss && signal.take_profit && signal.entry_price
    ? Math.abs((signal.take_profit - signal.entry_price) / (signal.entry_price - signal.stop_loss))
    : null

  const reasons: string[] = (() => {
    try { return JSON.parse(signal.reasons || '[]') } catch { return signal.reasons ? [signal.reasons] : [] }
  })()

  const executeSignal = async () => {
    try {
      await axios.post('/api/signals/execute', { signal_id: signal.id })
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
          <p className="text-xs text-gray-600 mt-1 flex items-center gap-1 justify-end">
            <Clock size={10} />
            {new Date(signal.created_at).toLocaleTimeString()}
          </p>
        </div>
      </div>

      {/* Price Levels */}
      <div className="grid grid-cols-3 gap-2 text-xs">
        <div className="bg-dark-700 rounded-lg p-2 text-center">
          <p className="text-gray-500 mb-0.5">Entry</p>
          <p className="text-white font-mono font-medium">{signal.entry_price?.toFixed(4) ?? '—'}</p>
        </div>
        <div className="bg-dark-700 rounded-lg p-2 text-center">
          <p className="text-gray-500 mb-0.5">Stop Loss</p>
          <p className="text-red-400 font-mono font-medium">{signal.stop_loss?.toFixed(4) ?? '—'}</p>
        </div>
        <div className="bg-dark-700 rounded-lg p-2 text-center">
          <p className="text-gray-500 mb-0.5">Take Profit</p>
          <p className="text-green-400 font-mono font-medium">{signal.take_profit?.toFixed(4) ?? '—'}</p>
        </div>
      </div>

      {/* Confidence Bar */}
      <div>
        <div className="flex justify-between text-xs text-gray-500 mb-1">
          <span>Confidence</span>
          <span>{(signal.confidence * 100).toFixed(0)}%{rr ? ` · R:R ${rr.toFixed(2)}` : ''}</span>
        </div>
        <div className="h-1.5 bg-dark-700 rounded-full overflow-hidden">
          <div
            className="h-full rounded-full transition-all duration-500"
            style={{
              width: `${signal.confidence * 100}%`,
              background: signal.confidence >= 0.75
                ? '#4ade80'
                : signal.confidence >= 0.5 ? '#fbbf24' : '#f87171',
            }}
          />
        </div>
      </div>

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
        <button
          onClick={executeSignal}
          className={`w-full py-1.5 text-xs font-semibold rounded-lg transition-all ${cfg.bg} ${cfg.color} border ${cfg.border} hover:opacity-80`}
        >
          Execute Signal
        </button>
      )}
    </div>
  )
}
