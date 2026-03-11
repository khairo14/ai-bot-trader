import { useEffect, useState } from 'react'
import { Plus, Layers, ToggleLeft, ToggleRight, X, BookOpen, Sparkles, Trash2, Pencil } from 'lucide-react'
import { Link } from 'react-router-dom'
import axios from 'axios'
import toast from 'react-hot-toast'
import { SkeletonCard } from '../components/Skeleton'
import { useStrategyRegistry } from '../hooks/useStrategyRegistry'

interface Strategy {
  id: number
  name: string
  description: string
  asset_class: string
  broker: string
  execution_mode: string
  is_active: boolean
  is_paper: boolean
  parameters: Record<string, unknown>
}

const BROKERS = ['binance', 'alpaca', 'ibkr']
const ASSET_CLASSES = ['crypto', 'stock', 'forex', 'option']

// Regex to detect a forex pair like EUR/USD, GBP/JPY, etc.
const FX_SYMBOL_RE = /^[A-Z]{3}\/[A-Z]{3}$/

// Default asset class per broker (IBKR can be stock or forex — resolved by symbol)
const BROKER_ASSET_CLASS: Record<string, string> = {
  binance: 'crypto',
  alpaca:  'stock',
  ibkr:    'stock',
}

function deriveAssetClass(broker: string, symbol: string, currentStratType: string): string {
  if (OPTION_STRATEGIES.has(currentStratType)) return 'option'
  if (broker === 'ibkr' && FX_SYMBOL_RE.test(symbol.trim().toUpperCase())) return 'forex'
  return BROKER_ASSET_CLASS[broker] ?? 'stock'
}

// Option-only strategies always force asset_class = 'option'
const OPTION_STRATEGIES = new Set(['iron_condor', 'covered_call', 'bull_call_spread'])

const TIMEFRAMES = ['1m', '3m', '5m', '15m', '30m', '1h', '2h', '4h', '6h', '12h', '1d', '1w']

const ModeBadge = ({ mode }: { mode: string }) => {
  const colors: Record<string, string> = {
    suggestion: 'bg-blue-900/30 text-blue-400 border-blue-900/50',
    'semi-auto': 'bg-yellow-900/30 text-yellow-400 border-yellow-900/50',
    'full-auto': 'bg-green-900/30 text-green-400 border-green-900/50',
  }
  return (
    <span className={`text-xs px-2 py-0.5 rounded border ${colors[mode] || 'bg-dark-600 text-gray-400 border-dark-500'}`}>
      {mode}
    </span>
  )
}

const defaultForm = {
  name: '',
  description: '',
  strategy_type: 'hybrid_macd_rsi',
  symbol: 'BTC/USDT',
  timeframe: '1h',
  broker: 'binance',
  asset_class: 'crypto',
  execution_mode: 'suggestion',
  is_paper: true,
  trailing_stop_pct: '',
  regime_mode: 'fixed',
}

