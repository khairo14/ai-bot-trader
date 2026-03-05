/**
 * ChartPanel — a fully self-contained single chart panel.
 * Renders candles, indicators, and signal markers for one symbol/broker.
 * Used by Chart.tsx in 1 / 2 / 4-panel grid layouts.
 */
import { useEffect, useRef, useState, useCallback, useId } from 'react'
import {
  createChart,
  CandlestickSeries,
  HistogramSeries,
  LineSeries,
  createSeriesMarkers,
} from 'lightweight-charts'
import type { IChartApi, ISeriesApi } from 'lightweight-charts'
import axios from 'axios'
import toast from 'react-hot-toast'
import { RefreshCw } from 'lucide-react'

// ─── Indicator math ───────────────────────────────────────────────────────────
function computeEMA(closes: number[], period: number): (number | null)[] {
  const result: (number | null)[] = new Array(closes.length).fill(null)
  if (closes.length < period) return result
  const k = 2 / (period + 1)
  let ema = closes.slice(0, period).reduce((a, b) => a + b, 0) / period
  result[period - 1] = ema
  for (let i = period; i < closes.length; i++) { ema = closes[i] * k + ema * (1 - k); result[i] = ema }
  return result
}

function computeRSI(closes: number[], period = 14): (number | null)[] {
  const result: (number | null)[] = new Array(closes.length).fill(null)
  if (closes.length < period + 1) return result
  let gains = 0, losses = 0
  for (let i = 1; i <= period; i++) { const d = closes[i] - closes[i - 1]; if (d >= 0) gains += d; else losses -= d }
  let ag = gains / period, al = losses / period
  result[period] = al === 0 ? 100 : 100 - 100 / (1 + ag / al)
  for (let i = period + 1; i < closes.length; i++) {
    const d = closes[i] - closes[i - 1]
    ag = (ag * (period - 1) + (d > 0 ? d : 0)) / period
    al = (al * (period - 1) + (d < 0 ? -d : 0)) / period
    result[i] = al === 0 ? 100 : 100 - 100 / (1 + ag / al)
  }
  return result
}

interface MACDResult { macd: (number | null)[]; signal: (number | null)[]; hist: (number | null)[] }
function computeMACD(closes: number[], fast = 12, slow = 26, sig = 9): MACDResult {
  const emaFast = computeEMA(closes, fast)
  const emaSlow = computeEMA(closes, slow)
  const macd: (number | null)[] = closes.map((_, i) =>
    emaFast[i] !== null && emaSlow[i] !== null ? emaFast[i]! - emaSlow[i]! : null
  )
  const macdForEMA = macd.map(v => v ?? 0)
  const rawSig = computeEMA(macdForEMA, sig)
  const signal: (number | null)[] = macd.map((v, i) => v !== null && rawSig[i] !== null ? rawSig[i] : null)
  const hist: (number | null)[] = macd.map((v, i) =>
    v !== null && signal[i] !== null ? v - signal[i]! : null
  )
  return { macd, signal, hist }
}

interface BBResult { upper: (number | null)[]; mid: (number | null)[]; lower: (number | null)[] }
function computeBB(closes: number[], period = 20, mult = 2): BBResult {
  const upper: (number | null)[] = new Array(closes.length).fill(null)
  const mid: (number | null)[] = new Array(closes.length).fill(null)
  const lower: (number | null)[] = new Array(closes.length).fill(null)
  for (let i = period - 1; i < closes.length; i++) {
    const sl = closes.slice(i - period + 1, i + 1)
    const mean = sl.reduce((a, b) => a + b, 0) / period
    const std = Math.sqrt(sl.reduce((a, b) => a + (b - mean) ** 2, 0) / period)
    mid[i] = mean; upper[i] = mean + mult * std; lower[i] = mean - mult * std
  }
  return { upper, mid, lower }
}

