import { useState, useEffect, useCallback } from 'react'
import { ScanSearch, Play, Loader2, AlertTriangle, ChevronDown } from 'lucide-react'
import axios from 'axios'
import toast from 'react-hot-toast'

// ─── Types ────────────────────────────────────────────────────────────────────

interface ScanResult {
  symbol: string
  signal: string
  confidence: number
  entry_price: number | null
  stop_loss: number | null
  take_profit: number | null
  regime: string | null
  reasons: string[]
  error?: string
}

interface Watchlists {
  [key: string]: string[]
}

// Hardcoded fallback — matches STRATEGY_REGISTRY in signal_engine.py
const DEFAULT_STRATEGIES = ['hybrid_macd_rsi', 'momentum_breakout', 'mean_reversion_bb']

const BROKERS = [
  { value: 'binance', label: 'Binance (Crypto)' },
  { value: 'alpaca', label: 'Alpaca (Stocks)' },
  { value: 'ibkr', label: 'IBKR (Stocks/Options)' },
]

// Which watchlist keys are valid for each broker
const BROKER_WATCHLISTS: Record<string, string[]> = {
  binance: ['crypto_major', 'crypto_mid'],
  alpaca:  ['us_stocks', 'us_stocks_mid'],
  ibkr:    ['us_stocks', 'us_stocks_mid'],
}

const TIMEFRAMES = ['1m', '5m', '15m', '30m', '1h', '2h', '4h', '1d']

const WATCHLIST_LABELS: Record<string, string> = {
  crypto_major:  'Crypto — Major (BTC, ETH, SOL…)',
  crypto_mid:    'Crypto — Mid Cap (LINK, UNI, ARB…)',
  us_stocks:     'US Stocks — Large Cap (AAPL, NVDA…)',
  us_stocks_mid: 'US Stocks — Mid Cap (COIN, PLTR…)',
  custom:        'Custom symbols',
}

// ─── Sub-components ───────────────────────────────────────────────────────────

const ConfidenceBar = ({ value }: { value: number }) => {
  const pct = Math.round(value * 100)
  const color = pct >= 70 ? 'bg-green-500' : pct >= 50 ? 'bg-yellow-500' : 'bg-red-500'
  return (
    <div className="flex items-center gap-2 min-w-[80px]">
      <div className="flex-1 h-1.5 bg-dark-600 rounded-full overflow-hidden">
        <div className={`h-full rounded-full ${color}`} style={{ width: `${pct}%` }} />
      </div>
      <span className="text-xs text-gray-400 w-7 text-right">{pct}%</span>
    </div>
  )
}

const SignalBadge = ({ signal }: { signal: string }) => {
  if (signal === 'BUY')  return <span className="px-2 py-0.5 rounded-md bg-green-500/15 text-green-400 text-xs font-semibold">▲ BUY</span>
  if (signal === 'SELL') return <span className="px-2 py-0.5 rounded-md bg-red-500/15 text-red-400 text-xs font-semibold">▼ SELL</span>
  if (signal === 'SHORT') return <span className="px-2 py-0.5 rounded-md bg-orange-500/15 text-orange-400 text-xs font-semibold">↓ SHORT</span>
  if (signal === 'HOLD') return <span className="px-2 py-0.5 rounded-md bg-gray-500/15 text-gray-400 text-xs font-semibold">— HOLD</span>
  return <span className="px-2 py-0.5 rounded-md bg-dark-600 text-gray-500 text-xs font-semibold">{signal}</span>
}

const fmtPrice = (n: number | null) =>
  n == null ? '—' : n >= 1 ? `$${n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 4 })}` : `$${n.toFixed(6)}`

// ─── Main component ───────────────────────────────────────────────────────────