export default function Strategies() {
  const { brokerStrategies, allStrategies: allStrategyTypes } = useStrategyRegistry()
  const [strategies, setStrategies] = useState<Strategy[]>([])
  const [loadingStrategies, setLoadingStrategies] = useState(true)
  const [showModal, setShowModal] = useState(false)
  const [editTarget, setEditTarget] = useState<Strategy | null>(null)
  const [form, setForm] = useState(defaultForm)
  const [saving, setSaving] = useState(false)
  const [formErrors, setFormErrors] = useState<{ name?: string; symbol?: string }>({})

  const load = () => {
    setLoadingStrategies(true)
    axios.get('/api/strategies/').then(r => setStrategies(r.data.strategies || [])).finally(() => setLoadingStrategies(false))
  }

  useEffect(() => { load() }, [])

  const toggleActive = async (s: Strategy) => {
    try {
      await axios.patch(`/api/strategies/${s.id}`, { is_active: !s.is_active })
      toast.success(`Strategy ${s.is_active ? 'paused' : 'activated'}`)
      load()
    } catch { toast.error('Failed to update strategy') }
  }

  const changeMode = async (s: Strategy, mode: string) => {
    try {
      await axios.patch(`/api/strategies/${s.id}`, { execution_mode: mode })
      toast.success(`Mode changed to ${mode}`)
      load()
    } catch { toast.error('Failed to change mode') }
  }

  const togglePaper = async (s: Strategy) => {
    try {
      await axios.patch(`/api/strategies/${s.id}`, { is_paper: !s.is_paper })
      toast.success(s.is_paper ? 'Switched to Live trading' : 'Switched to Paper trading')
      load()
    } catch { toast.error('Failed to toggle paper mode') }
  }

  const deleteStrategy = (s: Strategy) => {
    toast(
      (t) => (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '8px', minWidth: '220px' }}>
          <p style={{ fontWeight: 600, fontSize: '13px' }}>Delete &quot;{s.name}&quot;?</p>
          <p style={{ fontSize: '12px', color: '#9ca3af' }}>This cannot be undone.</p>
          <div style={{ display: 'flex', gap: '8px', marginTop: '4px' }}>
            <button
              onClick={async () => {
                toast.dismiss(t.id)
                await axios.delete(`/api/strategies/${s.id}`)
                toast.success(`"${s.name}" deleted`)
                load()
              }}
              style={{ flex: 1, padding: '5px 0', background: '#ef4444', color: '#fff', borderRadius: '6px', fontWeight: 700, fontSize: '12px', cursor: 'pointer', border: 'none' }}
            >
              Delete
            </button>
            <button
              onClick={() => toast.dismiss(t.id)}
              style={{ flex: 1, padding: '5px 0', background: '#374151', color: '#d1d5db', borderRadius: '6px', fontSize: '12px', cursor: 'pointer', border: 'none' }}
            >
              Cancel
            </button>
          </div>
        </div>
      ),
      { duration: 10000, icon: null }
    )
  }

  const seedDefaults = async () => {
    try {
      const res = await axios.post('/api/strategies/seed')
      const { created, skipped } = res.data
      toast.success(`Seeded ${created} strategies (${skipped} already existed)`)
      load()
    } catch (e: any) {
      toast.error(e?.response?.data?.detail || 'Seed failed')
    }
  }

  const openEdit = (s: Strategy) => {
    setEditTarget(s)
    setForm({
      name: s.name,
      description: s.description || '',
      strategy_type: (s.parameters?.strategy_type as string) || 'hybrid_macd_rsi',
      symbol: (s.parameters?.symbol as string) || '',
      timeframe: (s.parameters?.timeframe as string) || '1h',
      broker: s.broker,
      asset_class: s.asset_class,
      execution_mode: s.execution_mode,
      is_paper: s.is_paper,
      trailing_stop_pct: s.parameters?.trailing_stop_pct != null ? String(s.parameters.trailing_stop_pct) : '',
      regime_mode: (s.parameters?.regime_mode as string) || 'fixed',
    })
    setFormErrors({})
    setShowModal(true)
  }

  const closeModal = () => {
    setShowModal(false)
    setEditTarget(null)
    setForm(defaultForm)
    setFormErrors({})
  }

  const createStrategy = async () => {
    const errors: { name?: string; symbol?: string } = {}
    if (!form.name.trim()) errors.name = 'Name is required'
    if (!form.symbol.trim()) errors.symbol = 'Symbol is required (e.g. BTC/USDT or EUR/USD)'
    else if (!/^[A-Z0-9]+\/[A-Z0-9]+$|^[A-Z]{1,6}$/.test(form.symbol.trim().toUpperCase()))
      errors.symbol = 'Use format BTC/USDT, EUR/USD (forex) or AAPL (stocks)'
    if (Object.keys(errors).length) { setFormErrors(errors); return }
    setFormErrors({})
    setSaving(true)
    try {
      await axios.post('/api/strategies/', {
        name: form.name.trim(),
        description: form.description.trim() || null,
        asset_class: form.asset_class,
        broker: form.broker,
        execution_mode: form.execution_mode,
        parameters: {
          strategy_type: form.strategy_type,
          symbol: form.symbol.trim().toUpperCase(),
          timeframe: form.timeframe,
          limit: 200,
          regime_mode: form.regime_mode,
          ...(form.trailing_stop_pct !== '' && !isNaN(parseFloat(form.trailing_stop_pct))
            ? { trailing_stop_pct: parseFloat(form.trailing_stop_pct) }
            : {}),
        },
      })
      toast.success('Strategy created')
      closeModal()
      load()
    } catch (e: any) {
      toast.error(e?.response?.data?.detail || 'Failed to create strategy')
    } finally {
      setSaving(false)
    }
  }

  const saveEdit = async () => {
    if (!editTarget) return
    const errors: { name?: string; symbol?: string } = {}
    if (!form.name.trim()) errors.name = 'Name is required'
    if (!form.symbol.trim()) errors.symbol = 'Symbol is required'
    if (Object.keys(errors).length) { setFormErrors(errors); return }
    setFormErrors({})
    setSaving(true)
    try {
      await axios.patch(`/api/strategies/${editTarget.id}`, {
        name: form.name.trim(),
        description: form.description.trim() || null,
        broker: form.broker,
        asset_class: form.asset_class,
        execution_mode: form.execution_mode,
        is_paper: form.is_paper,
        parameters: {
          strategy_type: form.strategy_type,
          symbol: form.symbol.trim().toUpperCase(),
          timeframe: form.timeframe,
          limit: 200,
          regime_mode: form.regime_mode,
          ...(form.trailing_stop_pct !== '' && !isNaN(parseFloat(form.trailing_stop_pct))
            ? { trailing_stop_pct: parseFloat(form.trailing_stop_pct) }
            : {}),
        },
      })
      toast.success('Strategy updated')
      closeModal()
      load()
    } catch (e: any) {
      toast.error(e?.response?.data?.detail || 'Failed to update strategy')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="p-6 space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-bold text-white">Strategies</h1>
          <p className="text-sm text-gray-500 mt-0.5">Manage and configure your trading strategies</p>
        </div>
        <div className="flex items-center gap-2">
          <Link
            to="/strategy-library"
            className="flex items-center gap-1.5 px-3 py-2 bg-dark-700 hover:bg-dark-600 text-gray-300 text-sm rounded-lg transition-all border border-dark-500"
          >
            <BookOpen size={15} /> Library
          </Link>
          <button
            onClick={seedDefaults}
            className="flex items-center gap-1.5 px-3 py-2 bg-dark-700 hover:bg-dark-600 text-gray-300 text-sm rounded-lg transition-all border border-dark-500"
          >
            <Sparkles size={15} /> Seed Defaults
          </button>
          <button
            onClick={() => { setEditTarget(null); setForm(defaultForm); setFormErrors({}); setShowModal(true) }}
            className="flex items-center gap-1.5 px-3 py-2 bg-brand-500 hover:bg-green-400 text-black text-sm font-semibold rounded-lg transition-all"
          >
            <Plus size={16} /> New Strategy
          </button>
        </div>
      </div>

      {loadingStrategies ? (
        <div className="space-y-3">
          <SkeletonCard lines={2} /><SkeletonCard lines={2} /><SkeletonCard lines={2} />
        </div>
      ) : strategies.length === 0 ? (
        <div className="bg-dark-800 border border-dark-600 rounded-xl p-12 text-center">
          <Layers size={40} className="text-gray-600 mx-auto mb-4 opacity-30" />
          <p className="text-gray-500 text-sm">No strategies configured yet.</p>
          <p className="text-gray-600 text-xs mt-1">
            Click <strong className="text-gray-400">Seed Defaults</strong> to load 9 curated strategies, or browse the{' '}
            <Link to="/strategy-library" className="text-brand-500 hover:underline">Strategy Library</Link>.
          </p>
        </div>
      ) : (() => {
        // Group by broker, preserving BROKERS order, then any unknown brokers
        const known = BROKERS.map(b => ({ broker: b, items: strategies.filter(s => s.broker === b) })).filter(g => g.items.length > 0)
        const knownSet = new Set(BROKERS)
        const unknownItems = strategies.filter(s => !knownSet.has(s.broker))
        const groups = unknownItems.length > 0 ? [...known, { broker: 'other', items: unknownItems }] : known

        const brokerAccent: Record<string, string> = {
          binance: 'text-yellow-400',
          alpaca:  'text-blue-400',
          ibkr:    'text-orange-400',
          other:   'text-gray-400',
        }

        return (
          <div className="space-y-6">
            {groups.map(({ broker, items }) => (
              <div key={broker}>
                <div className="flex items-center gap-3 mb-3">
                  <span className={`text-xs font-bold uppercase tracking-widest ${brokerAccent[broker] ?? 'text-gray-400'}`}>
                    {broker}
                  </span>
                  <div className="flex-1 h-px bg-dark-600" />
                  <span className="text-xs text-gray-600">{items.length} {items.length === 1 ? 'strategy' : 'strategies'}</span>
                </div>
                <div className="space-y-3">
                  {items.map(s => (
            <div key={s.id} className="bg-dark-800 border border-dark-600 rounded-xl p-4 flex items-center justify-between">
              <div className="flex items-center gap-4">
                <button onClick={() => toggleActive(s)}>
                  {s.is_active
                    ? <ToggleRight size={28} className="text-brand-500" />
                    : <ToggleLeft size={28} className="text-gray-600" />
                  }
                </button>
                <div>
                  <p className="text-sm font-medium text-white">{s.name}</p>
                  <p className="text-xs text-gray-500">
                    {s.asset_class} · {s.is_paper ? '📄 Paper' : '💰 Live'}
                    {s.parameters?.symbol ? ` · ${s.parameters.symbol}` : ''}
                    {s.parameters?.timeframe ? ` · ${s.parameters.timeframe}` : ''}
                  </p>
                </div>
              </div>
              <div className="flex items-center gap-4">
                {/* Regime Mode pill */}
                {s.parameters?.regime_mode === 'auto_switch' && (
                  <span className="text-xs px-1.5 py-0.5 rounded bg-purple-900/30 text-purple-400 border border-purple-900/50" title="Auto-Switch: strategy swaps based on market regime">
                    Auto-Switch
                  </span>
                )}

                {/* Paper / Live toggle switch */}
                <div className="flex items-center gap-1.5" title={s.is_paper ? 'Paper trading — click to switch to Live' : 'Live trading — click to switch to Paper'}>
                  <span className={`text-xs font-medium ${s.is_paper ? 'text-blue-400' : 'text-yellow-400'}`}>
                    {s.is_paper ? 'Paper' : 'Live'}
                  </span>
                  <button
                    onClick={() => togglePaper(s)}
                    className={`relative w-9 h-5 rounded-full transition-colors duration-200 focus:outline-none ${
                      s.is_paper ? 'bg-blue-600' : 'bg-yellow-500'
                    }`}
                  >
                    <span className={`absolute top-0.5 w-4 h-4 rounded-full bg-white shadow transition-transform duration-200 ${
                      s.is_paper ? 'left-0.5' : 'translate-x-4'
                    }`} />
                  </button>
                </div>
                <ModeBadge mode={s.execution_mode} />
                <select
                  value={s.execution_mode}
                  onChange={e => changeMode(s, e.target.value)}
                  className="bg-dark-700 border border-dark-500 text-xs text-gray-300 rounded px-2 py-1 focus:outline-none"
                >
                  <option value="suggestion">Suggestion</option>
                  <option value="semi-auto">Semi-Auto</option>
                  <option value="full-auto">Full-Auto</option>
                </select>
                <button
                  onClick={() => openEdit(s)}
                  className="p-1.5 text-gray-600 hover:text-blue-400 hover:bg-blue-900/20 rounded transition-colors"
                  title="Edit strategy"
                >
                  <Pencil size={14} />
                </button>
                <button
                  onClick={() => deleteStrategy(s)}
                  className="p-1.5 text-gray-600 hover:text-red-400 hover:bg-red-900/20 rounded transition-colors"
                  title="Delete strategy"
                >
                  <Trash2 size={14} />
                </button>
              </div>
            </div>
          ))}
                </div>
              </div>
            ))}
          </div>
        )
      })()}

      {/* Create / Edit Strategy Modal */}
      {showModal && (
        <div className="fixed inset-0 bg-black/60 backdrop-blur-sm z-50 flex items-center justify-center p-4">
          <div className="bg-dark-800 border border-dark-600 rounded-2xl w-full max-w-md shadow-2xl">
            <div className="flex items-center justify-between p-5 border-b border-dark-600">
              <h2 className="text-base font-semibold text-white">{editTarget ? 'Edit Strategy' : 'New Strategy'}</h2>
              <button onClick={closeModal} className="text-gray-500 hover:text-gray-300 transition-colors">
                <X size={18} />
              </button>
            </div>
            <div className="p-5 space-y-4">
              {/* Name */}
              <div>
                <label className="text-xs text-gray-500 block mb-1">Name *</label>
                <input
                  value={form.name}
                  onChange={e => {
                    setForm(f => ({ ...f, name: e.target.value }))
                    if (formErrors.name) setFormErrors(fe => ({ ...fe, name: undefined }))
                  }}
                  placeholder="e.g. BTC MACD-RSI 1h"
                  className={`w-full bg-dark-700 border rounded-lg px-3 py-2 text-sm text-white placeholder-gray-600 focus:outline-none focus:border-brand-500 ${
                    formErrors.name ? 'border-red-500' : 'border-dark-500'
                  }`}
                />
                {formErrors.name && <p className="text-xs text-red-400 mt-1">{formErrors.name}</p>}
              </div>

              {/* Strategy Type + Symbol */}
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="text-xs text-gray-500 block mb-1">Strategy Type</label>
                  <select value={form.strategy_type} onChange={e => {
                    const st = e.target.value
                    setForm(f => ({
                      ...f,
                      strategy_type: st,
                      // Force asset_class to 'option' for options strategies
                      asset_class: OPTION_STRATEGIES.has(st) ? 'option' : BROKER_ASSET_CLASS[f.broker] ?? f.asset_class,
                    }))
                  }}
                    className="w-full bg-dark-700 border border-dark-500 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-brand-500">
                    {(brokerStrategies[form.broker] ?? allStrategyTypes).map(t => <option key={t} value={t}>{t}</option>)}
                  </select>
                </div>
                <div>
                  <label className="text-xs text-gray-500 block mb-1">Symbol</label>
                  <input
                    value={form.symbol}
                    onChange={e => {
                      const sym = e.target.value
                      setForm(f => ({ ...f, symbol: sym, asset_class: deriveAssetClass(f.broker, sym, f.strategy_type) }))
                      if (formErrors.symbol) setFormErrors(fe => ({ ...fe, symbol: undefined }))
                    }}
                    placeholder="BTC/USDT or EUR/USD"
                    className={`w-full bg-dark-700 border rounded-lg px-3 py-2 text-sm text-white placeholder-gray-600 focus:outline-none focus:border-brand-500 ${
                      formErrors.symbol ? 'border-red-500' : 'border-dark-500'
                    }`}
                  />
                  {formErrors.symbol && <p className="text-xs text-red-400 mt-1">{formErrors.symbol}</p>}
                </div>
              </div>

              {/* Broker + Timeframe */}
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="text-xs text-gray-500 block mb-1">Broker</label>
                  <select value={form.broker} onChange={e => {
                    const broker = e.target.value
                    const validStrats = brokerStrategies[broker] ?? allStrategyTypes
                    const newStratType = validStrats.includes(form.strategy_type)
                      ? form.strategy_type
                      : validStrats[0]
                    setForm(f => ({
                      ...f,
                      broker,
                      asset_class: OPTION_STRATEGIES.has(newStratType) ? 'option' : BROKER_ASSET_CLASS[broker] ?? f.asset_class,
                      strategy_type: newStratType,
                    }))
                  }}
                    className="w-full bg-dark-700 border border-dark-500 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-brand-500">
                    {BROKERS.map(b => <option key={b} value={b}>{b}</option>)}
                  </select>
                </div>
                <div>
                  <label className="text-xs text-gray-500 block mb-1">Timeframe</label>
                  <select value={form.timeframe} onChange={e => setForm(f => ({ ...f, timeframe: e.target.value }))}
                    className="w-full bg-dark-700 border border-dark-500 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-brand-500">
                    {TIMEFRAMES.map(t => <option key={t} value={t}>{t}</option>)}
                  </select>
                </div>
              </div>

              {/* Asset Class + Execution Mode */}
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="text-xs text-gray-500 block mb-1">Asset Class</label>
                  <select value={form.asset_class} onChange={e => setForm(f => ({ ...f, asset_class: e.target.value }))}
                    className="w-full bg-dark-700 border border-dark-500 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-brand-500">
                    {ASSET_CLASSES.map(a => <option key={a} value={a}>{a}</option>)}
                  </select>
                </div>
                <div>
                  <label className="text-xs text-gray-500 block mb-1">Execution Mode</label>
                  <select value={form.execution_mode} onChange={e => setForm(f => ({ ...f, execution_mode: e.target.value }))}
                    className="w-full bg-dark-700 border border-dark-500 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-brand-500">
                    <option value="suggestion">Suggestion</option>
                    <option value="semi-auto">Semi-Auto</option>
                    <option value="full-auto">Full-Auto</option>
                  </select>
                </div>
              </div>

              {/* Paper toggle */}
              <label className="flex items-center gap-3 cursor-pointer select-none">
                <span className="text-sm text-gray-300">Paper trading</span>
                <div
                  onClick={() => setForm(f => ({ ...f, is_paper: !f.is_paper }))}
                  className={`w-10 h-6 rounded-full transition-colors ${form.is_paper ? 'bg-brand-500' : 'bg-dark-600'} relative`}
                >
                  <div className={`absolute top-1 w-4 h-4 rounded-full bg-white transition-transform ${form.is_paper ? 'left-5' : 'left-1'}`} />
                </div>
                <span className="text-xs text-gray-500">{form.is_paper ? 'Paper' : 'Live'}</span>
              </label>

              {/* Regime Mode */}
              <div>
                <label className="text-xs text-gray-500 block mb-1">Regime Mode</label>
                <select
                  value={form.regime_mode}
                  onChange={e => setForm(f => ({ ...f, regime_mode: e.target.value }))}
                  className="w-full bg-dark-700 border border-dark-500 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-brand-500"
                >
                  <option value="fixed">Fixed (Hold on Mismatch)</option>
                  <option value="auto_switch">Auto-Switch</option>
                </select>
                <p className="text-xs text-gray-600 mt-1">
                  Auto-Switch swaps to a compatible strategy when the market regime changes. Fixed holds the signal instead.
                </p>
              </div>

              {/* Trailing Stop */}
              <div>
                <label className="text-xs text-gray-500 block mb-1">
                  Trailing Stop % <span className="text-gray-700">(optional — e.g. 2 = trail by 2%)</span>
                </label>
                <input
                  type="number"
                  min="0.1"
                  step="0.1"
                  value={form.trailing_stop_pct}
                  onChange={e => setForm(f => ({ ...f, trailing_stop_pct: e.target.value }))}
                  placeholder="Disabled"
                  className="w-full bg-dark-700 border border-dark-500 rounded-lg px-3 py-2 text-sm text-white placeholder-gray-600 focus:outline-none focus:border-brand-500"
                />
                {form.trailing_stop_pct !== '' && !isNaN(parseFloat(form.trailing_stop_pct)) && parseFloat(form.trailing_stop_pct) > 0 && (
                  <p className="text-xs text-brand-400 mt-1">
                    Stop trails {form.trailing_stop_pct}% below the highest price reached — locks in profit automatically.
                  </p>
                )}
              </div>
            </div>

            <div className="flex gap-2 p-5 pt-0">
              <button
                onClick={closeModal}
                className="flex-1 px-4 py-2 bg-dark-700 hover:bg-dark-600 text-gray-300 text-sm rounded-lg transition-all"
              >
                Cancel
              </button>
              <button
                onClick={editTarget ? saveEdit : createStrategy}
                disabled={saving}
                className="flex-1 px-4 py-2 bg-brand-500 hover:bg-green-400 text-black text-sm font-semibold rounded-lg transition-all disabled:opacity-50"
              >
                {saving ? (editTarget ? 'Saving…' : 'Creating…') : (editTarget ? 'Save Changes' : 'Create Strategy')}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

