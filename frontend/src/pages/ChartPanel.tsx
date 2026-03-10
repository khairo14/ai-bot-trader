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
import { RefreshCw, LayoutGrid } from 'lucide-react'

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
  // Compute the signal EMA only over valid MACD values — seeding it from zeros
  // (via `macd.map(v => v ?? 0)`) causes the EMA warmup to run on zeroed data,
  // producing a signal line that lags many candles behind where it should start.
  const signal: (number | null)[] = new Array(closes.length).fill(null)
  const hist: (number | null)[] = new Array(closes.length).fill(null)
  const firstValid = macd.findIndex(v => v !== null)
  if (firstValid !== -1) {
    const validMacd = macd.slice(firstValid) as number[]
    const sigEMA = computeEMA(validMacd, sig)
    for (let i = 0; i < sigEMA.length; i++) {
      if (sigEMA[i] !== null) {
        signal[firstValid + i] = sigEMA[i]
        hist[firstValid + i] = macd[firstValid + i]! - sigEMA[i]!
      }
    }
  }
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

function computeMA(closes: number[], period: number): (number | null)[] {
  const result: (number | null)[] = new Array(closes.length).fill(null)
  for (let i = period - 1; i < closes.length; i++) {
    let sum = 0
    for (let j = i - period + 1; j <= i; j++) sum += closes[j]
    result[i] = sum / period
  }
  return result
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
interface TradeMarker {
  id: number; side: string; status: string; is_paper: boolean
  strategy_name: string | null
  entry_price: number | null; entry_time: number | null
  exit_price: number | null; exit_time: number | null
  stop_loss: number | null; take_profit: number | null
  pnl: number | null; pnl_pct: number | null; quantity: number
}

interface MAOverlay { id: string; period: number; color: string }

/** Returns the next NYSE open time as a human-readable ET string. */
// DST-safe helpers — never use the "parse locale string as local time" offset trick.
// Instead: find the target ET calendar date, then probe UTC candidates to find
// the one that actually lands on the desired ET wall-clock hour.
function _etHour(d: Date) {
  return +d.toLocaleTimeString('en-GB', { timeZone: 'America/New_York', hour: '2-digit', minute: '2-digit' }).slice(0, 2)
}
function _etDow(d: Date) {
  return ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'].indexOf(
    d.toLocaleDateString('en-US', { timeZone: 'America/New_York', weekday: 'short' })
  )
}
function _etDateStr(d: Date) {
  return d.toLocaleDateString('en-CA', { timeZone: 'America/New_York' }) // "YYYY-MM-DD"
}
/** Find the UTC Date that equals targetHour:targetMin on the given ET date string. */
function _utcForETTime(etDate: string, targetHour: number, targetMin: number): Date {
  for (const offsetH of [4, 5]) { // try EDT (-4) then EST (-5)
    const candidate = new Date(`${etDate}T${String(targetHour + offsetH).padStart(2,'0')}:${String(targetMin).padStart(2,'0')}:00Z`)
    if (_etHour(candidate) === targetHour) return candidate
  }
  return new Date(`${etDate}T${String(targetHour + 5).padStart(2,'0')}:${String(targetMin).padStart(2,'0')}:00Z`)
}

function nextNYSEOpen(): string {
  const now = new Date()
  let target = new Date(now)
  const h = _etHour(now)
  const etM = +now.toLocaleTimeString('en-GB', { timeZone: 'America/New_York', hour: '2-digit', minute: '2-digit' }).slice(3, 5)
  const dow = _etDow(now)
  const isWeekday = (d: number) => d >= 1 && d <= 5
  if (!(isWeekday(dow) && (h < 9 || (h === 9 && etM < 30)))) {
    do { target = new Date(target.getTime() + 86_400_000) } while (!isWeekday(_etDow(target)))
  }
  return _utcForETTime(_etDateStr(target), 9, 30).toLocaleString('en-US', {
    timeZone: 'America/New_York', weekday: 'short', month: 'short',
    day: 'numeric', hour: 'numeric', minute: '2-digit', hour12: true,
  }) + ' ET'
}

/** Next time the 24/5 FX market opens (Sunday 5 PM ET). */
function nextFXOpen(): string {
  const now = new Date()
  let target = new Date(now)
  const dow = _etDow(now), h = _etHour(now)
  if (!(dow === 0 && h < 17)) {
    do { target = new Date(target.getTime() + 86_400_000) } while (_etDow(target) !== 0)
  }
  return _utcForETTime(_etDateStr(target), 17, 0).toLocaleString('en-US', {
    timeZone: 'America/New_York', weekday: 'short', month: 'short',
    day: 'numeric', hour: 'numeric', minute: '2-digit', hour12: true,
  }) + ' ET'
}

/** True when symbol looks like a spot FX pair (EUR/USD, GBP/JPY, etc.) */
const isFxPair = (sym: string) => /^[A-Za-z]{3}\/[A-Za-z]{3}$/.test(sym)

// ─── Constants ─────────────────────────────────────────────────────────────────
const MA_COLORS  = ['#06b6d4', '#f97316', '#84cc16', '#ec4899', '#8b5cf6', '#14b8a6']
const BROKERS    = ['binance', 'alpaca', 'ibkr'] as const
const TIMEFRAMES = ['1m', '5m', '15m', '1h', '4h', '1d', '3d', '1w']
const PRESETS    = ['1W', '1M', '3M', '6M', '1Y', 'Custom'] as const
type Preset = typeof PRESETS[number]
const BROKER_DEFAULT: Record<string, string> = { binance: 'BTC/USDT', alpaca: 'AAPL', ibkr: 'SPY' }
const THEME = {
  bg: '#111111', text: '#9ca3af', grid: '#1f2937', border: '#374151',
  up: '#22c55e', down: '#ef4444',
}

// Milliseconds per timeframe — mirrors backend _TF_MS
const TF_TO_MS: Record<string, number> = {
  '1m': 60_000, '5m': 300_000, '15m': 900_000,
  '1h': 3_600_000, '4h': 14_400_000,
  '1d': 86_400_000, '3d': 259_200_000, '1w': 604_800_000,
}
// How often (seconds) to poll for the live candle update per timeframe
// Generous intervals — 4-panel grid uses 4 × these rates, so keep them low.
// WS connection skips REST polling entirely while it's live.
const TF_POLL_SECS: Record<string, number> = {
  '1m': 30, '5m': 60, '15m': 90, '1h': 120, '4h': 300, '1d': 600, '3d': 1800, '1w': 3600,
}