// ─── Date range helpers ────────────────────────────────────────────────────────
const PRESET_MS: Record<string, number> = {
  '1W': 7*24*3600*1000, '1M': 30*24*3600*1000, '3M': 90*24*3600*1000,
  '6M': 180*24*3600*1000, '1Y': 365*24*3600*1000,
}
function presetToRange(preset: string): { since: number; until: number } {
  const until = Date.now()
  return { since: until - (PRESET_MS[preset] ?? PRESET_MS['1M']), until }
}
function toDateInput(ms: number) {
  return new Date(ms).toISOString().slice(0, 10) // 'YYYY-MM-DD'
}

// ─── Types ─────────────────────────────────────────────────────────────────────
interface Candle {
  time: number; open: number; high: number; low: number; close: number; volume: number
}
interface SignalMarker {
  time: number; signal: string; price: number; confidence: number | null
  strategy_name: string; stop_loss: number | null; take_profit: number | null
}

// ─── Constants ─────────────────────────────────────────────────────────────────
const BROKERS    = ['binance', 'alpaca', 'ibkr'] as const
const TIMEFRAMES = ['1m', '5m', '15m', '1h', '4h', '1d', '3d', '1w']
const PRESETS    = ['1W', '1M', '3M', '6M', '1Y', 'Custom'] as const
type Preset = typeof PRESETS[number]
const BROKER_DEFAULT: Record<string, string> = { binance: 'BTC/USDT', alpaca: 'AAPL', ibkr: 'SPY' }
const THEME = {
  bg: '#111111', text: '#9ca3af', grid: '#1f2937', border: '#374151',
  up: '#22c55e', down: '#ef4444',
}

// ─── Props ─────────────────────────────────────────────────────────────────────
interface ChartPanelProps {
  /** When true, toolbar is condensed — used in 4-panel grid */
  compact?: boolean
  /** Initial symbol override */
  defaultSymbol?: string
  /** Initial broker override */
  defaultBroker?: string
}

