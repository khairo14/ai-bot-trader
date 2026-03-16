import { useState } from 'react'
import { TrendingUp, TrendingDown, Wifi, WifiOff, Zap } from 'lucide-react'
import { useWebSocket } from '../hooks/useWebSocket'

interface ScalpSignal {
  id: number | null
  symbol: string
  signal: string
  entry_price: number
  stop_loss: number | null
  take_profit: number | null
  confidence: number
  timeframe: string
  strategy_name: string
  reasons: string[]
  spread_pct: number
  source: string
  _received_at: number   // local timestamp, injected on arrival
}

const WS_URL = (() => {
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${proto}//${window.location.host}/ws/scalping`
})()

const MAX_SIGNALS = 50

function fmtPrice(v: number | null): string {
  if (v == null || v === 0) return '—'
  const abs = Math.abs(v)
  if (abs >= 1000) return v.toFixed(2)
  if (abs >= 10)   return v.toFixed(3)
  if (abs >= 0.1)  return v.toFixed(5)
  return v.toFixed(6)
}

export default function ScalpSignalFeed() {
  const [signals, setSignals] = useState<ScalpSignal[]>([])

  const { connected } = useWebSocket(WS_URL, {
    onMessage: (raw) => {
      const msg = raw as { type?: string; data?: unknown }
      if (msg?.type === 'scalp_signal' && msg.data) {
        setSignals(prev =>
          [{ ...(msg.data as unknown as ScalpSignal), _received_at: Date.now() }, ...prev].slice(0, MAX_SIGNALS)
        )
      }
    },
  })

  return (
    <div className="space-y-2">
      {/* Connection indicator */}
      <div className="flex items-center gap-1.5 text-xs">
        {connected
          ? <><Wifi size={11} className="text-green-400" /><span className="text-green-400">Connected</span></>
          : <><WifiOff size={11} className="text-gray-500" /><span className="text-gray-500">Reconnecting…</span></>
        }
        {signals.length > 0 && (
          <span className="text-gray-600 ml-2">{signals.length} signal{signals.length !== 1 ? 's' : ''} received</span>
        )}
      </div>

      {signals.length === 0 ? (
        <div className="bg-dark-800 border border-dark-600 rounded-xl p-6 text-center">
          <Zap size={24} className="text-gray-600 mx-auto mb-2 opacity-40" />
          <p className="text-gray-500 text-xs">Waiting for scalp signals…</p>
          {!connected && <p className="text-gray-600 text-xs mt-1">Connecting to stream…</p>}
        </div>
      ) : (
        <div className="space-y-2">
          {signals.map((sig, i) => {
            const isBull = sig.signal === 'BUY' || sig.signal === 'COVER'
            return (
              <div
                key={`${sig.id ?? i}-${sig._received_at}`}
                className={`bg-dark-800 border rounded-xl p-3 flex items-center gap-3 ${
                  isBull ? 'border-green-900/40' : 'border-red-900/40'
                }`}
              >
                <div className={`p-1.5 rounded-lg flex-shrink-0 ${isBull ? 'bg-green-900/30' : 'bg-red-900/30'}`}>
                  {isBull
                    ? <TrendingUp size={14} className="text-green-400" />
                    : <TrendingDown size={14} className="text-red-400" />
                  }
                </div>

                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className={`text-xs font-bold px-1.5 py-0.5 rounded ${
                      isBull ? 'bg-green-900/30 text-green-400' : 'bg-red-900/30 text-red-400'
                    }`}>{sig.signal}</span>
                    <span className="text-sm font-semibold text-white">{sig.symbol}</span>
                    <span className="text-xs text-gray-500">{sig.timeframe}</span>
                    <span className="text-xs text-gray-600 truncate max-w-[120px]" title={sig.strategy_name}>
                      {sig.strategy_name}
                    </span>
                  </div>
                  <div className="flex items-center gap-3 mt-1 text-xs text-gray-400 flex-wrap">
                    <span>Entry <span className="text-white">{fmtPrice(sig.entry_price)}</span></span>
                    {sig.stop_loss  != null && <span>SL <span className="text-red-400">{fmtPrice(sig.stop_loss)}</span></span>}
                    {sig.take_profit != null && <span>TP <span className="text-green-400">{fmtPrice(sig.take_profit)}</span></span>}
                    <span>Conf <span className="text-white">{((sig.confidence || 0) * 100).toFixed(0)}%</span></span>
                    {sig.spread_pct > 0 && (
                      <span>Spread <span className="text-gray-300">{(sig.spread_pct * 100).toFixed(3)}%</span></span>
                    )}
                  </div>
                  {sig.reasons?.length > 0 && (
                    <p className="text-[10px] text-gray-600 mt-1 truncate" title={sig.reasons.join(' · ')}>
                      {sig.reasons.slice(0, 3).join(' · ')}
                    </p>
                  )}
                </div>

                <div className="flex flex-col items-end gap-1 flex-shrink-0">
                  <span className="text-[10px] text-gray-600">
                    {new Date(sig._received_at).toLocaleTimeString()}
                  </span>
                  <span className={`text-[10px] px-1.5 py-0.5 rounded ${
                    sig.source === 'ws_stream'
                      ? 'bg-purple-900/30 text-purple-400'
                      : 'bg-dark-600 text-gray-500'
                  }`}>
                    {sig.source === 'ws_stream' ? 'stream' : 'celery'}
                  </span>
                </div>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
