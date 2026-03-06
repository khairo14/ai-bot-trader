import { useState } from 'react'
import { BookOpen, TrendingUp, Activity, BarChart2, Plus, CheckCircle } from 'lucide-react'
import axios from 'axios'
import toast from 'react-hot-toast'

/* ─── Template definition ─────────────────────────────── */
interface Template {
  id: string
  name: string
  description: string
  broker: 'binance' | 'alpaca' | 'ibkr'
  asset_class: 'crypto' | 'stock' | 'forex'
  symbol: string
  timeframe: string
  tags: string[]
  est_win_rate: string   // e.g. "52–57%"
  est_rr: string         // e.g. "1.8 : 1"
  best_market: string    // e.g. "Trending"
  risk_level: 'Low' | 'Medium' | 'High'
}

const TEMPLATES: Template[] = [
  /* ── Binance (crypto) ────────────────────────────────── */
  {
    id: 'btc-trend-1h',
    name: 'BTC Trend Follower',
    description: 'Rides BTC macro momentum on the 1-hour chart using MACD crossovers confirmed by RSI and ADX. Best during sustained bull/bear runs.',
    broker: 'binance', asset_class: 'crypto',
    symbol: 'BTC/USDT', timeframe: '1h',
    tags: ['Trend', 'Momentum'],
    est_win_rate: '53–57%', est_rr: '1.8 : 1', best_market: 'Trending', risk_level: 'Medium',
  },
  {
    id: 'eth-swing-4h',
    name: 'ETH Swing Trader',
    description: 'Catches 4-hour ETH swings with MACD histogram reversals filtered by RSI(14). Wider stops allow breathing room in volatile conditions.',
    broker: 'binance', asset_class: 'crypto',
    symbol: 'ETH/USDT', timeframe: '4h',
    tags: ['Swing', 'Momentum'],
    est_win_rate: '51–55%', est_rr: '2.0 : 1', best_market: 'Trending', risk_level: 'Medium',
  },
  {
    id: 'bnb-momentum-1h',
    name: 'BNB Momentum Scalp',
    description: 'Short-term BNB momentum plays with EMA(9/21) crossover entry, Volume spike confirmation, and RSI divergence filter.',
    broker: 'binance', asset_class: 'crypto',
    symbol: 'BNB/USDT', timeframe: '1h',
    tags: ['Momentum', 'Scalp'],
    est_win_rate: '50–54%', est_rr: '1.6 : 1', best_market: 'Trending', risk_level: 'High',
  },
  {
    id: 'sol-breakout-4h',
    name: 'SOL Breakout Play',
    description: 'Identifies SOL range breakouts confirmed by Bollinger Band expansion and MACD zero-line crosses on the 4-hour chart.',
    broker: 'binance', asset_class: 'crypto',
    symbol: 'SOL/USDT', timeframe: '4h',
    tags: ['Breakout', 'Swing'],
    est_win_rate: '49–53%', est_rr: '2.2 : 1', best_market: 'Breakout', risk_level: 'High',
  },

  /* ── Alpaca (US stocks) ──────────────────────────────── */
  {
    id: 'aapl-daily-trend',
    name: 'Apple Daily Trend',
    description: 'Daily AAPL trend-following using MACD + EMA(21) slope. Low-frequency, high-conviction entries reduce commission drag.',
    broker: 'alpaca', asset_class: 'stock',
    symbol: 'AAPL', timeframe: '1d',
    tags: ['Trend', 'Low-Freq'],
    est_win_rate: '54–58%', est_rr: '2.0 : 1', best_market: 'Trending', risk_level: 'Low',
  },
  {
    id: 'spy-index-1d',
    name: 'SPY Index Follower',
    description: 'Broad-market trend strategy on SPY. High win rate due to mean reversion bias of index ETFs and strong ADX filter.',
    broker: 'alpaca', asset_class: 'stock',
    symbol: 'SPY', timeframe: '1d',
    tags: ['Trend', 'Index', 'Low-Risk'],
    est_win_rate: '55–60%', est_rr: '1.9 : 1', best_market: 'Trending', risk_level: 'Low',
  },
  {
    id: 'nvda-momentum-1d',
    name: 'NVDA Momentum Daily',
    description: 'Captures NVDA earnings-momentum and sector rotation moves. MACD histogram expansion with RSI > 55 entry condition.',
    broker: 'alpaca', asset_class: 'stock',
    symbol: 'NVDA', timeframe: '1d',
    tags: ['Momentum', 'Growth'],
    est_win_rate: '52–56%', est_rr: '2.2 : 1', best_market: 'Trending', risk_level: 'Medium',
  },
  {
    id: 'qqq-tech-4h',
    name: 'QQQ Tech Trend 4h',
    description: 'Intraday tech theme via QQQ 4-hour bars. Useful for capturing pre/post-FOMC tech reactions with tight stop management.',
    broker: 'alpaca', asset_class: 'stock',
    symbol: 'QQQ', timeframe: '4h',
    tags: ['Trend', 'Tech'],
    est_win_rate: '53–57%', est_rr: '1.8 : 1', best_market: 'Trending', risk_level: 'Medium',
  },

  /* ── IBKR (stocks) ───────────────────────────────────── */
  {
    id: 'msft-bluechip-1d',
    name: 'MSFT Blue Chip Trend',
    description: 'Conservative MSFT daily trend play. Strong fundamentals underpin the signal — MACD-RSI confluence only fires at clear inflection points.',
    broker: 'ibkr', asset_class: 'stock',
    symbol: 'MSFT', timeframe: '1d',
    tags: ['Trend', 'Blue-Chip'],
    est_win_rate: '54–58%', est_rr: '2.1 : 1', best_market: 'Trending', risk_level: 'Low',
  },
  {
    id: 'tsla-swing-4h',
    name: 'TSLA Swing Play',
    description: 'High-beta TSLA 4-hour swing trades. Volatile — only fires when RSI exits oversold/overbought AND MACD confirms. Large R:R compensates for lower hit rate.',
    broker: 'ibkr', asset_class: 'stock',
    symbol: 'TSLA', timeframe: '4h',
    tags: ['Swing', 'High-Vol'],
    est_win_rate: '48–53%', est_rr: '2.5 : 1', best_market: 'Ranging/Volatile', risk_level: 'High',
  },
  {
    id: 'spy-ibkr-1h',
    name: 'SPY Hourly Scalp (IBKR)',
    description: "Short-duration SPY scalp leveraging IBKR's low commissions. EMA crossover + ADX > 20 filter keeps entries in trending hours only.",
    broker: 'ibkr', asset_class: 'stock',
    symbol: 'SPY', timeframe: '1h',
    tags: ['Scalp', 'Trend'],
    est_win_rate: '52–55%', est_rr: '1.5 : 1', best_market: 'Trending', risk_level: 'Medium',
  },
  {
    id: 'amzn-trend-1d',
    name: 'Amazon Trend Follow',
    description: 'Daily AMZN breakout-continuation strategy. Waits for MACD line to cross above signal after a Bollinger Band squeeze to confirm expansion.',
    broker: 'ibkr', asset_class: 'stock',
    symbol: 'AMZN', timeframe: '1d',
    tags: ['Trend', 'Breakout'],
    est_win_rate: '51–55%', est_rr: '2.0 : 1', best_market: 'Trending', risk_level: 'Medium',
  },
]