// ─── Component ─────────────────────────────────────────────────────────────────
export function ChartPanel({ compact = false, defaultSymbol, defaultBroker }: ChartPanelProps) {
  const initBroker = defaultBroker ?? 'binance'
  const initSymbol = defaultSymbol ?? BROKER_DEFAULT[initBroker]

  const [broker, setBroker]           = useState(initBroker)
  const [symbol, setSymbol]           = useState(initSymbol)
  const [timeframe, setTimeframe]     = useState('1h')
  const [rangePreset, setRangePreset] = useState<Preset>('1M')
  const [customFrom, setCustomFrom]   = useState(() => toDateInput(Date.now() - 30*24*3600*1000))
  const [customTo, setCustomTo]       = useState(() => toDateInput(Date.now()))
  const [loading, setLoading]         = useState(false)
  const [showEMA, setShowEMA]         = useState(true)
  const [showBB, setShowBB]           = useState(false)
  const [showVolume, setShowVolume]   = useState(true)
  const [subPanel, setSubPanel]       = useState<'RSI' | 'MACD'>('RSI')
  const [lastUpdated, setLastUpdated] = useState<string | null>(null)
  const [signalCount, setSignalCount] = useState(0)
  const [candleCount, setCandleCount] = useState(0)

  // Symbol combobox
  const [allSymbols, setAllSymbols] = useState<string[]>([])
  const [symQuery, setSymQuery]     = useState(initSymbol)
  const [symOpen, setSymOpen]       = useState(false)
  const symRef = useRef<HTMLDivElement>(null)
  const _id = useId()

  // DOM refs
  const containerRef = useRef<HTMLDivElement>(null)
  const mainRef      = useRef<HTMLDivElement>(null)
  const subRef       = useRef<HTMLDivElement>(null)

  // Chart instance refs
  const mainChart = useRef<IChartApi | null>(null)
  const subChart  = useRef<IChartApi | null>(null)

  // Series refs
  const candleRef  = useRef<ISeriesApi<'Candlestick'> | null>(null)
  const volRef     = useRef<ISeriesApi<'Histogram'>   | null>(null)
  const ema20Ref   = useRef<ISeriesApi<'Line'>        | null>(null)
  const ema50Ref   = useRef<ISeriesApi<'Line'>        | null>(null)
  const bbUpperRef = useRef<ISeriesApi<'Line'>        | null>(null)
  const bbMidRef   = useRef<ISeriesApi<'Line'>        | null>(null)
  const bbLowerRef = useRef<ISeriesApi<'Line'>        | null>(null)
  const rsiRef     = useRef<ISeriesApi<'Line'>        | null>(null)
  const macdLineRef = useRef<ISeriesApi<'Line'>       | null>(null)
  const macdSignRef = useRef<ISeriesApi<'Line'>       | null>(null)
  const macdHistRef = useRef<ISeriesApi<'Histogram'>  | null>(null)

  // ─── Init charts ────────────────────────────────────────────────────────────
  useEffect(() => {
    if (!mainRef.current || !subRef.current) return

    const mc = createChart(mainRef.current, {
      layout: { background: { color: THEME.bg }, textColor: THEME.text },
      grid: { vertLines: { color: THEME.grid }, horzLines: { color: THEME.grid } },
      crosshair: { mode: 1 },
      rightPriceScale: { borderColor: THEME.border },
      timeScale: { borderColor: THEME.border, timeVisible: true, secondsVisible: false },
      width: mainRef.current.clientWidth,
      height: mainRef.current.clientHeight,
    })
    mainChart.current = mc

    candleRef.current = mc.addSeries(CandlestickSeries, {
      upColor: THEME.up, downColor: THEME.down,
      borderUpColor: THEME.up, borderDownColor: THEME.down,
      wickUpColor: THEME.up, wickDownColor: THEME.down,
    })
    volRef.current = mc.addSeries(HistogramSeries, { priceFormat: { type: 'volume' }, priceScaleId: 'volume' })
    mc.priceScale('volume').applyOptions({ scaleMargins: { top: 0.85, bottom: 0 }, visible: false })

    ema20Ref.current = mc.addSeries(LineSeries, {
      color: '#f59e0b', lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
    })
    ema50Ref.current = mc.addSeries(LineSeries, {
      color: '#a78bfa', lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
    })
    bbUpperRef.current = mc.addSeries(LineSeries, {
      color: '#6366f155', lineWidth: 1, lineStyle: 2, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false, visible: false,
    })
    bbMidRef.current = mc.addSeries(LineSeries, {
      color: '#6366f1aa', lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false, visible: false,
    })
    bbLowerRef.current = mc.addSeries(LineSeries, {
      color: '#6366f155', lineWidth: 1, lineStyle: 2, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false, visible: false,
    })

    const sc = createChart(subRef.current, {
      layout: { background: { color: THEME.bg }, textColor: THEME.text },
      grid: { vertLines: { color: THEME.grid }, horzLines: { color: THEME.grid } },
      rightPriceScale: { borderColor: THEME.border },
      timeScale: { borderColor: THEME.border, timeVisible: true, secondsVisible: false, visible: false },
      width: subRef.current.clientWidth,
      height: subRef.current.clientHeight,
    })
    subChart.current = sc

    rsiRef.current = sc.addSeries(LineSeries, { color: '#38bdf8', lineWidth: 2, priceLineVisible: false })
    macdLineRef.current = sc.addSeries(LineSeries, { color: '#f59e0b', lineWidth: 1, priceLineVisible: false, visible: false })
    macdSignRef.current = sc.addSeries(LineSeries, { color: '#ec4899', lineWidth: 1, priceLineVisible: false, visible: false })
    macdHistRef.current = sc.addSeries(HistogramSeries, { priceLineVisible: false, visible: false })

    // Sync scroll/zoom between panels
    mc.timeScale().subscribeVisibleLogicalRangeChange(range => {
      if (range && subChart.current) subChart.current.timeScale().setVisibleLogicalRange(range)
    })
    sc.timeScale().subscribeVisibleLogicalRangeChange(range => {
      if (range && mainChart.current) mainChart.current.timeScale().setVisibleLogicalRange(range)
    })

    // ResizeObserver — responds to grid cell size changes (not just window resize)
    const ro = new ResizeObserver(() => {
      if (mainRef.current && mainChart.current)
        mainChart.current.applyOptions({ width: mainRef.current.clientWidth, height: mainRef.current.clientHeight })
      if (subRef.current && subChart.current)
        subChart.current.applyOptions({ width: subRef.current.clientWidth, height: subRef.current.clientHeight })
    })
    if (mainRef.current) ro.observe(mainRef.current)
    if (subRef.current)  ro.observe(subRef.current)

    return () => { ro.disconnect(); mc.remove(); sc.remove() }
  }, [])

  // ─── Toggle overlays ──────────────────────────────────────────────────────
  useEffect(() => { ema20Ref.current?.applyOptions({ visible: showEMA }); ema50Ref.current?.applyOptions({ visible: showEMA }) }, [showEMA])
  useEffect(() => {
    bbUpperRef.current?.applyOptions({ visible: showBB })
    bbMidRef.current?.applyOptions({ visible: showBB })
    bbLowerRef.current?.applyOptions({ visible: showBB })
  }, [showBB])
  useEffect(() => { volRef.current?.applyOptions({ visible: showVolume }) }, [showVolume])
  useEffect(() => {
    const isRSI = subPanel === 'RSI'
    rsiRef.current?.applyOptions({ visible: isRSI })
    macdLineRef.current?.applyOptions({ visible: !isRSI })
    macdSignRef.current?.applyOptions({ visible: !isRSI })
    macdHistRef.current?.applyOptions({ visible: !isRSI })
  }, [subPanel])

  // ─── Fetch & render ──────────────────────────────────────────────────────
  const fetchAndRender = useCallback(async () => {
    if (!candleRef.current) return
    setLoading(true)
    try {
      // Compute since/until ms from preset or custom date pickers
      let since: number, until: number
      if (rangePreset === 'Custom') {
        since = customFrom ? new Date(customFrom).getTime() : Date.now() - 30*24*3600*1000
        until = customTo   ? new Date(customTo + 'T23:59:59').getTime() : Date.now()
      } else {
        ;({ since, until } = presetToRange(rangePreset))
      }

      const [candleRes, signalRes] = await Promise.all([
        axios.get('/api/charts/candles', { params: { symbol, timeframe, broker, since, until } }),
        axios.get('/api/charts/signals', { params: { symbol, timeframe, broker, limit: 500 } }),
      ])

      const candles: Candle[] = (candleRes.data.candles as Candle[]).sort((a, b) => a.time - b.time)
      if (!candles.length) { toast.error('No candle data for this symbol.'); return }

      candleRef.current!.setData(
        candles.map(c => ({ time: c.time as any, open: c.open, high: c.high, low: c.low, close: c.close }))
      )
      volRef.current?.setData(
        candles.map(c => ({ time: c.time as any, value: c.volume, color: c.close >= c.open ? '#22c55e33' : '#ef444433' }))
      )

      const closes = candles.map(c => c.close)

      const ema20 = computeEMA(closes, 20)
      const ema50 = computeEMA(closes, 50)
      ema20Ref.current?.setData(candles.flatMap((c, i) => ema20[i] !== null ? [{ time: c.time as any, value: ema20[i]! }] : []))
      ema50Ref.current?.setData(candles.flatMap((c, i) => ema50[i] !== null ? [{ time: c.time as any, value: ema50[i]! }] : []))

      const bb = computeBB(closes)
      bbUpperRef.current?.setData(candles.flatMap((c, i) => bb.upper[i] !== null ? [{ time: c.time as any, value: bb.upper[i]! }] : []))
      bbMidRef.current?.setData(candles.flatMap((c, i) => bb.mid[i] !== null   ? [{ time: c.time as any, value: bb.mid[i]! }]   : []))
      bbLowerRef.current?.setData(candles.flatMap((c, i) => bb.lower[i] !== null ? [{ time: c.time as any, value: bb.lower[i]! }] : []))

      const rsi = computeRSI(closes)
      rsiRef.current?.setData(candles.flatMap((c, i) => rsi[i] !== null ? [{ time: c.time as any, value: rsi[i]! }] : []))

      const { macd, signal: macdSig, hist } = computeMACD(closes)
      macdLineRef.current?.setData(candles.flatMap((c, i) => macd[i] !== null ? [{ time: c.time as any, value: macd[i]! }] : []))
      macdSignRef.current?.setData(candles.flatMap((c, i) => macdSig[i] !== null ? [{ time: c.time as any, value: macdSig[i]! }] : []))
      macdHistRef.current?.setData(candles.flatMap((c, i) => hist[i] !== null ? [{
        time: c.time as any, value: hist[i]!, color: hist[i]! >= 0 ? '#22c55e66' : '#ef444466',
      }] : []))

      const markers: SignalMarker[] = signalRes.data.markers
      setSignalCount(markers.length)
      const minTime = candles[0].time, maxTime = candles[candles.length - 1].time
      const lwtMarkers = markers
        .filter(m => m.time >= minTime && m.time <= maxTime)
        .map(m => ({
          time: m.time as any,
          position: (m.signal === 'BUY' || m.signal === 'COVER' ? 'belowBar' : 'aboveBar') as any,
          color: m.signal === 'BUY' || m.signal === 'COVER' ? '#22c55e' : '#ef4444',
          shape: (m.signal === 'BUY' || m.signal === 'COVER' ? 'arrowUp' : 'arrowDown') as any,
          text: `${m.signal}${m.confidence ? ` ${Math.round(m.confidence * 100)}%` : ''}`,
          size: 1,
        }))
        .sort((a, b) => (a.time as number) - (b.time as number))
      createSeriesMarkers(candleRef.current!, lwtMarkers)

      mainChart.current?.timeScale().fitContent()
      subChart.current?.timeScale().fitContent()
      setCandleCount(candleRes.data.candle_count ?? candleRes.data.candles?.length ?? 0)
      setLastUpdated(new Date().toLocaleTimeString())
    } catch (err: any) {
      toast.error(err?.response?.data?.detail ?? 'Failed to load chart data.')
    } finally {
      setLoading(false)
    }
  }, [symbol, timeframe, broker, rangePreset, customFrom, customTo])

  useEffect(() => { fetchAndRender() }, [fetchAndRender])

  // Fetch symbols when broker changes
  useEffect(() => {
    setAllSymbols([])
    const def = BROKER_DEFAULT[broker] ?? ''
    setSymQuery(def); setSymbol(def)
    axios.get('/api/charts/symbols', { params: { broker } })
      .then(r => setAllSymbols(r.data.symbols ?? []))
      .catch(() => {})
  }, [broker])

  // Close symbol dropdown on outside click
  useEffect(() => {
    const h = (e: MouseEvent) => { if (symRef.current && !symRef.current.contains(e.target as Node)) setSymOpen(false) }
    document.addEventListener('mousedown', h)
    return () => document.removeEventListener('mousedown', h)
  }, [])

  const filteredSymbols = allSymbols.filter(s => s.toLowerCase().includes(symQuery.toLowerCase())).slice(0, 50)
  const selectSymbol = (s: string) => { setSymbol(s); setSymQuery(s); setSymOpen(false) }

  const sel = 'bg-dark-700 border border-dark-500 text-gray-200 text-xs rounded-lg px-2 py-1.5 focus:outline-none focus:ring-1 focus:ring-brand-500'

  return (
    <div ref={containerRef} className="flex flex-col h-full bg-[#111111] text-gray-100 border border-dark-700">

      {/* ─── Toolbar ──────────────────────────────────────────── */}
      <div className="flex flex-wrap items-center gap-1.5 px-2 py-1.5 border-b border-dark-600 bg-dark-800 shrink-0">

        {/* Broker */}
        <select value={broker} onChange={e => setBroker(e.target.value)} className={sel}>
          {BROKERS.map(b => <option key={b} value={b}>{b.charAt(0).toUpperCase() + b.slice(1)}</option>)}
        </select>

        {/* Symbol combobox */}
        <div ref={symRef} className="relative">
          <input id={_id} value={symQuery} autoComplete="off" placeholder="Symbol…"
            onFocus={() => setSymOpen(true)}
            onChange={e => { setSymQuery(e.target.value.toUpperCase()); setSymOpen(true) }}
            onKeyDown={e => {
              if (e.key === 'Enter' && filteredSymbols.length > 0) selectSymbol(filteredSymbols[0])
              if (e.key === 'Escape') setSymOpen(false)
            }}
            className={`${sel} w-24 uppercase`}
          />
          {symOpen && filteredSymbols.length > 0 && (
            <ul className="absolute z-50 top-full mt-1 left-0 w-40 max-h-56 overflow-y-auto bg-dark-800 border border-dark-600 rounded-lg shadow-xl text-xs text-gray-200">
              {filteredSymbols.map(s => (
                <li key={s} onMouseDown={() => selectSymbol(s)}
                  className={`px-3 py-1.5 cursor-pointer hover:bg-brand-500/20 hover:text-white ${s === symbol ? 'text-brand-400 font-medium' : ''}`}>
                  {s}
                </li>
              ))}
            </ul>
          )}
          {symOpen && allSymbols.length === 0 && (
            <div className="absolute z-50 top-full mt-1 left-0 w-36 bg-dark-800 border border-dark-600 rounded-lg px-3 py-2 text-xs text-gray-500">
              Loading…
            </div>
          )}
        </div>

        {/* Timeframe */}
        <select value={timeframe} onChange={e => setTimeframe(e.target.value)} className={sel}>
          {TIMEFRAMES.map(tf => <option key={tf} value={tf}>{tf}</option>)}
        </select>

        {/* Date range */}
        {!compact && (
          <div className="flex items-center gap-0">
            <div className="flex rounded-lg overflow-hidden border border-dark-500">
              {PRESETS.map(p => (
                <button key={p} onClick={() => setRangePreset(p)}
                  className={`text-xs px-1.5 py-1.5 transition-all ${
                    rangePreset === p
                      ? p === 'Custom' ? 'bg-violet-600 text-white' : 'bg-brand-500 text-white'
                      : 'bg-dark-700 text-gray-400 hover:text-gray-200 hover:bg-dark-600'
                  }`}>
                  {p}
                </button>
              ))}
            </div>
            {rangePreset === 'Custom' && (
              <div className="flex items-center gap-1 ml-1.5">
                <input type="date" value={customFrom} onChange={e => setCustomFrom(e.target.value)}
                  className={`${sel} w-32 [color-scheme:dark]`} />
                <span className="text-gray-600 text-xs">→</span>
                <input type="date" value={customTo} onChange={e => setCustomTo(e.target.value)}
                  className={`${sel} w-32 [color-scheme:dark]`} />
              </div>
            )}
          </div>
        )}
        {compact && (
          <select value={rangePreset} onChange={e => setRangePreset(e.target.value as Preset)} className={sel}>
            {PRESETS.map(p => <option key={p} value={p}>{p}</option>)}
          </select>
        )}

        {/* Refresh */}
        <button onClick={fetchAndRender} disabled={loading}
          className="flex items-center gap-1 text-xs px-2 py-1.5 rounded-lg bg-brand-500 hover:bg-brand-600 text-white disabled:opacity-50 transition-all">
          <RefreshCw size={11} className={loading ? 'animate-spin' : ''} />
          {!compact && (loading ? 'Loading…' : 'Refresh')}
        </button>

        {/* Indicator toggles */}
        <div className="flex items-center gap-2 border-l border-dark-600 pl-2">
          <label className="flex items-center gap-1 text-xs cursor-pointer select-none">
            <input type="checkbox" checked={showEMA} onChange={e => setShowEMA(e.target.checked)} className="accent-amber-400" />
            <span className="text-amber-400">EMA</span>
          </label>
          <label className="flex items-center gap-1 text-xs cursor-pointer select-none">
            <input type="checkbox" checked={showBB} onChange={e => setShowBB(e.target.checked)} className="accent-indigo-400" />
            <span className="text-indigo-400">BB</span>
          </label>
          <label className="flex items-center gap-1 text-xs text-gray-400 cursor-pointer select-none">
            <input type="checkbox" checked={showVolume} onChange={e => setShowVolume(e.target.checked)} />
            <span>Vol</span>
          </label>
        </div>

        {/* Sub-panel toggle */}
        <div className="flex items-center gap-1 border-l border-dark-600 pl-2">
          {(['RSI', 'MACD'] as const).map(ind => (
            <button key={ind} onClick={() => setSubPanel(ind)}
              className={`text-xs px-1.5 py-1 rounded-md transition-all ${subPanel === ind
                ? ind === 'RSI' ? 'bg-sky-500/20 text-sky-400 font-medium' : 'bg-yellow-500/20 text-yellow-400 font-medium'
                : 'text-gray-500 hover:text-gray-300'}`}>
              {ind}
            </button>
          ))}
        </div>

        {/* Status */}
        {!compact && (
          <div className="ml-auto flex items-center gap-2 text-xs text-gray-500">
            {signalCount > 0 && <span className="text-brand-400">{signalCount} signal{signalCount !== 1 ? 's' : ''}</span>}
            {candleCount > 0 && <span className="text-gray-600">{candleCount.toLocaleString()} candles</span>}
            {lastUpdated && <span>{lastUpdated}</span>}
          </div>
        )}
      </div>

      {/* ─── Legend ───────────────────────────────────────────── */}
      {!compact && (
        <div className="flex flex-wrap items-center gap-3 px-3 py-1 bg-dark-800/60 border-b border-dark-700 text-xs text-gray-500 shrink-0">
          <span className="flex items-center gap-1.5"><span className="w-2 h-2 rounded-full bg-green-500" /> BUY</span>
          <span className="flex items-center gap-1.5"><span className="w-2 h-2 rounded-full bg-red-500" /> SELL</span>
          <span className="flex items-center gap-1.5"><span className="w-4 h-px bg-amber-400" /> EMA 20</span>
          <span className="flex items-center gap-1.5"><span className="w-4 h-px bg-violet-400" /> EMA 50</span>
          {showBB && <span className="flex items-center gap-1.5"><span className="w-4 border-t-2 border-dashed border-indigo-400" /> BB(20,2)</span>}
          {subPanel === 'RSI' && <span className="flex items-center gap-1.5"><span className="w-4 h-px bg-sky-400" /> RSI 14</span>}
          {subPanel === 'MACD' && <>
            <span className="flex items-center gap-1.5"><span className="w-4 h-px bg-amber-400" /> MACD</span>
            <span className="flex items-center gap-1.5"><span className="w-4 h-px bg-pink-400" /> Signal</span>
          </>}
        </div>
      )}

      {/* ─── Charts ───────────────────────────────────────────── */}
      <div className="flex-1 flex flex-col min-h-0">
        <div ref={mainRef} className="flex-[3] min-h-0 w-full" />
        <div className="relative flex-1 min-h-0 border-t border-dark-700">
          <span className={`absolute top-1 left-2 text-[9px] font-medium z-10 pointer-events-none
            ${subPanel === 'RSI' ? 'text-sky-400' : 'text-yellow-400'}`}>
            {subPanel === 'RSI' ? 'RSI 14' : 'MACD (12,26,9)'}
          </span>
          <div ref={subRef} className="w-full h-full" />
        </div>
      </div>
    </div>
  )
}
