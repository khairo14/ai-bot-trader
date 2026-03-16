import { useEffect, useState } from 'react'
import { Timer, Zap, Settings, RefreshCw, ToggleLeft, ToggleRight } from 'lucide-react'
import axios from 'axios'
import toast from 'react-hot-toast'
import ScalpSignalFeed from '../components/ScalpSignalFeed'
import { SkeletonStat } from '../components/Skeleton'

interface ScalpStats {
  period_days: number
  total_outcomes: number
  wins: number
  losses: number
  win_rate_pct: number
  avg_pnl_pct: number
  avg_hold_candles: number
  signals_today: number
}

interface ScalpSettings {
  enabled: boolean
  timeframe: string
  risk_per_trade_pct: number
  sl_atr_mult: number
  tp_atr_mult: number
  min_volume_ratio: number
  max_spread_pct: number
  min_score: number
  ml_veto_threshold: number
  sr_tp_snap: boolean
}

export default function Scalping() {
  const [stats, setStats] = useState<ScalpStats | null>(null)
  const [settings, setSettings] = useState<ScalpSettings | null>(null)
  const [loadingStats, setLoadingStats] = useState(true)
  const [togglingEnabled, setTogglingEnabled] = useState(false)
  const [showSettings, setShowSettings] = useState(false)
  const [savingSettings, setSavingSettings] = useState(false)
  const [form, setForm] = useState<Partial<ScalpSettings>>({})

  const fetchAll = async () => {
    setLoadingStats(true)
    const [statsRes, settingsRes] = await Promise.allSettled([
      axios.get('/api/scalping/stats'),
      axios.get('/api/scalping/settings'),
    ])
    if (statsRes.status === 'fulfilled') setStats(statsRes.value.data)
    if (settingsRes.status === 'fulfilled') {
      setSettings(settingsRes.value.data)
      setForm(settingsRes.value.data)
    }
    setLoadingStats(false)
  }

  useEffect(() => { fetchAll() }, [])

  const toggleEnabled = async () => {
    if (!settings) return
    setTogglingEnabled(true)
    try {
      const next = !settings.enabled
      await axios.post('/api/scalping/enable', { enabled: next })
      toast.success(`Scalping ${next ? 'enabled' : 'disabled'}`)
      setSettings(s => s ? { ...s, enabled: next } : s)
      setForm(f => ({ ...f, enabled: next }))
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } } }
      toast.error(err?.response?.data?.detail || 'Failed to toggle')
    } finally {
      setTogglingEnabled(false)
    }
  }

  const saveSettings = async () => {
    setSavingSettings(true)
    try {
      const res = await axios.post('/api/scalping/settings', form)
      setSettings(res.data.settings)
      setForm(res.data.settings)
      toast.success('Settings saved')
      setShowSettings(false)
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: unknown } } }
      const detail = err?.response?.data?.detail
      toast.error(typeof detail === 'string' ? detail : 'Failed to save settings')
    } finally {
      setSavingSettings(false)
    }
  }

  const enabled = settings?.enabled ?? true

  return (
    <div className="p-6 space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <div className="flex items-center gap-2">
            <Timer size={18} className="text-brand-400" />
            <h1 className="text-xl font-bold text-white">Scalping</h1>
            <span className={`text-xs px-2 py-0.5 rounded-full border ${
              enabled
                ? 'bg-green-900/20 text-green-400 border-green-900/50'
                : 'bg-dark-700 text-gray-500 border-dark-600'
            }`}>{enabled ? 'Active' : 'Disabled'}</span>
          </div>
          <p className="text-sm text-gray-500 mt-0.5">EMA ribbon 8/13/21 · VWAP · ATR · 1m–5m timeframes</p>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={fetchAll}
            title="Refresh stats"
            className="p-2 rounded-lg bg-dark-700 hover:bg-dark-600 text-gray-400 hover:text-gray-200 transition-all border border-dark-600"
          >
            <RefreshCw size={14} className={loadingStats ? 'animate-spin' : ''} />
          </button>
          <button
            onClick={() => setShowSettings(v => !v)}
            className={`flex items-center gap-1.5 px-3 py-2 rounded-lg text-sm transition-all border ${
              showSettings
                ? 'bg-brand-500/10 text-brand-400 border-brand-500/40'
                : 'bg-dark-700 text-gray-400 hover:text-gray-200 border-dark-600'
            }`}
          >
            <Settings size={14} /> Settings
          </button>
          <button
            disabled={togglingEnabled}
            onClick={toggleEnabled}
            className={`flex items-center gap-2 px-3 py-2 rounded-lg text-sm font-medium transition-all border disabled:opacity-50 ${
              enabled
                ? 'bg-green-900/20 text-green-400 hover:bg-green-900/40 border-green-900/50'
                : 'bg-dark-700 text-gray-400 hover:bg-dark-600 border-dark-600'
            }`}
          >
            {togglingEnabled
              ? <RefreshCw size={14} className="animate-spin" />
              : enabled ? <ToggleRight size={16} /> : <ToggleLeft size={16} />
            }
            {enabled ? 'Enabled' : 'Disabled'}
          </button>
        </div>
      </div>

      {/* Stats Row */}
      {loadingStats ? (
        <div className="grid grid-cols-4 gap-4">
          <SkeletonStat /><SkeletonStat /><SkeletonStat /><SkeletonStat />
        </div>
      ) : (
        <div className="grid grid-cols-4 gap-4">
          <div className="bg-dark-800 border border-dark-600 rounded-xl p-4">
            <p className="text-xs text-gray-500 uppercase tracking-wider mb-1">Win Rate</p>
            <p className={`text-2xl font-bold ${
              !stats ? 'text-gray-600'
              : stats.win_rate_pct >= 55 ? 'text-green-400'
              : stats.win_rate_pct >= 45 ? 'text-yellow-400'
              : 'text-red-400'
            }`}>{stats && stats.total_outcomes > 0 ? `${stats.win_rate_pct}%` : '—'}</p>
            <p className="text-xs text-gray-500 mt-0.5">
              {stats && stats.total_outcomes > 0
                ? `${stats.wins}W / ${stats.losses}L · ${stats.period_days}d`
                : 'No resolved trades yet'}
            </p>
          </div>

          <div className="bg-dark-800 border border-dark-600 rounded-xl p-4">
            <p className="text-xs text-gray-500 uppercase tracking-wider mb-1">Avg P&L</p>
            <p className={`text-2xl font-bold ${
              !stats || stats.total_outcomes === 0 ? 'text-gray-600'
              : stats.avg_pnl_pct > 0 ? 'text-green-400'
              : stats.avg_pnl_pct < 0 ? 'text-red-400'
              : 'text-gray-500'
            }`}>
              {stats && stats.total_outcomes > 0
                ? `${stats.avg_pnl_pct > 0 ? '+' : ''}${stats.avg_pnl_pct}%`
                : '—'}
            </p>
            <p className="text-xs text-gray-500 mt-0.5">per resolved trade</p>
          </div>

          <div className="bg-dark-800 border border-dark-600 rounded-xl p-4">
            <p className="text-xs text-gray-500 uppercase tracking-wider mb-1">Avg Hold</p>
            <p className="text-2xl font-bold text-white">
              {stats && stats.total_outcomes > 0 ? `${stats.avg_hold_candles}` : '—'}
            </p>
            <p className="text-xs text-gray-500 mt-0.5">
              candles{settings ? ` · ${settings.timeframe}` : ''}
            </p>
          </div>

          <div className="bg-dark-800 border border-dark-600 rounded-xl p-4">
            <p className="text-xs text-gray-500 uppercase tracking-wider mb-1">Today's Signals</p>
            <p className="text-2xl font-bold text-white">{stats?.signals_today ?? '—'}</p>
            <p className="text-xs text-gray-500 mt-0.5">
              {stats ? `${stats.total_outcomes} resolved total` : ''}
            </p>
          </div>
        </div>
      )}

      {/* Settings Panel */}
      {showSettings && settings && (
        <div className="bg-dark-800 border border-dark-600 rounded-xl p-5 space-y-4">
          <h2 className="text-sm font-semibold text-gray-300">Scalping Settings</h2>
          <div className="grid grid-cols-3 gap-4">
            <div>
              <label className="text-xs text-gray-500 block mb-1">Timeframe</label>
              <select
                value={form.timeframe ?? settings.timeframe}
                onChange={e => setForm(f => ({ ...f, timeframe: e.target.value }))}
                className="w-full bg-dark-700 border border-dark-500 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-brand-500"
              >
                {['1m', '3m', '5m', '15m', '30m'].map(t => <option key={t} value={t}>{t}</option>)}
              </select>
            </div>

            <div>
              <label className="text-xs text-gray-500 block mb-1">Risk per Trade (%)</label>
              <input
                type="number" step="0.1" min="0.1" max="5"
                value={form.risk_per_trade_pct ?? settings.risk_per_trade_pct}
                onChange={e => setForm(f => ({ ...f, risk_per_trade_pct: parseFloat(e.target.value) }))}
                className="w-full bg-dark-700 border border-dark-500 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-brand-500"
              />
            </div>

            <div>
              <label className="text-xs text-gray-500 block mb-1">Min Score (0–8)</label>
              <input
                type="number" step="1" min="0" max="8"
                value={form.min_score ?? settings.min_score}
                onChange={e => setForm(f => ({ ...f, min_score: parseInt(e.target.value) }))}
                className="w-full bg-dark-700 border border-dark-500 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-brand-500"
              />
            </div>

            <div>
              <label className="text-xs text-gray-500 block mb-1">SL ATR Multiplier</label>
              <input
                type="number" step="0.1"
                value={form.sl_atr_mult ?? settings.sl_atr_mult}
                onChange={e => setForm(f => ({ ...f, sl_atr_mult: parseFloat(e.target.value) }))}
                className="w-full bg-dark-700 border border-dark-500 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-brand-500"
              />
            </div>

            <div>
              <label className="text-xs text-gray-500 block mb-1">TP ATR Multiplier</label>
              <input
                type="number" step="0.1"
                value={form.tp_atr_mult ?? settings.tp_atr_mult}
                onChange={e => setForm(f => ({ ...f, tp_atr_mult: parseFloat(e.target.value) }))}
                className="w-full bg-dark-700 border border-dark-500 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-brand-500"
              />
            </div>

            <div>
              <label className="text-xs text-gray-500 block mb-1">ML Veto Threshold</label>
              <input
                type="number" step="0.01" min="0.20" max="0.70"
                value={form.ml_veto_threshold ?? settings.ml_veto_threshold}
                onChange={e => setForm(f => ({ ...f, ml_veto_threshold: parseFloat(e.target.value) }))}
                className="w-full bg-dark-700 border border-dark-500 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-brand-500"
              />
            </div>

            <div>
              <label className="text-xs text-gray-500 block mb-1">Min Volume Ratio</label>
              <input
                type="number" step="0.1"
                value={form.min_volume_ratio ?? settings.min_volume_ratio}
                onChange={e => setForm(f => ({ ...f, min_volume_ratio: parseFloat(e.target.value) }))}
                className="w-full bg-dark-700 border border-dark-500 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-brand-500"
              />
            </div>

            <div>
              <label className="text-xs text-gray-500 block mb-1">Max Spread (%)</label>
              <input
                type="number" step="0.001"
                value={form.max_spread_pct ?? settings.max_spread_pct}
                onChange={e => setForm(f => ({ ...f, max_spread_pct: parseFloat(e.target.value) }))}
                className="w-full bg-dark-700 border border-dark-500 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-brand-500"
              />
            </div>

            <div className="flex items-center gap-3 pt-4">
              <label className="text-xs text-gray-500">S/R TP Snap</label>
              <button
                type="button"
                onClick={() => setForm(f => ({ ...f, sr_tp_snap: !f.sr_tp_snap }))}
                title="Snap take-profit to nearest support/resistance level"
              >
                {form.sr_tp_snap
                  ? <ToggleRight size={22} className="text-brand-500" />
                  : <ToggleLeft size={22} className="text-gray-600" />
                }
              </button>
            </div>
          </div>

          <div className="flex gap-2 pt-2">
            <button
              onClick={saveSettings}
              disabled={savingSettings}
              className="flex items-center gap-1.5 px-4 py-2 bg-brand-500 hover:bg-green-400 text-black text-sm font-semibold rounded-lg transition-all disabled:opacity-50"
            >
              {savingSettings && <RefreshCw size={14} className="animate-spin" />}
              Save Settings
            </button>
            <button
              onClick={() => { setForm(settings); setShowSettings(false) }}
              className="px-4 py-2 bg-dark-700 hover:bg-dark-600 text-gray-300 text-sm rounded-lg transition-all border border-dark-600"
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {/* Live Signal Feed */}
      <div>
        <div className="flex items-center gap-2 mb-3">
          <Zap size={14} className="text-brand-400" />
          <h2 className="text-sm font-semibold text-gray-300">Live Signal Feed</h2>
          <span className="text-xs text-gray-600">/ws/scalping</span>
        </div>
        <ScalpSignalFeed />
      </div>
    </div>
  )
}