export default function MarketScanner() {
  const [strategies, setStrategies] = useState<string[]>(DEFAULT_STRATEGIES)
  const [watchlists, setWatchlists] = useState<Watchlists>({})
  const [form, setForm] = useState({
    strategy: 'momentum_breakout',
    broker: 'binance',
    timeframe: '1h',
    watchlist: 'crypto_major',
    customSymbols: '',
  })
  const [results, setResults] = useState<ScanResult[]>([])
  const [errors, setErrors] = useState<ScanResult[]>([])
  const [scanning, setScanning] = useState(false)
  const [scannedCount, setScannedCount] = useState<number | null>(null)
  const [expandedRow, setExpandedRow] = useState<string | null>(null)
  const [filterSignal, setFilterSignal] = useState<string>('all')

  // Load strategies + watchlists on mount
  useEffect(() => {
    Promise.allSettled([
      axios.get('/api/scanner/strategies'),
      axios.get('/api/scanner/watchlists'),
    ]).then(([strRes, wlRes]) => {
      if (strRes.status === 'fulfilled' && strRes.value.data.strategies?.length)
        setStrategies(strRes.value.data.strategies)
      if (wlRes.status === 'fulfilled')  setWatchlists(wlRes.value.data.watchlists ?? {})
    })
  }, [])

  const set = (k: keyof typeof form, v: string) => setForm(f => ({ ...f, [k]: v }))

  // When broker changes, reset watchlist to first valid one for that broker
  const setBroker = (broker: string) => {
    const validWatchlists = BROKER_WATCHLISTS[broker] ?? Object.keys(watchlists)
    const firstValid = validWatchlists[0] ?? 'custom'
    setForm(f => ({ ...f, broker, watchlist: firstValid }))
  }

  // Watchlist keys valid for currently selected broker
  const validWatchlistKeys = BROKER_WATCHLISTS[form.broker] ?? Object.keys(watchlists)

  const runScan = useCallback(async () => {
    setScanning(true)
    setResults([])
    setErrors([])
    setScannedCount(null)
    setExpandedRow(null)

    // Build symbol list
    let symbols: string[] | undefined
    if (form.watchlist === 'custom') {
      symbols = form.customSymbols
        .split(/[\n,]+/)
        .map(s => s.trim().toUpperCase())
        .filter(Boolean)
      if (!symbols.length) {
        toast.error('Enter at least one symbol.')
        setScanning(false)
        return
      }
    }

    try {
      const res = await axios.post('/api/scanner/scan', {
        strategy: form.strategy,
        broker: form.broker,
        timeframe: form.timeframe,
        watchlist: form.watchlist !== 'custom' ? form.watchlist : undefined,
        symbols,
        include_hold: false,
      })

      setResults(res.data.results ?? [])
      setErrors(res.data.errors ?? [])
      setScannedCount(res.data.scanned ?? 0)

      const hits = (res.data.results ?? []).length
      if (hits === 0) {
        toast('No signals found — all symbols returned HOLD.', { icon: '🔍' })
      } else {
        toast.success(`${hits} signal${hits !== 1 ? 's' : ''} found across ${res.data.scanned} symbols`)
      }
    } catch (e: any) {
      toast.error(e?.response?.data?.detail ?? 'Scan failed')
    } finally {
      setScanning(false)
    }
  }, [form])

  const filteredResults = filterSignal === 'all'
    ? results
    : results.filter(r => r.signal === filterSignal)

  const signalCounts = results.reduce<Record<string, number>>((acc, r) => {
    acc[r.signal] = (acc[r.signal] ?? 0) + 1
    return acc
  }, {})

  return (
    <div className="p-6 space-y-6 max-w-6xl mx-auto">
      {/* Header */}
      <div>
        <div className="flex items-center gap-2">
          <ScanSearch size={20} className="text-brand-400" />
          <h1 className="text-xl font-bold text-white">Market Scanner</h1>
        </div>
        <p className="text-sm text-gray-500 mt-0.5">
          Run a strategy across an entire watchlist in parallel — find the strongest signals instantly.
        </p>
      </div>

      {/* Config panel */}
      <div className="bg-dark-800 border border-dark-600 rounded-xl p-5">
        <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
          {/* Strategy */}
          <div>
            <label className="block text-xs text-gray-500 mb-1.5 font-medium">Strategy</label>
            <div className="relative">
              <select
                value={form.strategy}
                onChange={e => set('strategy', e.target.value)}
                className="w-full appearance-none bg-dark-700 border border-dark-500 text-white text-sm rounded-lg px-3 py-2 pr-8 focus:outline-none focus:border-brand-500"
              >
                {strategies.map(s => (
                  <option key={s} value={s}>{s}</option>
                ))}
              </select>
              <ChevronDown size={12} className="absolute right-2.5 top-1/2 -translate-y-1/2 text-gray-500 pointer-events-none" />
            </div>
          </div>

          {/* Broker */}
          <div>
            <label className="block text-xs text-gray-500 mb-1.5 font-medium">Broker</label>
            <div className="relative">
              <select
                value={form.broker}
                onChange={e => setBroker(e.target.value)}
                className="w-full appearance-none bg-dark-700 border border-dark-500 text-white text-sm rounded-lg px-3 py-2 pr-8 focus:outline-none focus:border-brand-500"
              >
                {BROKERS.map(b => (
                  <option key={b.value} value={b.value}>{b.label}</option>
                ))}
              </select>
              <ChevronDown size={12} className="absolute right-2.5 top-1/2 -translate-y-1/2 text-gray-500 pointer-events-none" />
            </div>
          </div>

          {/* Timeframe */}
          <div>
            <label className="block text-xs text-gray-500 mb-1.5 font-medium">Timeframe</label>
            <div className="flex gap-1 flex-wrap">
              {TIMEFRAMES.map(tf => (
                <button
                  key={tf}
                  onClick={() => set('timeframe', tf)}
                  className={`px-2 py-1 rounded-md text-xs font-medium transition-all ${
                    form.timeframe === tf
                      ? 'bg-brand-500 text-white'
                      : 'bg-dark-700 text-gray-400 hover:bg-dark-600'
                  }`}
                >
                  {tf}
                </button>
              ))}
            </div>
          </div>

          {/* Watchlist */}
          <div>
            <label className="block text-xs text-gray-500 mb-1.5 font-medium">Watchlist</label>
            <div className="relative">
              <select
                value={form.watchlist}
                onChange={e => set('watchlist', e.target.value)}
                className="w-full appearance-none bg-dark-700 border border-dark-500 text-white text-sm rounded-lg px-3 py-2 pr-8 focus:outline-none focus:border-brand-500"
              >
                {validWatchlistKeys.filter(k => k in watchlists).map(k => (
                  <option key={k} value={k}>{WATCHLIST_LABELS[k] ?? k}</option>
                ))}
                <option value="custom">Custom symbols</option>
              </select>
              <ChevronDown size={12} className="absolute right-2.5 top-1/2 -translate-y-1/2 text-gray-500 pointer-events-none" />
            </div>
          </div>
        </div>

        {/* Custom symbols textarea */}
        {form.watchlist === 'custom' && (
          <div className="mt-4">
            <label className="block text-xs text-gray-500 mb-1.5 font-medium">
              Symbols <span className="text-gray-600">(comma or newline separated)</span>
            </label>
            <textarea
              value={form.customSymbols}
              onChange={e => set('customSymbols', e.target.value)}
              placeholder="BTC/USDT, ETH/USDT, SOL/USDT"
              rows={3}
              className="w-full bg-dark-700 border border-dark-500 text-white text-sm rounded-lg px-3 py-2 focus:outline-none focus:border-brand-500 resize-none font-mono"
            />
          </div>
        )}

        {/* Scan button */}
        <div className="mt-4 flex items-center justify-between">
          <p className="text-xs text-gray-600">
            {form.watchlist !== 'custom'
              ? `${watchlists[form.watchlist]?.length ?? '–'} symbols in watchlist`
              : `${form.customSymbols.split(/[\n,]+/).filter(s => s.trim()).length} custom symbols`
            }
          </p>
          <button
            onClick={runScan}
            disabled={scanning}
            className="flex items-center gap-2 px-5 py-2 rounded-lg bg-brand-500 hover:bg-brand-400 disabled:opacity-50 text-white text-sm font-medium transition-all"
          >
            {scanning
              ? <><Loader2 size={14} className="animate-spin" /> Scanning…</>
              : <><Play size={14} /> Run Scan</>
            }
          </button>
        </div>
      </div>

      {/* Results */}
      {(results.length > 0 || scannedCount !== null) && (
        <div className="bg-dark-800 border border-dark-600 rounded-xl p-5">
          {/* Summary row */}
          <div className="flex items-center justify-between mb-4 flex-wrap gap-3">
            <div className="flex items-center gap-3">
              <h2 className="text-sm font-semibold text-gray-300">Scan Results</h2>
              {scannedCount !== null && (
                <span className="text-xs text-gray-500">
                  {results.length} signal{results.length !== 1 ? 's' : ''} from {scannedCount} symbols
                </span>
              )}
              {errors.length > 0 && (
                <span className="text-xs text-orange-400 flex items-center gap-1">
                  <AlertTriangle size={10} /> {errors.length} error{errors.length !== 1 ? 's' : ''}
                </span>
              )}
            </div>

            {/* Signal filter pills */}
            <div className="flex gap-1.5">
              {(['all', ...Object.keys(signalCounts)] as string[]).map(s => (
                <button
                  key={s}
                  onClick={() => setFilterSignal(s)}
                  className={`px-2.5 py-1 rounded-lg text-xs font-medium transition-all ${
                    filterSignal === s
                      ? 'bg-brand-500/20 text-brand-400 border border-brand-500/40'
                      : 'bg-dark-700 text-gray-500 hover:text-gray-300'
                  }`}
                >
                  {s === 'all' ? `All (${results.length})` : `${s} (${signalCounts[s]})`}
                </button>
              ))}
            </div>
          </div>

          {filteredResults.length === 0 ? (
            <div className="text-center py-10 text-gray-600 text-sm">No signals match this filter.</div>
          ) : (
            <div className="space-y-2">
              {filteredResults.map((r) => {
                const isExpanded = expandedRow === r.symbol
                const rr = r.entry_price && r.stop_loss && r.take_profit
                  ? ((r.take_profit - r.entry_price) / (r.entry_price - r.stop_loss)).toFixed(2)
                  : null
                return (
                  <div
                    key={r.symbol}
                    className="bg-dark-700 border border-dark-500 rounded-xl overflow-hidden"
                  >
                    {/* Main row */}
                    <button
                      className="w-full px-4 py-3 flex items-center gap-4 text-left hover:bg-dark-600 transition-all"
                      onClick={() => setExpandedRow(isExpanded ? null : r.symbol)}
                    >
                      {/* Signal */}
                      <div className="w-20 shrink-0"><SignalBadge signal={r.signal} /></div>

                      {/* Symbol */}
                      <div className="w-28 shrink-0">
                        <p className="text-sm font-semibold text-white">{r.symbol}</p>
                        {r.regime && <p className="text-[10px] text-gray-500 capitalize">{r.regime}</p>}
                      </div>

                      {/* Confidence bar */}
                      <div className="w-32 shrink-0">
                        <ConfidenceBar value={r.confidence} />
                      </div>

                      {/* Price data */}
                      <div className="flex-1 hidden sm:grid grid-cols-3 gap-2 text-right">
                        <div>
                          <p className="text-[10px] text-gray-600">Entry</p>
                          <p className="text-xs text-white">{fmtPrice(r.entry_price)}</p>
                        </div>
                        <div>
                          <p className="text-[10px] text-gray-600">Stop</p>
                          <p className="text-xs text-red-400">{fmtPrice(r.stop_loss)}</p>
                        </div>
                        <div>
                          <p className="text-[10px] text-gray-600">Target</p>
                          <p className="text-xs text-green-400">{fmtPrice(r.take_profit)}</p>
                        </div>
                      </div>

                      {/* R:R */}
                      {rr && (
                        <div className="text-right shrink-0">
                          <p className="text-[10px] text-gray-600">R:R</p>
                          <p className={`text-xs font-medium ${parseFloat(rr) >= 1.5 ? 'text-green-400' : 'text-yellow-400'}`}>{rr}x</p>
                        </div>
                      )}

                      {/* Chevron */}
                      <ChevronDown
                        size={14}
                        className={`text-gray-600 transition-transform shrink-0 ${isExpanded ? 'rotate-180' : ''}`}
                      />
                    </button>

                    {/* Expanded reasons */}
                    {isExpanded && r.reasons.length > 0 && (
                      <div className="px-4 pb-3 pt-1 border-t border-dark-600">
                        <p className="text-[10px] text-gray-600 mb-1.5 font-medium uppercase tracking-wider">Signal reasons</p>
                        <ul className="space-y-1">
                          {r.reasons.map((reason, i) => (
                            <li key={i} className="flex items-start gap-1.5 text-xs text-gray-400">
                              <span className="text-brand-400 mt-0.5 shrink-0">›</span>
                              {reason}
                            </li>
                          ))}
                        </ul>
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          )}

          {/* Errors section */}
          {errors.length > 0 && (
            <details className="mt-4">
              <summary className="text-xs text-orange-400 cursor-pointer select-none">
                {errors.length} symbol{errors.length !== 1 ? 's' : ''} failed to scan
              </summary>
              <div className="mt-2 space-y-1">
                {errors.map(e => (
                  <div key={e.symbol} className="flex items-center gap-2 text-xs p-2 bg-orange-900/10 border border-orange-900/30 rounded-lg">
                    <span className="text-orange-400 font-medium">{e.symbol}</span>
                    <span className="text-gray-500">{e.error}</span>
                  </div>
                ))}
              </div>
            </details>
          )}
        </div>
      )}

      {/* Empty state */}
      {results.length === 0 && scannedCount === null && !scanning && (
        <div className="flex flex-col items-center justify-center py-20 text-gray-600 gap-3">
          <ScanSearch size={44} className="opacity-20" />
          <p className="text-sm">Configure your scan and press <span className="text-gray-400">Run Scan</span></p>
          <p className="text-xs text-gray-700">All symbols are analyzed in parallel — results appear ranked by confidence</p>
        </div>
      )}
    </div>
  )
}