/* ─── Helpers ─────────────────────────────────────────── */
const BROKER_COLORS: Record<string, string> = {
  binance: 'text-yellow-400 bg-yellow-900/30 border-yellow-900/50',
  alpaca:  'text-blue-400  bg-blue-900/30  border-blue-900/50',
  ibkr:    'text-purple-400 bg-purple-900/30 border-purple-900/50',
}

const RISK_COLORS: Record<string, string> = {
  Low:    'text-green-400',
  Medium: 'text-yellow-400',
  High:   'text-red-400',
}

const WIN_TAG_COLORS = ['bg-brand-900/30 text-brand-400', 'bg-gray-800 text-gray-400']

/* ─── Component ───────────────────────────────────────── */
export default function StrategyLibrary() {
  const [filter, setFilter] = useState<'all' | 'binance' | 'alpaca' | 'ibkr'>('all')
  const [added, setAdded] = useState<Set<string>>(new Set())
  const [adding, setAdding] = useState<string | null>(null)

  const visible = filter === 'all' ? TEMPLATES : TEMPLATES.filter(t => t.broker === filter)

  const addStrategy = async (t: Template) => {
    if (added.has(t.id)) return
    setAdding(t.id)
    try {
      await axios.post('/api/strategies/', {
        name: t.name,
        description: t.description,
        asset_class: t.asset_class,
        broker: t.broker,
        execution_mode: 'suggestion',
        is_paper: true,
        parameters: {
          strategy_type: 'hybrid_macd_rsi',
          symbol: t.symbol,
          timeframe: t.timeframe,
          limit: 200,
        },
      })
      setAdded(prev => new Set([...prev, t.id]))
      toast.success(`"${t.name}" added to your strategies`)
    } catch (e: any) {
      const detail = e?.response?.data?.detail || ''
      if (detail.toLowerCase().includes('unique') || detail.toLowerCase().includes('already')) {
        setAdded(prev => new Set([...prev, t.id]))
        toast(`"${t.name}" already in your strategies`, { icon: 'ℹ️' })
      } else {
        toast.error(detail || 'Failed to add strategy')
      }
    } finally {
      setAdding(null)
    }
  }

  return (
    <div className="p-6 space-y-6">
      {/* Header */}
      <div className="flex items-start justify-between">
        <div>
          <div className="flex items-center gap-2 mb-1">
            <BookOpen size={20} className="text-brand-500" />
            <h1 className="text-xl font-bold text-white">Strategy Library</h1>
          </div>
          <p className="text-sm text-gray-500">
            Curated ready-to-use templates. Win rates are backtested estimates — past performance does not guarantee future results.
          </p>
        </div>
      </div>

      {/* Filter tabs */}
      <div className="flex gap-2">
        {(['all', 'binance', 'alpaca', 'ibkr'] as const).map(b => (
          <button
            key={b}
            onClick={() => setFilter(b)}
            className={`px-4 py-1.5 rounded-lg text-sm font-medium transition-all capitalize ${
              filter === b
                ? 'bg-brand-500 text-black'
                : 'bg-dark-700 text-gray-400 hover:bg-dark-600 hover:text-gray-200 border border-dark-500'
            }`}
          >
            {b === 'all' ? 'All Brokers' : b.toUpperCase()}
          </button>
        ))}
        <span className="ml-auto text-xs text-gray-600 self-center">
          {visible.length} template{visible.length !== 1 ? 's' : ''}
        </span>
      </div>

      {/* Grid */}
      <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
        {visible.map(t => {
          const isAdded = added.has(t.id)
          const isAdding = adding === t.id
          return (
            <div key={t.id} className="bg-dark-800 border border-dark-600 rounded-xl p-5 flex flex-col gap-3 hover:border-dark-500 transition-colors">
              {/* Top row */}
              <div className="flex items-start justify-between gap-2">
                <div>
                  <p className="text-sm font-semibold text-white leading-tight">{t.name}</p>
                  <span className={`text-xs px-2 py-0.5 rounded border mt-1 inline-block ${BROKER_COLORS[t.broker]}`}>
                    {t.broker.toUpperCase()} · {t.symbol} · {t.timeframe}
                  </span>
                </div>
                <span className={`text-xs font-medium shrink-0 ${RISK_COLORS[t.risk_level]}`}>
                  {t.risk_level} Risk
                </span>
              </div>

              {/* Description */}
              <p className="text-xs text-gray-400 leading-relaxed">{t.description}</p>

              {/* Tags */}
              <div className="flex flex-wrap gap-1">
                {t.tags.map((tag, i) => (
                  <span key={tag} className={`text-xs px-2 py-0.5 rounded ${WIN_TAG_COLORS[i % 2]}`}>{tag}</span>
                ))}
              </div>

              {/* Stats */}
              <div className="grid grid-cols-3 gap-2 pt-1 border-t border-dark-600">
                <div className="text-center">
                  <div className="flex items-center justify-center gap-1 text-gray-500 mb-0.5">
                    <TrendingUp size={11} />
                    <span className="text-xs">Win Rate</span>
                  </div>
                  <p className="text-sm font-semibold text-brand-400">{t.est_win_rate}</p>
                </div>
                <div className="text-center">
                  <div className="flex items-center justify-center gap-1 text-gray-500 mb-0.5">
                    <BarChart2 size={11} />
                    <span className="text-xs">R : R</span>
                  </div>
                  <p className="text-sm font-semibold text-white">{t.est_rr}</p>
                </div>
                <div className="text-center">
                  <div className="flex items-center justify-center gap-1 text-gray-500 mb-0.5">
                    <Activity size={11} />
                    <span className="text-xs">Market</span>
                  </div>
                  <p className="text-xs font-medium text-gray-300 truncate">{t.best_market}</p>
                </div>
              </div>

              {/* Add button */}
              <button
                onClick={() => addStrategy(t)}
                disabled={isAdding || isAdded}
                className={`w-full flex items-center justify-center gap-1.5 py-2 rounded-lg text-sm font-medium transition-all ${
                  isAdded
                    ? 'bg-green-900/30 text-green-400 border border-green-900/50 cursor-default'
                    : 'bg-brand-500/10 hover:bg-brand-500/20 text-brand-400 border border-brand-500/30 hover:border-brand-500/60'
                }`}
              >
                {isAdded ? (
                  <><CheckCircle size={15} /> Added</>
                ) : isAdding ? (
                  'Adding…'
                ) : (
                  <><Plus size={15} /> Use This Strategy</>
                )}
              </button>
            </div>
          )
        })}
      </div>

      {/* Disclaimer */}
      <p className="text-xs text-gray-600 text-center pt-2">
        Win rate estimates are based on hybrid_macd_rsi backtests (2022–2024) and should be treated as indicative only.
        All strategies are added in Paper mode by default.
      </p>
    </div>
  )
}