// ─── Strategy → indicator mapping ─────────────────────────────────────────────
// Maps strategy_type or strategy name keywords to default indicator states.
// Keyword matching is case-insensitive; direct strategy_type takes priority.
const STRATEGY_IND_MAP: Record<string, { showEMA: boolean; showBB: boolean; subPanel: 'RSI' | 'MACD' }> = {
  hybrid_macd_rsi:    { showEMA: true,  showBB: false, subPanel: 'MACD' },
  volatility_squeeze: { showEMA: false, showBB: true,  subPanel: 'MACD' },
  momentum:           { showEMA: true,  showBB: false, subPanel: 'RSI'  },
  mean_reversion:     { showEMA: false, showBB: true,  subPanel: 'RSI'  },
  covered_call:       { showEMA: true,  showBB: false, subPanel: 'RSI'  },
  bull_call_spread:   { showEMA: true,  showBB: false, subPanel: 'MACD' },
  iron_condor:        { showEMA: false, showBB: true,  subPanel: 'RSI'  },
}
function strategyIndicators(strategy?: string): { showEMA: boolean; showBB: boolean; subPanel: 'RSI' | 'MACD' } {
  if (!strategy) return { showEMA: true, showBB: false, subPanel: 'RSI' }
  const key = strategy.toLowerCase().replace(/\s+/g, '_')
  // Direct match on strategy_type
  if (STRATEGY_IND_MAP[key]) return STRATEGY_IND_MAP[key]
  // Keyword fallback on human-readable names (e.g. "BTC Trend Follower")
  if (key.includes('macd'))                          return { showEMA: true,  showBB: false, subPanel: 'MACD' }
  if (key.includes('squeeze') || key.includes('bb')) return { showEMA: false, showBB: true,  subPanel: 'MACD' }
  if (key.includes('reversi') || key.includes('mean')) return { showEMA: false, showBB: true, subPanel: 'RSI' }
  if (key.includes('swing'))                         return { showEMA: false, showBB: true,  subPanel: 'RSI'  }
  if (key.includes('trend') || key.includes('ema'))  return { showEMA: true,  showBB: false, subPanel: 'RSI'  }
  if (key.includes('momentum'))                      return { showEMA: true,  showBB: false, subPanel: 'RSI'  }
  return { showEMA: true, showBB: false, subPanel: 'RSI' }
}

// ─── Props ─────────────────────────────────────────────────────────────────────
interface ChartPanelProps {
  /** When true, toolbar is condensed — used in 4-panel grid */
  compact?: boolean
  /** Initial symbol override */
  defaultSymbol?: string
  /** Initial broker override */
  defaultBroker?: string
  /** Initial timeframe override (e.g. '4h') */
  defaultTimeframe?: string
  /** Strategy name or strategy_type — auto-enables matching indicators */
  strategy?: string
}

