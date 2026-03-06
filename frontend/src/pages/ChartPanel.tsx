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
const TF_POLL_SECS: Record<string, number> = {
  '1m': 10, '5m': 20, '15m': 30, '1h': 60, '4h': 120, '1d': 300, '3d': 1800, '1w': 3600,
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
  const [tradeCount, setTradeCount]   = useState(0)
  const [candleCount, setCandleCount] = useState(0)
  const [showTrades, setShowTrades]   = useState(true)
  const [maOverlays, setMaOverlays]   = useState<MAOverlay[]>([])
  const [showAddMA, setShowAddMA]     = useState(false)
  const [newMAPeriod, setNewMAPeriod] = useState(20)
  const [wsLive, setWsLive]           = useState(false)

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

  // MA overlay refs
  const maSeriesRefs   = useRef<Map<string, ISeriesApi<'Line'>>>(new Map())
  const candlesDataRef = useRef<Candle[]>([])
  const maOverlaysRef  = useRef<MAOverlay[]>([])
  // Live WebSocket ref
  const wsRef = useRef<WebSocket | null>(null)

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
    isFetchingRef.current = true
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

      const [candleRes, signalRes, tradeRes] = await Promise.all([
        axios.get('/api/charts/candles', { params: { symbol, timeframe, broker, since, until } }),
        axios.get('/api/charts/signals', { params: { symbol, timeframe, broker, limit: 500 } }),
        axios.get('/api/charts/trades',  { params: { symbol, broker, since, until } }),
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

      // ── Trade markers & TP/SL price lines ─────────────────────────────────
      // Remove price lines from previous render
      tradeLineRefs.current.forEach(l => { try { candleRef.current!.removePriceLine(l) } catch {} })
      tradeLineRefs.current = []

      const trades: TradeMarker[] = tradeRes.data.trades ?? []
      setTradeCount(trades.length)
      const tradeMarkers: any[] = []

      if (showTrades && candleRef.current) {
        for (const t of trades) {
          const isBuy     = t.side === 'buy' || t.side === 'cover'
          const entryColor = t.is_paper ? '#60a5fa' : '#f59e0b'  // blue = paper, gold = live
          const modeTag   = t.is_paper ? 'P' : 'L'

          // Entry marker
          if (t.entry_time && t.entry_price && t.entry_time >= minTime && t.entry_time <= maxTime) {
            const pnlTag = t.pnl_pct != null
              ? ` ${t.pnl_pct > 0 ? '+' : ''}${t.pnl_pct.toFixed(1)}%`
              : ''
            tradeMarkers.push({
              time:     t.entry_time as any,
              position: isBuy ? 'belowBar' : 'aboveBar',
              color:    entryColor,
              shape:    isBuy ? 'arrowUp' : 'arrowDown',
              text:     `[${modeTag}] ${t.side.toUpperCase()}${pnlTag}`,
              size:     2,
            })
          }

          // Exit marker (closed trades)
          if (t.exit_time && t.exit_price && t.exit_time >= minTime && t.exit_time <= maxTime) {
            const won = t.pnl != null ? t.pnl > 0 : null
            const exitColor = won === null ? '#9ca3af' : won ? '#22c55e' : '#ef4444'
            const pnlTag = t.pnl_pct != null
              ? ` ${t.pnl_pct > 0 ? '+' : ''}${t.pnl_pct.toFixed(1)}%`
              : ''
            tradeMarkers.push({
              time:     t.exit_time as any,
              position: isBuy ? 'aboveBar' : 'belowBar',
              color:    exitColor,
              shape:    'circle',
              text:     `EXIT${pnlTag}`,
              size:     1,
            })
          }

          // TP/SL price lines (only for open positions — clutter if shown for closed)
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

      // Combine signal + trade markers, sort by time
      const allMarkers = [...lwtMarkers, ...tradeMarkers].sort((a, b) => (a.time as number) - (b.time as number))
      createSeriesMarkers(candleRef.current!, allMarkers)

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
      toast.error(err?.response?.data?.detail ?? 'Failed to load chart data.')
    } finally {
      setLoading(false)
      isFetchingRef.current = false
    }
  }, [symbol, timeframe, broker, rangePreset, customFrom, customTo, showTrades])

  useEffect(() => { fetchAndRender() }, [fetchAndRender])

  // ─── Live candle poll ─────────────────────────────────────────────────────
  // Fetches the last 2 candles at TF-appropriate intervals and calls
  // series.update() — lightweight-charts updates the current bar in place
  // or appends a new one when a new period starts. Exactly like TradingView.
  const fetchLiveUpdate = useCallback(async () => {
    if (!candleRef.current || lastCandleTimeRef.current === null || isFetchingRef.current) return
    // Request from last known candle onwards (no backward buffer so we never try
    // to update a non-last bar, which lightweight-charts forbids and throws on).
    const since = lastCandleTimeRef.current * 1000
    const until = Date.now()
    try {
      const res = await axios.get('/api/charts/candles', {
        params: { symbol, timeframe, broker, since, until },
      })
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
        // Keep the view scrolled to the latest bar when updates arrive
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

  // Live WebSocket — real-time candle updates for all brokers
  // Falls back to the REST poll (above) automatically when the WS closes.
  useEffect(() => {
    if (wsRef.current) { wsRef.current.close(); wsRef.current = null }
    setWsLive(false)
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const url = `${proto}//${window.location.host}/ws/kline?symbol=${encodeURIComponent(symbol)}&timeframe=${encodeURIComponent(timeframe)}&broker=${encodeURIComponent(broker)}`
    const ws = new WebSocket(url)
    wsRef.current = ws
    ws.onopen  = () => setWsLive(true)
    ws.onerror = () => setWsLive(false)
    ws.onclose = () => setWsLive(false)
    // Throttle the React state update (setLastUpdated) to at most once per 2s.
    // series.update() is called every message but is a DOM mutation with no
    // React re-render — safe to call at full rate.
    let lastStateUpdate = 0
    ws.onmessage = (evt) => {
      try {
        const d = JSON.parse(evt.data) as { time: number; open: number; high: number; low: number; close: number; volume: number }
        if (!candleRef.current || !d.time || !d.close) return
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
    return () => { ws.close(); setWsLive(false) }
  }, [symbol, timeframe, broker])

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
          <label className="flex items-center gap-1 text-xs cursor-pointer select-none">
            <input type="checkbox" checked={showTrades} onChange={e => setShowTrades(e.target.checked)} className="accent-sky-400" />
            <span className="text-sky-400">Trades</span>
          </label>
        </div>

        {/* MA overlays */}
        <div className="flex items-center gap-1.5 border-l border-dark-600 pl-2">
          {maOverlays.map(m => (
            <span key={m.id} className="flex items-center gap-0.5 text-xs font-medium" style={{ color: m.color }}>
              MA{m.period}
              <button onClick={() => removeMaOverlay(m.id)} className="text-gray-500 hover:text-gray-200 leading-none ml-0.5 text-[10px]">×</button>
            </span>
          ))}
          {maOverlays.length < 6 && (
            <div className="relative">
              <button onClick={() => setShowAddMA(v => !v)}
                className="text-xs px-1.5 py-1 rounded-md bg-dark-600 text-cyan-400 hover:bg-dark-500 hover:text-cyan-300">
                + MA
              </button>
              {showAddMA && (
                <div className="absolute z-50 top-full mt-1 left-0 bg-dark-800 border border-dark-600 rounded-lg p-2 shadow-xl flex flex-col gap-1.5 w-28">
                  <span className="text-xs text-gray-400">Period</span>
                  <input type="number" min={2} max={500} value={newMAPeriod}
                    onChange={e => setNewMAPeriod(Number(e.target.value))}
                    className="bg-dark-700 text-gray-200 text-xs rounded px-2 py-1 border border-dark-500 w-full" />
                  <button onClick={addMaOverlay}
                    className="text-xs py-1 rounded-md bg-cyan-600 text-white hover:bg-cyan-500">Add</button>
                </div>
              )}
            </div>
          )}
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
          <span className="flex items-center gap-1.5"><span className="w-2 h-2 rounded-full bg-green-500" /> BUY</span>
          <span className="flex items-center gap-1.5"><span className="w-2 h-2 rounded-full bg-red-500" /> SELL</span>
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