// ─── Component ─────────────────────────────────────────────────────────────────
export function ChartPanel({ compact = false, defaultSymbol, defaultBroker, defaultTimeframe, strategy }: ChartPanelProps) {
  const initBroker = defaultBroker ?? 'binance'
  const initSymbol = defaultSymbol ?? BROKER_DEFAULT[initBroker]
  const initInds   = strategyIndicators(strategy)

  const [broker, setBroker]           = useState(initBroker)
  const [symbol, setSymbol]           = useState(initSymbol)
  const [timeframe, setTimeframe]     = useState(defaultTimeframe ?? '1h')
  const [rangePreset, setRangePreset] = useState<Preset>('1M')
  const [customFrom, setCustomFrom]   = useState(() => toDateInput(Date.now() - 30*24*3600*1000))
  const [customTo, setCustomTo]       = useState(() => toDateInput(Date.now()))
  const [loading, setLoading]         = useState(false)
  const [loadingMsg, setLoadingMsg]   = useState('Loading…')
  const [showEMA, setShowEMA]         = useState(initInds.showEMA)
  const [showBB, setShowBB]           = useState(initInds.showBB)
  const [showVolume, setShowVolume]   = useState(true)
  const [subPanel, setSubPanel]       = useState<'RSI' | 'MACD'>(initInds.subPanel)
  const [lastUpdated, setLastUpdated] = useState<string | null>(null)
  const [signalCount, setSignalCount] = useState(0)
  const [tradeCount, setTradeCount]   = useState(0)
  const [candleCount, setCandleCount] = useState(0)
  const [showTrades, setShowTrades]   = useState(true)
  const [showSignals, setShowSignals] = useState(true)
  const [maOverlays, setMaOverlays]   = useState<MAOverlay[]>([])
  const [showAddMA, setShowAddMA]     = useState(false)
  const [newMAPeriod, setNewMAPeriod] = useState(20)
  const [wsLive, setWsLive]           = useState(false)
  const [marketClosed, setMarketClosed] = useState(false)
  const [wsRetryKey, setWsRetryKey]   = useState(0)   // increments to trigger WS reconnect
  const [showIndPanel, setShowIndPanel] = useState(false)

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

  // Price-line refs for trade TP/SL — cleaned up on each fetch
  const tradeLineRefs = useRef<any[]>([])

  // Live candle update refs
  const lastCandleTimeRef = useRef<number | null>(null)   // unix seconds of last known candle
  const livePollRef       = useRef<ReturnType<typeof setInterval> | null>(null)
  const isFetchingRef     = useRef(false)   // guard: don't live-update during full fetch
  const wsLiveRef         = useRef(false)   // true while WS is connected — disables REST poll
  // Tracks current symbol/timeframe/broker so stale live-poll responses (from
  // a previous symbol that resolved after a parameter change) are discarded.
  const paramsKeyRef      = useRef(`${broker}/${symbol}/${timeframe}`)

  // MA overlay refs
  const maSeriesRefs   = useRef<Map<string, ISeriesApi<'Line'>>>(new Map())
  const candlesDataRef = useRef<Candle[]>([])
  const maOverlaysRef  = useRef<MAOverlay[]>([])
  // Live WebSocket refs
  const wsRef       = useRef<WebSocket | null>(null)
  const wsRetryRef  = useRef<ReturnType<typeof setTimeout> | null>(null)
  const abortRef    = useRef<AbortController | null>(null)   // cancels in-flight fetchAndRender
  const indPanelRef = useRef<HTMLDivElement>(null)

  // Series refs
  const markersPluginRef = useRef<any>(null)   // lightweight-charts v5 marker plugin — reused via setMarkers()
  const cachedMarkersRef = useRef<{ signals: SignalMarker[]; trades: TradeMarker[]; minTime: number; maxTime: number } | null>(null)
  const showSignalsRef   = useRef(showSignals)   // ref-mirrors for stable applyMarkersImpl
  const showTradesRef    = useRef(showTrades)
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

    // Dynamic price formatter — adapts decimal places to the instrument's price magnitude.
    // Forex pairs like EUR/GBP (~0.87) need 4-5 dp; crypto like BTC (~50000) needs 2.
    const mainPriceFmt = {
      type: 'custom' as const,
      formatter: (price: number) => {
        const abs = Math.abs(price)
        if (abs >= 1000) return price.toFixed(2)
        if (abs >= 10)   return price.toFixed(3)
        if (abs >= 0.1)  return price.toFixed(4)
        if (abs >= 0.001) return price.toFixed(5)
        return price.toFixed(6)
      },
      minMove: 0.00001,
    }
    candleRef.current = mc.addSeries(CandlestickSeries, {
      upColor: THEME.up, downColor: THEME.down,
      borderUpColor: THEME.up, borderDownColor: THEME.down,
      wickUpColor: THEME.up, wickDownColor: THEME.down,
      priceFormat: mainPriceFmt,
    })
    // Create the marker plugin once — subsequent updates use .setMarkers() not createSeriesMarkers()
    markersPluginRef.current = createSeriesMarkers(candleRef.current, [])
    volRef.current = mc.addSeries(HistogramSeries, { priceFormat: { type: 'volume' }, priceScaleId: 'volume' })
    mc.priceScale('volume').applyOptions({ scaleMargins: { top: 0.85, bottom: 0 }, visible: false })

    ema20Ref.current = mc.addSeries(LineSeries, {
      color: '#f59e0b', lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false, priceFormat: mainPriceFmt,
    })
    ema50Ref.current = mc.addSeries(LineSeries, {
      color: '#a78bfa', lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false, priceFormat: mainPriceFmt,
    })
    bbUpperRef.current = mc.addSeries(LineSeries, {
      color: '#6366f155', lineWidth: 1, lineStyle: 2, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false, visible: false, priceFormat: mainPriceFmt,
    })
    bbMidRef.current = mc.addSeries(LineSeries, {
      color: '#6366f1aa', lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false, visible: false, priceFormat: mainPriceFmt,
    })
    bbLowerRef.current = mc.addSeries(LineSeries, {
      color: '#6366f155', lineWidth: 1, lineStyle: 2, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false, visible: false, priceFormat: mainPriceFmt,
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

    // MACD price format — uses enough decimal places for forex (tiny values like 0.00012)
    const macdPriceFmt = { type: 'custom' as const, formatter: (v: number) => {
      const abs = Math.abs(v)
      if (abs === 0) return '0.00'
      if (abs < 0.0001) return v.toFixed(6)
      if (abs < 0.01)   return v.toFixed(5)
      if (abs < 1)      return v.toFixed(4)
      return v.toFixed(2)
    }}
    // Apply same formatter globally to the sub-chart axis tick labels.
    // Without this, the RSI series (added first, no custom priceFormat) controls
    // the scale formatter, causing MACD values like -0.00030 to display as "-0.00".
    sc.applyOptions({ localization: { priceFormatter: (v: number) => {
      const abs = Math.abs(v)
      if (abs === 0) return '0.00'
      if (abs < 0.0001) return v.toFixed(6)
      if (abs < 0.01)   return v.toFixed(5)
      if (abs < 1)      return v.toFixed(4)
      return v.toFixed(2)
    }}})
    rsiRef.current = sc.addSeries(LineSeries, { color: '#38bdf8', lineWidth: 2, priceLineVisible: false })
    macdLineRef.current = sc.addSeries(LineSeries, { color: '#f59e0b', lineWidth: 1, priceLineVisible: false, lastValueVisible: true, priceFormat: macdPriceFmt, visible: false })
    macdSignRef.current = sc.addSeries(LineSeries, { color: '#ec4899', lineWidth: 1, priceLineVisible: false, lastValueVisible: true, priceFormat: macdPriceFmt, visible: false })
    macdHistRef.current = sc.addSeries(HistogramSeries, { priceLineVisible: false, lastValueVisible: true, priceFormat: macdPriceFmt, visible: false })

    // Sync scroll/zoom between panels using TIME range (not logical index).
    // Logical-index sync breaks because the sub chart has fewer data points than
    // the main chart (MACD starts at candle 25+, signal at 33+), so the same
    // logical index maps to a different timestamp in each chart — causing the
    // MACD line to appear shifted relative to the candles beneath it.
    // Guard against infinite feedback: setVisibleRange fires subscribeVisibleTimeRangeChange
    // synchronously, so without this flag A→setRange(B)→B fires→setRange(A)→∞ loop.
    let syncing = false
    mc.timeScale().subscribeVisibleTimeRangeChange(range => {
      if (syncing || !range || !subChart.current) return
      // Guard: setVisibleRange on an empty chart calls logicalRangeForTimeRange
      // which calls ensureNotNull(firstIndex()) — firstIndex is null when the
      // sub-chart has no data, throwing "Value is null" synchronously inside
      // the main chart's setData() call (because setData fires this event).
      if (subChart.current.timeScale().getVisibleLogicalRange() === null) return
      syncing = true; try { subChart.current.timeScale().setVisibleRange(range) } catch {} ; syncing = false
    })
    sc.timeScale().subscribeVisibleTimeRangeChange(range => {
      if (syncing || !range || !mainChart.current) return
      if (mainChart.current.timeScale().getVisibleLogicalRange() === null) return
      syncing = true; try { mainChart.current.timeScale().setVisibleRange(range) } catch {} ; syncing = false
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

    return () => { ro.disconnect(); mc.remove(); sc.remove(); markersPluginRef.current = null; cachedMarkersRef.current = null }
  }, [])

  // ─── Apply-markers (no re-fetch) ─────────────────────────────────────────
  // Reads cached signal/trade data + toggle refs → rebuilds markers/price-lines
  // client-side. Safe to call from fetchAndRender AND from toggle effects.
  const applyMarkersImpl = useCallback(() => {
    const data = cachedMarkersRef.current
    if (!data || !candleRef.current) return
    const { signals, trades, minTime, maxTime } = data

    // Remove old TP/SL price lines before we re-add them
    tradeLineRefs.current.forEach(l => { try { candleRef.current!.removePriceLine(l) } catch {} })
    tradeLineRefs.current = []

    const lwtMarkers = showSignalsRef.current
      ? signals
          .filter(m => m.time >= minTime && m.time <= maxTime)
          .map(m => ({
            time: m.time as any,
            position: (m.signal === 'BUY' || m.signal === 'COVER' ? 'belowBar' : 'aboveBar') as any,
            color: m.signal === 'BUY' || m.signal === 'COVER' ? '#86efac' : '#fca5a5',
            shape: (m.signal === 'BUY' || m.signal === 'COVER' ? 'arrowUp' : 'arrowDown') as any,
            text: `SIG ${m.signal}${m.confidence ? ` ${Math.round(m.confidence * 100)}%` : ''}`,
            size: 1,
          }))
          .sort((a, b) => (a.time as number) - (b.time as number))
      : []

    const tradeMarkers: any[] = []
    if (showTradesRef.current && candleRef.current) {
      for (const t of trades) {
        const isBuy      = t.side === 'buy' || t.side === 'cover' || t.side === 'long'
        const entryColor = t.is_paper ? '#60a5fa' : '#f59e0b'
        const modeTag    = t.is_paper ? 'P' : 'L'
        const sideLabel  = t.side === 'long' ? 'BUY' : t.side === 'short' ? 'SELL' : t.side.toUpperCase()

        if (t.entry_time && t.entry_price && t.entry_time >= minTime) {
          // Snap to the last candle when the trade was placed after the chart window
          // ends (e.g. a live open trade during a weekend gap on IBKR forex).
          const markerTime = Math.min(t.entry_time, maxTime)
          const pnlTag = t.pnl_pct != null ? ` ${t.pnl_pct > 0 ? '+' : ''}${t.pnl_pct.toFixed(1)}%` : ''
          tradeMarkers.push({
            time:     markerTime as any,
            position: isBuy ? 'belowBar' : 'aboveBar',
            color:    entryColor,
            shape:    isBuy ? 'arrowUp' : 'arrowDown',
            text:     `[${modeTag}] ${sideLabel}${pnlTag}`,
            size:     2,
          })
        }

        if (t.exit_time && t.exit_price && t.exit_time >= minTime && t.exit_time <= maxTime) {
          const won = t.pnl != null ? t.pnl > 0 : null
          const exitColor = won === null ? '#9ca3af' : won ? '#22c55e' : '#ef4444'
          const pnlTag = t.pnl_pct != null ? ` ${t.pnl_pct > 0 ? '+' : ''}${t.pnl_pct.toFixed(1)}%` : ''
          tradeMarkers.push({
            time:     t.exit_time as any,
            position: isBuy ? 'aboveBar' : 'belowBar',
            color:    exitColor,
            shape:    'circle',
            text:     `EXIT${pnlTag}`,
            size:     1,
          })
        }

        if (t.status === 'OPEN' || t.status === 'open') {
          if (t.stop_loss) {
            const l = candleRef.current!.createPriceLine({ price: t.stop_loss, color: '#ef4444', lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: 'SL' })
            tradeLineRefs.current.push(l)
          }
          if (t.take_profit) {
            const l = candleRef.current!.createPriceLine({ price: t.take_profit, color: '#22c55e', lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: 'TP' })
            tradeLineRefs.current.push(l)
          }
        }
      }
    }

    const allMarkers = [...lwtMarkers, ...tradeMarkers].sort((a, b) => (a.time as number) - (b.time as number))
    markersPluginRef.current?.setMarkers(allMarkers)
  }, []) // reads only from refs — stable, no state deps

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
  // Sync state → ref BEFORE re-applying markers (declaration order = execution order)
  useEffect(() => { showSignalsRef.current = showSignals }, [showSignals])
  useEffect(() => { showTradesRef.current  = showTrades  }, [showTrades])
  // Re-apply markers from cache when a visibility toggle changes — never triggers a re-fetch
  useEffect(() => { if (cachedMarkersRef.current) applyMarkersImpl() }, [showSignals, showTrades, applyMarkersImpl])

  // ─── Fetch & render ──────────────────────────────────────────────────────
  const fetchAndRender = useCallback(async () => {
    if (!candleRef.current) return
    // Cancel any previous in-flight fetch so stale data from the old
    // symbol/timeframe never populates the series after it was cleared.
    abortRef.current?.abort()
    const ctrl = new AbortController()
    abortRef.current = ctrl
    const { signal } = ctrl
    isFetchingRef.current = true
    setLoading(true)
    setLoadingMsg(broker === 'ibkr' ? 'Connecting to IBKR…' : 'Loading…')
    // Update paramsKey so in-flight live polls from the previous symbol abort
    paramsKeyRef.current = `${broker}/${symbol}/${timeframe}`
    // CRITICAL: clear markers BEFORE setData — the createSeriesMarkers plugin
    // fires synchronously inside setData and tries to anchor each marker to a
    // bar by index.  If old markers from a previous symbol are still attached,
    // findBar() returns null for non-matching timestamps and ensureNotNull()
    // throws "Value is null", aborting the entire setData call.
    markersPluginRef.current?.setMarkers([])
    cachedMarkersRef.current = null
    // Clear all series immediately so the chart goes blank while the new
    // symbol loads — prevents the WS from appending new-symbol candles onto
    // old-symbol data (which creates a visible gap)
    lastCandleTimeRef.current = null
    candlesDataRef.current = []
    candleRef.current.setData([])
    volRef.current?.setData([])
    ema20Ref.current?.setData([])
    ema50Ref.current?.setData([])
    bbUpperRef.current?.setData([])
    bbMidRef.current?.setData([])
    bbLowerRef.current?.setData([])
    rsiRef.current?.setData([])
    macdLineRef.current?.setData([])
    macdSignRef.current?.setData([])
    macdHistRef.current?.setData([])
    for (const s of maSeriesRefs.current.values()) s.setData([])
    try {
      // Compute since/until ms from preset or custom date pickers
      let since: number, until: number
      if (rangePreset === 'Custom') {
        since = customFrom ? new Date(customFrom).getTime() : Date.now() - 30*24*3600*1000
        until = customTo   ? new Date(customTo + 'T23:59:59').getTime() : Date.now()
      } else {
        ;({ since, until } = presetToRange(rangePreset))
      }

      const [candleRes, signalRes, tradeRes] = await Promise.all([
        axios.get('/api/charts/candles', { params: { symbol, timeframe, broker, since, until }, signal }),
        axios.get('/api/charts/signals', { params: { symbol, timeframe, broker, limit: 500 }, signal }),
        axios.get('/api/charts/trades',  { params: { symbol, broker, since, until }, signal }),
      ])

      const rawCandles: Candle[] = (candleRes.data.candles as Candle[]).sort((a, b) => a.time - b.time)
      // Strip any bar where OHLCV contains null/undefined/NaN/Infinity.
      // FastAPI can serialise Python float('nan') as the literal token NaN (invalid
      // JSON) or as null depending on the serialiser (orjson → null, stdlib → NaN).
      // Either way lightweight-charts' ensureNotNull() throws "Value is null" if
      // it receives a null for time/open/high/low/close.  Filter here so the
      // candlestick series never receives bad data regardless of what the backend sends.
      const candles = rawCandles.filter(c =>
        c.time != null && isFinite(c.time) && c.time > 0 &&
        c.open  != null && isFinite(c.open)  && c.open  > 0 &&
        c.high  != null && isFinite(c.high)  && c.high  > 0 &&
        c.low   != null && isFinite(c.low)   && c.low   > 0 &&
        c.close != null && isFinite(c.close) && c.close > 0
      )
      if (!candles.length) { toast.error('No candle data for this symbol.'); return }

      try {
        candleRef.current!.setData(
          candles.map(c => ({ time: c.time as any, open: c.open, high: c.high, low: c.low, close: c.close }))
        )
      } catch (seriesErr: any) {
        console.error('[Chart] setData error:', seriesErr, 'first candle:', candles[0], 'last candle:', candles[candles.length - 1])
        toast.error(`Chart series error: ${seriesErr?.message ?? seriesErr}`)
        return
      }
      try {
        volRef.current?.setData(
          candles.map(c => ({ time: c.time as any, value: c.volume ?? 0, color: c.close >= c.open ? '#22c55e33' : '#ef444433' }))
        )
      } catch {}

      // Sanitise closes: replace null/NaN/Infinity (IBKR sentinel values) with
      // the nearest previous valid close so EMA/RSI/MACD don't propagate NaN.
      const rawCloses = candles.map(c => c.close)
      let lastValid = rawCloses.find(v => v != null && isFinite(v)) ?? 0
      const closes = rawCloses.map(v => {
        if (v == null || !isFinite(v)) return lastValid
        lastValid = v; return v
      })

      // Each indicator series is wrapped in its own try/catch so that an error
      // on one (e.g. lightweight-charts "Value is null" for an edge-case value)
      // does not prevent subsequent indicators from being rendered.
      // isOK(v) rejects null AND NaN/Infinity which all pass a plain !== null check.
      const isOK = (v: number | null): v is number => v !== null && isFinite(v)

      const ema20 = computeEMA(closes, 20)
      const ema50 = computeEMA(closes, 50)
      try { ema20Ref.current?.setData(candles.flatMap((c, i) => isOK(ema20[i]) ? [{ time: c.time as any, value: ema20[i]! }] : [])) } catch {}
      try { ema50Ref.current?.setData(candles.flatMap((c, i) => isOK(ema50[i]) ? [{ time: c.time as any, value: ema50[i]! }] : [])) } catch {}

      const bb = computeBB(closes)
      try { bbUpperRef.current?.setData(candles.flatMap((c, i) => isOK(bb.upper[i]) ? [{ time: c.time as any, value: bb.upper[i]! }] : [])) } catch {}
      try { bbMidRef.current?.setData(candles.flatMap((c, i)   => isOK(bb.mid[i])   ? [{ time: c.time as any, value: bb.mid[i]! }]   : [])) } catch {}
      try { bbLowerRef.current?.setData(candles.flatMap((c, i) => isOK(bb.lower[i]) ? [{ time: c.time as any, value: bb.lower[i]! }] : [])) } catch {}

      const rsi = computeRSI(closes)
      try { rsiRef.current?.setData(candles.flatMap((c, i) => isOK(rsi[i]) ? [{ time: c.time as any, value: rsi[i]! }] : [])) } catch {}

      const { macd, signal: macdSig, hist } = computeMACD(closes)
      try { macdLineRef.current?.setData(candles.flatMap((c, i) => isOK(macd[i])    ? [{ time: c.time as any, value: macd[i]! }]    : [])) } catch {}
      try { macdSignRef.current?.setData(candles.flatMap((c, i) => isOK(macdSig[i]) ? [{ time: c.time as any, value: macdSig[i]! }] : [])) } catch {}
      try { macdHistRef.current?.setData(candles.flatMap((c, i) => isOK(hist[i]) ? [{
        time: c.time as any, value: hist[i]!, color: hist[i]! >= 0 ? '#22c55e66' : '#ef444466',
      }] : [])) } catch {}

      const markers: SignalMarker[] = signalRes.data.markers
      setSignalCount(markers.length)
      const trades: TradeMarker[] = tradeRes.data.trades ?? []
      setTradeCount(trades.length)
      const minTime = candles[0].time, maxTime = candles[candles.length - 1].time
      // Cache raw data then apply markers client-side (toggles reuse this without re-fetching)
      cachedMarkersRef.current = { signals: markers, trades, minTime, maxTime }
      applyMarkersImpl()

      mainChart.current?.timeScale().fitContent()
      subChart.current?.timeScale().fitContent()
      setCandleCount(candleRes.data.candle_count ?? candleRes.data.candles?.length ?? 0)
      setLastUpdated(new Date().toLocaleTimeString())
      // Record last candle time for live poll and refresh all MA overlays
      if (candles.length > 0) {
        lastCandleTimeRef.current = candles[candles.length - 1].time
        candlesDataRef.current = candles
        const maCloses = candles.map(c => c.close)
        for (const m of maOverlaysRef.current) {
          const s = maSeriesRefs.current.get(m.id)
          if (!s) continue
          const maData = computeMA(maCloses, m.period)
          s.setData(candles.flatMap((c, i) => maData[i] !== null ? [{ time: c.time as any, value: maData[i]! }] : []))
        }
      }
    } catch (err: any) {
      if (!axios.isCancel(err))
        toast.error(err?.response?.data?.detail ?? err?.message ?? 'Failed to load chart data.')
    } finally {
      // Only release the loading lock if THIS fetch is still the active one.
      // If it was aborted, a newer fetchAndRender already owns the lock.
      if (!ctrl.signal.aborted) {
        setLoading(false)
        isFetchingRef.current = false
      }
    }
  }, [symbol, timeframe, broker, rangePreset, customFrom, customTo, applyMarkersImpl])

  useEffect(() => { fetchAndRender() }, [fetchAndRender])

  // ─── Live candle poll ─────────────────────────────────────────────────────
  // Fetches the last 2 candles at TF-appropriate intervals and calls
  // series.update() — lightweight-charts updates the current bar in place
  // or appends a new one when a new period starts. Exactly like TradingView.
  const fetchLiveUpdate = useCallback(async () => {
    // Skip REST poll entirely when the WebSocket is live — both updating
    // series.update() concurrently will crash lightweight-charts.
    if (wsLiveRef.current) return
    if (!candleRef.current || lastCandleTimeRef.current === null || isFetchingRef.current) return
    // Capture key before the async gap — used to discard stale responses
    const capturedKey = paramsKeyRef.current
    // Request from last known candle onwards (no backward buffer so we never try
    // to update a non-last bar, which lightweight-charts forbids and throws on).
    const since = lastCandleTimeRef.current * 1000
    const until = Date.now()
    try {
      const res = await axios.get('/api/charts/candles', {
        params: { symbol, timeframe, broker, since, until },
      })
      // Discard if params changed or a full fetch started while we were waiting
      if (isFetchingRef.current || paramsKeyRef.current !== capturedKey) return
      const fresh: Candle[] = (res.data.candles as Candle[]).sort((a, b) => a.time - b.time)
      let didUpdate = false
      for (const c of fresh) {
        // lightweight-charts only allows update() on the last bar or appending a
        // newer bar — skip anything strictly older than our last known candle.
        if (c.time < (lastCandleTimeRef.current ?? 0)) continue
        try {
          candleRef.current?.update({ time: c.time as any, open: c.open, high: c.high, low: c.low, close: c.close })
          volRef.current?.update({ time: c.time as any, value: c.volume, color: c.close >= c.open ? '#22c55e33' : '#ef444433' })
          didUpdate = true
        } catch {
          // time ordering violated for this bar — skip it
        }
        if (c.time > (lastCandleTimeRef.current ?? 0)) lastCandleTimeRef.current = c.time
      }
      if (didUpdate) {
        setLastUpdated(new Date().toLocaleTimeString())
        mainChart.current?.timeScale().scrollToRealTime()
      }
    } catch {
      // silently ignore network errors — next full fetchAndRender will recover
    }
  }, [symbol, timeframe, broker])

  useEffect(() => {
    if (livePollRef.current) clearInterval(livePollRef.current)
    const secs = TF_POLL_SECS[timeframe] ?? 60
    livePollRef.current = setInterval(fetchLiveUpdate, secs * 1000)
    return () => { if (livePollRef.current) clearInterval(livePollRef.current) }
  }, [fetchLiveUpdate, timeframe])

  // Keep maOverlaysRef in sync (avoids stale closures in fetchAndRender)
  useEffect(() => { maOverlaysRef.current = maOverlays }, [maOverlays])

  // MA series management — create/destroy series and re-apply data when overlays change
  useEffect(() => {
    if (!mainChart.current) return
    const activeIds = new Set(maOverlays.map(m => m.id))
    for (const [id, series] of Array.from(maSeriesRefs.current)) {
      if (!activeIds.has(id)) {
        try { mainChart.current.removeSeries(series) } catch {}
        maSeriesRefs.current.delete(id)
      }
    }
    for (const m of maOverlays) {
      if (!maSeriesRefs.current.has(m.id)) {
        const s = mainChart.current.addSeries(LineSeries, {
          color: m.color, lineWidth: 1,
          priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
        })
        maSeriesRefs.current.set(m.id, s)
      }
    }
    if (candlesDataRef.current.length > 0) {
      const closes = candlesDataRef.current.map(c => c.close)
      for (const m of maOverlays) {
        const s = maSeriesRefs.current.get(m.id)
        if (!s) continue
        const ma = computeMA(closes, m.period)
        s.setData(candlesDataRef.current.flatMap((c, i) => ma[i] !== null ? [{ time: c.time as any, value: ma[i]! }] : []))
      }
    }
  }, [maOverlays])

  // Live WebSocket — real-time candle updates for all brokers.
  // Auto-reconnects every 60 s after a close so the chart picks up data as soon
  // as IB Gateway comes online or the market reopens. Binance never closes
  // (persistent public stream) so reconnect is only needed for IBKR / Alpaca.
  useEffect(() => {
    // Cancel any pending reconnect from the previous render cycle
    if (wsRetryRef.current) { clearTimeout(wsRetryRef.current); wsRetryRef.current = null }
    if (wsRef.current) { wsRef.current.close(); wsRef.current = null }
    setWsLive(false)
    setMarketClosed(false)
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const url = `${proto}//${window.location.host}/ws/kline?symbol=${encodeURIComponent(symbol)}&timeframe=${encodeURIComponent(timeframe)}&broker=${encodeURIComponent(broker)}`
    const ws = new WebSocket(url)
    wsRef.current = ws
    ws.onopen  = () => {
      wsLiveRef.current = true
      setWsLive(true)
      setMarketClosed(false)
      if (wsRetryRef.current) { clearTimeout(wsRetryRef.current); wsRetryRef.current = null }
    }
    ws.onerror = () => { wsLiveRef.current = false; setWsLive(false) }
    ws.onclose = (e) => {
      // Guard: if the user switched symbol/timeframe, a newer WS is already
      // active.  Don't let this stale close clobber its state or schedule a
      // spurious reconnect.
      if (wsRef.current !== ws) return
      wsLiveRef.current = false
      setWsLive(false)
      if (broker !== 'binance') {
        // Show "Market Closed" only for normal close (code 1000/1001 = backend
        // 60-second no-data timeout).  Error closes (4001 IBKR stream error,
        // 4002 IBKR not connected) should not show a misleading overlay.
        if (e.code === 1000 || e.code === 1001) setMarketClosed(true)
        // Schedule reconnect: 60 s is short enough to pick up data quickly when
        // IB Gateway starts or the market opens, but not so frequent as to spam.
        wsRetryRef.current = setTimeout(() => {
          wsRetryRef.current = null
          setWsRetryKey(k => k + 1)
        }, 60_000)
      }
    }
    // Throttle the React state update (setLastUpdated) to at most once per 2s.
    // series.update() is called every message but is a DOM mutation with no
    // React re-render — safe to call at full rate.
    let lastStateUpdate = 0
    ws.onmessage = (evt) => {
      try {
        const d = JSON.parse(evt.data) as { time: number; open: number; high: number; low: number; close: number; volume: number }
        // Reject during a full fetch — prevents new-symbol WS candles being
        // appended to old-symbol series data before setData() replaces it.
        if (isFetchingRef.current) return
        // Use explicit positivity checks instead of !d.close so that NaN values
        // (ib_insync sentinel) are caught correctly — !NaN is true but NaN > 0
        // is false, so `!(d.close > 0)` rejects both NaN and non-positive.
        if (!candleRef.current || !(d.time > 0) || !(d.close > 0) || !(d.open > 0) || !(d.high > 0) || !(d.low > 0)) return
        if (lastCandleTimeRef.current !== null && d.time < lastCandleTimeRef.current) return
        try {
          candleRef.current.update({ time: d.time as any, open: d.open, high: d.high, low: d.low, close: d.close })
          volRef.current?.update({ time: d.time as any, value: d.volume ?? 0, color: d.close >= d.open ? '#22c55e33' : '#ef444433' })
          if (d.time > (lastCandleTimeRef.current ?? 0)) lastCandleTimeRef.current = d.time
          mainChart.current?.timeScale().scrollToRealTime()
          // Throttle React state update to avoid re-render on every tick
          const now = Date.now()
          if (now - lastStateUpdate > 2000) {
            lastStateUpdate = now
            setLastUpdated(new Date().toLocaleTimeString())
          }
        } catch {}
      } catch {}
    }
    return () => {
      ws.close()
      wsLiveRef.current = false
      setWsLive(false)
      if (wsRetryRef.current) { clearTimeout(wsRetryRef.current); wsRetryRef.current = null }
    }
  }, [symbol, timeframe, broker, wsRetryKey])

  // Fetch symbols when broker changes (keep symbol in sync atomically)
  useEffect(() => {
    setAllSymbols([])
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

  // Close indicator panel on outside click
  useEffect(() => {
    const h = (e: MouseEvent) => { if (indPanelRef.current && !indPanelRef.current.contains(e.target as Node)) setShowIndPanel(false) }
    document.addEventListener('mousedown', h)
    return () => document.removeEventListener('mousedown', h)
  }, [])

  const filteredSymbols = allSymbols.filter(s => s.toLowerCase().includes(symQuery.toLowerCase())).slice(0, 50)
  const selectSymbol = (s: string) => { setSymbol(s); setSymQuery(s); setSymOpen(false) }
  const addMaOverlay = () => {
    const period = Math.max(2, Math.min(500, newMAPeriod))
    const id = `ma_${Date.now()}`
    const color = MA_COLORS[maOverlays.length % MA_COLORS.length]
    setMaOverlays(prev => [...prev, { id, period, color }])
    setShowAddMA(false)
  }
  const removeMaOverlay = (id: string) => setMaOverlays(prev => prev.filter(m => m.id !== id))

  const sel = 'bg-dark-700 border border-dark-500 text-gray-200 text-xs rounded-lg px-2 py-1.5 focus:outline-none focus:ring-1 focus:ring-brand-500'

  return (
    <div ref={containerRef} className="flex flex-col h-full bg-[#111111] text-gray-100 border border-dark-700">

      {/* ─── Toolbar ──────────────────────────────────────────── */}
      <div className="flex flex-wrap items-center gap-1.5 px-2 py-1.5 border-b border-dark-600 bg-dark-800 shrink-0">

        {/* Broker */}
        <select value={broker} onChange={e => {
          const b = e.target.value
          const def = BROKER_DEFAULT[b] ?? ''
          // Batch all three updates so React re-renders ONCE with the new
          // broker + matching symbol — prevents fetchAndRender firing with
          // broker=binance / symbol=SPY (stale mismatch that causes errors)
          setBroker(b)
          setSymbol(def)
          setSymQuery(def)
        }} className={sel}>
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
          {!compact && (loading ? loadingMsg : 'Refresh')}
        </button>

        {/* ─── Indicators dropdown ───────────────────────────────── */}
        <div ref={indPanelRef} className="relative border-l border-dark-600 pl-2">
          <button
            onClick={() => setShowIndPanel(v => !v)}
            className={`flex items-center gap-1 text-xs px-2 py-1.5 rounded-lg border transition-all ${
              showIndPanel
                ? 'bg-brand-500/20 border-brand-500/60 text-brand-400'
                : 'bg-dark-700 border-dark-500 text-gray-300 hover:text-white hover:bg-dark-600'
            }`}
          >
            <LayoutGrid size={12} />
            {!compact && <span>Indicators</span>}
          </button>

          {showIndPanel && (
            <div className="absolute z-50 top-full mt-1 left-0 bg-dark-900 border border-dark-600 rounded-xl shadow-2xl p-3 flex flex-col gap-3 w-52">

              {/* Overlays group */}
              <div>
                <p className="text-[10px] font-semibold text-gray-500 uppercase tracking-wider mb-1.5">Overlays</p>
                <div className="flex flex-col gap-1">
                  {[
                    { label: 'EMA (20 / 50)', color: 'text-amber-400', accent: 'accent-amber-400', checked: showEMA, set: setShowEMA },
                    { label: 'Bollinger Bands', color: 'text-indigo-400', accent: 'accent-indigo-400', checked: showBB, set: setShowBB },
                    { label: 'Volume', color: 'text-gray-400', accent: '', checked: showVolume, set: setShowVolume },
                    { label: 'Signal markers', color: 'text-green-400', accent: 'accent-green-400', checked: showSignals, set: setShowSignals },
                    { label: 'Trade markers', color: 'text-sky-400', accent: 'accent-sky-400', checked: showTrades, set: setShowTrades },
                  ].map(({ label, color, accent, checked, set }) => (
                    <label key={label} className="flex items-center gap-2 cursor-pointer select-none px-1 py-0.5 rounded hover:bg-dark-700">
                      <input type="checkbox" checked={checked} onChange={e => set(e.target.checked)} className={accent || undefined} />
                      <span className={`text-xs ${color}`}>{label}</span>
                    </label>
                  ))}
                </div>
              </div>

              {/* Oscillator group */}
              <div className="border-t border-dark-700 pt-2">
                <p className="text-[10px] font-semibold text-gray-500 uppercase tracking-wider mb-1.5">Oscillator</p>
                <div className="flex gap-1">
                  {(['RSI', 'MACD'] as const).map(ind => (
                    <button key={ind} onClick={() => setSubPanel(ind)}
                      className={`flex-1 text-xs py-1 rounded-md transition-all border ${
                        subPanel === ind
                          ? ind === 'RSI'
                            ? 'bg-sky-500/20 border-sky-500/50 text-sky-400 font-medium'
                            : 'bg-yellow-500/20 border-yellow-500/50 text-yellow-400 font-medium'
                          : 'bg-dark-700 border-dark-600 text-gray-400 hover:text-gray-200'
                      }`}>
                      {ind}
                    </button>
                  ))}
                </div>
              </div>

              {/* MA group */}
              <div className="border-t border-dark-700 pt-2">
                <p className="text-[10px] font-semibold text-gray-500 uppercase tracking-wider mb-1.5">Moving Averages</p>
                <div className="flex flex-col gap-1">
                  {maOverlays.map(m => (
                    <div key={m.id} className="flex items-center justify-between px-1 py-0.5 rounded hover:bg-dark-700">
                      <span className="text-xs font-medium" style={{ color: m.color }}>MA {m.period}</span>
                      <button onClick={() => removeMaOverlay(m.id)}
                        className="text-gray-600 hover:text-red-400 text-xs leading-none">×</button>
                    </div>
                  ))}
                  {maOverlays.length < 6 && (
                    <div className="flex flex-col gap-1.5 mt-1">
                      {!showAddMA ? (
                        <button onClick={() => setShowAddMA(true)}
                          className="text-xs py-1 rounded-md bg-dark-700 border border-dark-600 text-cyan-400 hover:bg-dark-600 hover:text-cyan-300">
                          + Add MA
                        </button>
                      ) : (
                        <div className="flex gap-1">
                          <input type="number" min={2} max={500} value={newMAPeriod}
                            onChange={e => setNewMAPeriod(Number(e.target.value))}
                            onKeyDown={e => e.key === 'Enter' && addMaOverlay()}
                            autoFocus
                            className="bg-dark-700 text-gray-200 text-xs rounded px-2 py-1 border border-dark-500 w-full" />
                          <button onClick={addMaOverlay}
                            className="text-xs px-2 py-1 rounded-md bg-cyan-600 text-white hover:bg-cyan-500 shrink-0">Add</button>
                          <button onClick={() => setShowAddMA(false)}
                            className="text-xs px-1.5 py-1 rounded-md bg-dark-700 text-gray-400 hover:text-gray-200 shrink-0">×</button>
                        </div>
                      )}
                    </div>
                  )}
                </div>
              </div>

            </div>
          )}
        </div>

        {/* Active MA tags in toolbar (quick remove) */}
        {maOverlays.length > 0 && (
          <div className="flex items-center gap-1">
            {maOverlays.map(m => (
              <span key={m.id} className="flex items-center gap-0.5 text-xs font-medium px-1 py-0.5 rounded bg-dark-700" style={{ color: m.color }}>
                MA{m.period}
                <button onClick={() => removeMaOverlay(m.id)} className="text-gray-500 hover:text-red-400 leading-none ml-0.5 text-[10px]">×</button>
              </span>
            ))}
          </div>
        )}

        {/* Oscillator quick badge */}
        <div className="flex items-center gap-1 border-l border-dark-600 pl-2">
          {(['RSI', 'MACD'] as const).map(ind => (
            <button key={ind} onClick={() => setSubPanel(ind)}
              className={`text-xs px-1.5 py-1 rounded-md transition-all ${
                subPanel === ind
                  ? ind === 'RSI' ? 'bg-sky-500/20 text-sky-400 font-medium' : 'bg-yellow-500/20 text-yellow-400 font-medium'
                  : 'text-gray-500 hover:text-gray-300'
              }`}>
              {ind}
            </button>
          ))}
        </div>

        {/* Status */}
        {!compact && (
          <div className="ml-auto flex items-center gap-2 text-xs text-gray-500">
            {wsLive && <span className="flex items-center gap-1 text-green-400 font-medium"><span className="w-1.5 h-1.5 rounded-full bg-green-400 animate-pulse inline-block" />LIVE</span>}
            {signalCount > 0 && <span className="text-brand-400">{signalCount} signal{signalCount !== 1 ? 's' : ''}</span>}
            {tradeCount > 0 && <span className="text-sky-400">{tradeCount} trade{tradeCount !== 1 ? 's' : ''}</span>}
            {candleCount > 0 && <span className="text-gray-600">{candleCount.toLocaleString()} candles</span>}
            {lastUpdated && <span>{lastUpdated}</span>}
          </div>
        )}
      </div>

      {/* ─── Legend ───────────────────────────────────────────── */}
      {!compact && (
        <div className="flex flex-wrap items-center gap-3 px-3 py-1 bg-dark-800/60 border-b border-dark-700 text-xs text-gray-500 shrink-0">
          <span className="flex items-center gap-1.5"><span className="w-2 h-2 rounded-full bg-green-300" /> SIG BUY (no trade)</span>
          <span className="flex items-center gap-1.5"><span className="w-2 h-2 rounded-full bg-red-300" /> SIG SELL (no trade)</span>
          <span className="flex items-center gap-1.5"><span className="w-2 h-2 rounded-full bg-sky-400" /> Trade entry (paper)</span>
          <span className="flex items-center gap-1.5"><span className="w-2 h-2 rounded-full bg-yellow-400" /> Trade entry (live)</span>
          <span className="flex items-center gap-1.5"><span className="w-2 h-2 rounded-full border border-gray-400" /> Exit</span>
          <span className="flex items-center gap-1.5"><span className="w-4 border-t-2 border-dashed border-green-500" /> TP</span>
          <span className="flex items-center gap-1.5"><span className="w-4 border-t-2 border-dashed border-red-500" /> SL</span>
          <span className="flex items-center gap-1.5"><span className="w-4 h-px bg-amber-400" /> EMA 20</span>
          <span className="flex items-center gap-1.5"><span className="w-4 h-px bg-violet-400" /> EMA 50</span>
          {maOverlays.map(m => (
            <span key={m.id} className="flex items-center gap-1.5">
              <span className="w-4 h-px" style={{ backgroundColor: m.color }} />
              MA {m.period}
            </span>
          ))}
          {showBB && <span className="flex items-center gap-1.5"><span className="w-4 border-t-2 border-dashed border-indigo-400" /> BB(20,2)</span>}
          {subPanel === 'RSI' && <span className="flex items-center gap-1.5"><span className="w-4 h-px bg-sky-400" /> RSI 14</span>}
          {subPanel === 'MACD' && <>
            <span className="flex items-center gap-1.5"><span className="w-4 h-px bg-amber-400" /> MACD</span>
            <span className="flex items-center gap-1.5"><span className="w-4 h-px bg-pink-400" /> Signal</span>
          </>}
        </div>
      )}

      {/* ─── Charts ───────────────────────────────────────────── */}
      <div className="flex-1 flex flex-col min-h-0 relative">
        {broker !== 'binance' && marketClosed && (
          <div className="absolute inset-0 flex items-center justify-center z-20 pointer-events-none">
            <div className="bg-dark-900/90 border border-dark-600 rounded-xl px-5 py-4 text-center backdrop-blur-sm">
              {isFxPair(symbol) ? (
                <>
                  <p className="text-sm font-semibold text-gray-300">FX Market Closed</p>
                  <p className="text-xs text-gray-500 mt-1">Opens {nextFXOpen()}</p>
                </>
              ) : (
                <>
                  <p className="text-sm font-semibold text-gray-300">Market Closed</p>
                  <p className="text-xs text-gray-500 mt-1">NYSE opens {nextNYSEOpen()}</p>
                </>
              )}
            </div>
          </div>
        )}
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
