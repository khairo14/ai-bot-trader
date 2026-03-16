import { useState, useEffect } from 'react'
import { CheckCircle, XCircle, AlertTriangle, RefreshCw, ShieldAlert, ChevronDown, ChevronUp, Brain } from 'lucide-react'
import axios from 'axios'
import toast from 'react-hot-toast'
import { parseUtc } from '../lib/dates'

interface BrokerStatus {
  name: string
  connected: boolean
  paper: boolean
  total?: number
  currency?: string
}

interface BrokerRiskSettings {
  broker: string
  risk_per_trade_pct: number | null
  max_open_positions: number | null
  daily_circuit_breaker_pct: number | null
  max_consecutive_losses: number | null
  max_exposure_per_asset_pct: number | null
  max_exposure_per_class_pct: number | null
  updated_at: string | null
}

interface BrokerRiskState {
  circuit_breaker_active: boolean
  consecutive_losses: number
}

interface StrategyRiskState {
  circuit_breaker_active: boolean
  consecutive_losses: number
  circuit_breaker_date: string | null
  max_consecutive_losses_effective?: number
}

interface MLModelInfo {
  symbol: string
  model_path: string
  trained_date: string | null
}

interface MLStatus {
  model_count: number
  models: MLModelInfo[]
  last_retrain: string | null
  outcomes_total: number
  outcomes_resolved: number
  outcomes_pending: number
  win_rate_pct: number | null
  avg_pnl_pct: number | null
  feedback_loop_active: boolean
}

const GLOBAL_DEFAULTS = {
  risk_per_trade_pct: 2.0,
  max_open_positions: 5,
  daily_circuit_breaker_pct: 5.0,
  max_consecutive_losses: 3,
  max_exposure_per_asset_pct: 15.0,
  max_exposure_per_class_pct: 40.0,
}

const BROKERS = ['binance', 'alpaca', 'ibkr'] as const

const StatusIcon = ({ ok }: { ok: boolean }) =>
  ok
    ? <CheckCircle size={16} className="text-brand-500" />
    : <XCircle size={16} className="text-red-500" />

const Toggle = ({ checked, onChange, disabled }: { checked: boolean; onChange: () => void; disabled?: boolean }) => (
  <button
    onClick={onChange}
    disabled={disabled}
    className={`relative w-10 h-5 rounded-full transition-colors duration-200 focus:outline-none disabled:opacity-40 ${
      checked ? 'bg-blue-600' : 'bg-yellow-500'
    }`}
  >
    <span className={`absolute top-0.5 w-4 h-4 rounded-full bg-white shadow transition-transform duration-200 ${
      checked ? 'left-0.5' : 'translate-x-5'
    }`} />
  </button>
)

function RiskField({
  label, value, placeholder, step = '0.1', min, max, onChange,
}: {
  label: string; value: string; placeholder: string; step?: string; min: string; max: string;
  onChange: (v: string) => void
}) {
  return (
    <div>
      <label className="text-xs text-gray-400 block mb-1">
        {label} <span className="text-gray-600">(blank = global default: {placeholder})</span>
      </label>
      <input
        type="number" step={step} min={min} max={max} value={value}
        placeholder={placeholder}
        onChange={e => onChange(e.target.value)}
        className="w-full bg-dark-700 border border-dark-500 text-white text-sm rounded-lg px-3 py-2 focus:outline-none focus:border-brand-500 placeholder-gray-600"
      />
    </div>
  )
}

export default function Settings() {
  const [brokers, setBrokers] = useState<BrokerStatus[]>([
    { name: 'binance', connected: false, paper: true },
    { name: 'alpaca', connected: false, paper: true },
    { name: 'ibkr', connected: false, paper: false },
  ])
  const [modes, setModes] = useState<Record<string, string>>({})
  const [togglingBroker, setTogglingBroker] = useState<string | null>(null)

  // Per-broker risk settings (string values for controlled inputs; empty = null/use global)
  type FieldMap = Record<string, string>
  const emptyFields = (): FieldMap => ({
    risk_per_trade_pct: '', max_open_positions: '', daily_circuit_breaker_pct: '',
    max_consecutive_losses: '', max_exposure_per_asset_pct: '', max_exposure_per_class_pct: '',
  })
  const [brokerRisk, setBrokerRisk] = useState<Record<string, FieldMap>>({
    binance: emptyFields(), alpaca: emptyFields(), ibkr: emptyFields(),
  })
  const [brokerState, setBrokerState] = useState<Record<string, BrokerRiskState>>({})
  const [strategyState, setStrategyState] = useState<Record<string, StrategyRiskState>>({})
  const [savingBroker, setSavingBroker] = useState<string | null>(null)
  const [expandedBroker, setExpandedBroker] = useState<string | null>('binance')
  const [mlStatus, setMlStatus] = useState<MLStatus | null>(null)
  const [retraining, setRetraining] = useState(false)
  const [regimeEnabled, setRegimeEnabled] = useState(true)
  const [regimeHysteresis, setRegimeHysteresis] = useState('3')
  const [savingRegime, setSavingRegime] = useState(false)

  const loadAll = () => {
    axios.get('/api/portfolio/summary')
      .then(res => {
        const apibrokers: Array<{ broker: string; connected: boolean; is_paper: boolean; total: number; currency: string }> = res.data.brokers || []
        setBrokers(prev => prev.map(b => {
          const found = apibrokers.find(a => a.broker === b.name)
          if (!found) return b
          return { ...b, connected: found.connected, paper: found.is_paper, total: found.total, currency: found.currency }
        }))
      })
      .catch(() => {})
    axios.get('/api/brokers/modes')
      .then(res => setModes(res.data))
      .catch(() => {})
    // Load per-broker risk settings
    axios.get('/api/risk/broker-settings')
      .then(res => {
        const rows: BrokerRiskSettings[] = res.data
        const next: Record<string, FieldMap> = { binance: emptyFields(), alpaca: emptyFields(), ibkr: emptyFields() }
        rows.forEach(r => {
          next[r.broker] = {
            risk_per_trade_pct:        r.risk_per_trade_pct        != null ? String(r.risk_per_trade_pct)        : '',
            max_open_positions:        r.max_open_positions         != null ? String(r.max_open_positions)         : '',
            daily_circuit_breaker_pct: r.daily_circuit_breaker_pct  != null ? String(r.daily_circuit_breaker_pct)  : '',
            max_consecutive_losses:    r.max_consecutive_losses     != null ? String(r.max_consecutive_losses)     : '',
            max_exposure_per_asset_pct:r.max_exposure_per_asset_pct != null ? String(r.max_exposure_per_asset_pct) : '',
            max_exposure_per_class_pct:r.max_exposure_per_class_pct != null ? String(r.max_exposure_per_class_pct) : '',
          }
        })
        setBrokerRisk(next)
      })
      .catch(() => {})
    // Load per-broker runtime state (circuit breaker / consecutive losses)
    axios.get('/api/risk/status')
      .then(res => setBrokerState(res.data.per_broker || {}))
      .catch(() => {})
    // Load per-strategy CB state
    axios.get('/api/risk/strategy/status')
      .then(res => setStrategyState(res.data || {}))
      .catch(() => {})
    // Load ML status
    axios.get('/api/ml/status')
      .then(res => setMlStatus(res.data))
      .catch(() => {})
    // Load regime router settings
    axios.get('/api/settings/regime')
      .then(res => {
        setRegimeEnabled(res.data.enabled ?? true)
        setRegimeHysteresis(String(res.data.hysteresis_candles ?? 3))
      })
      .catch(() => {})
  }

  const retrainNow = async () => {
    setRetraining(true)
    try {
      const res = await axios.post('/api/ml/retrain')
      if (res.data.status === 'queued') {
        toast.success('ML retraining queued — runs in background. Results in ~2–5 min.')
      } else {
        toast.error(res.data.detail || 'Retrain request failed')
      }
    } catch (e: any) {
      toast.error(e?.response?.data?.detail || 'Retrain request failed')
    } finally {
      setRetraining(false)
    }
  }

  useEffect(() => { loadAll() }, [])

  const saveRegimeSettings = async () => {
    const n = parseInt(regimeHysteresis)
    if (isNaN(n) || n < 1 || n > 20) {
      toast.error('Hysteresis must be between 1 and 20 candles')
      return
    }
    setSavingRegime(true)
    try {
      await axios.put('/api/settings/regime', { enabled: regimeEnabled, hysteresis_candles: n })
      toast.success('Regime router settings saved')
    } catch (e: any) {
      toast.error(e?.response?.data?.detail || 'Save failed')
    } finally {
      setSavingRegime(false)
    }
  }

  const toggleBrokerMode = (broker: string, currentlyPaper: boolean) => {
    const nextMode = currentlyPaper ? 'live' : 'paper'
    if (nextMode === 'live') {
      toast(
        (t) => (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '8px', minWidth: '240px' }}>
            <p style={{ fontWeight: 700, fontSize: '13px', color: '#fbbf24' }}>
              [!] Switch {broker.toUpperCase()} to LIVE mode?
            </p>
            <p style={{ fontSize: '12px', color: '#9ca3af', lineHeight: 1.4 }}>
              Real orders will be placed using live API keys.
            </p>
            <div style={{ display: 'flex', gap: '8px', marginTop: '4px' }}>
              <button onClick={() => { toast.dismiss(t.id); doSwitch(broker, nextMode) }}
                style={{ flex: 1, padding: '5px 0', background: '#eab308', color: '#000', borderRadius: '6px', fontWeight: 700, fontSize: '12px', cursor: 'pointer', border: 'none' }}>
                Yes, go Live
              </button>
              <button onClick={() => toast.dismiss(t.id)}
                style={{ flex: 1, padding: '5px 0', background: '#374151', color: '#d1d5db', borderRadius: '6px', fontSize: '12px', cursor: 'pointer', border: 'none' }}>
                Cancel
              </button>
            </div>
          </div>
        ),
        { duration: 15000, icon: null }
      )
      return
    }
    doSwitch(broker, nextMode)
  }

  const doSwitch = async (broker: string, nextMode: string) => {
    setTogglingBroker(broker)
    try {
      await axios.post(`/api/brokers/${broker}/mode`, { mode: nextMode })
      setModes(prev => ({ ...prev, [broker]: nextMode }))
      toast.success(`${broker.toUpperCase()} switched to ${nextMode} mode`)
    } catch (e: any) {
      toast.error(e?.response?.data?.detail || 'Mode switch failed')
    } finally {
      setTogglingBroker(null)
    }
  }

  const saveBrokerRisk = async (broker: string) => {
    setSavingBroker(broker)
    const f = brokerRisk[broker]
    const body = {
      risk_per_trade_pct:        f.risk_per_trade_pct        !== '' ? parseFloat(f.risk_per_trade_pct)        : null,
      max_open_positions:        f.max_open_positions         !== '' ? parseInt(f.max_open_positions)          : null,
      daily_circuit_breaker_pct: f.daily_circuit_breaker_pct  !== '' ? parseFloat(f.daily_circuit_breaker_pct) : null,
      max_consecutive_losses:    f.max_consecutive_losses     !== '' ? parseInt(f.max_consecutive_losses)      : null,
      max_exposure_per_asset_pct:f.max_exposure_per_asset_pct !== '' ? parseFloat(f.max_exposure_per_asset_pct): null,
      max_exposure_per_class_pct:f.max_exposure_per_class_pct !== '' ? parseFloat(f.max_exposure_per_class_pct): null,
    }
    try {
      await axios.put(`/api/risk/broker-settings/${broker}`, body)
      toast.success(`${broker.toUpperCase()} risk settings saved`)
    } catch (e: any) {
      toast.error(e?.response?.data?.detail || 'Save failed')
    } finally {
      setSavingBroker(null)
    }
  }

  const resetBrokerCB = async (broker: string) => {
    try {
      await axios.post(`/api/risk/${broker}/reset-circuit-breaker`)
      toast.success(`${broker.toUpperCase()} circuit breaker reset`)
      loadAll()
    } catch { toast.error('Reset failed') }
  }

  const resetBrokerLosses = async (broker: string) => {
    try {
      await axios.post(`/api/risk/${broker}/reset-consecutive-losses`)
      toast.success(`${broker.toUpperCase()} loss counter reset`)
      loadAll()
    } catch { toast.error('Reset failed') }
  }

  const resetGlobalCB = async () => {
    try {
      await axios.post('/api/risk/reset-circuit-breaker')
      toast.success('Global circuit breaker reset')
      loadAll()
    } catch { toast.error('Reset failed') }
  }

  const resetStrategyCB = async (name: string) => {
    try {
      await axios.post(`/api/risk/strategy/${encodeURIComponent(name)}/reset-circuit-breaker`)
      toast.success(`CB reset for '${name}'`)
      loadAll()
    } catch { toast.error('Reset failed') }
  }

  const setField = (broker: string, field: string, value: string) =>
    setBrokerRisk(prev => ({ ...prev, [broker]: { ...prev[broker], [field]: value } }))

  return (
    <div className="p-6 space-y-6 max-w-2xl">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-bold text-white">Settings</h1>
          <p className="text-sm text-gray-500 mt-0.5">Broker connections, account modes, and risk configuration</p>
        </div>
        <button onClick={loadAll} className="p-1.5 text-gray-500 hover:text-gray-300 hover:bg-dark-700 rounded-lg transition-colors">
          <RefreshCw size={14} />
        </button>
      </div>

      {/* Broker Connection & Mode */}
      <section className="bg-dark-800 border border-dark-600 rounded-xl p-5 space-y-4">
        <h2 className="text-sm font-semibold text-gray-300 uppercase tracking-wider">Broker Accounts — Balance Source</h2>

        {brokers.map(b => {
          const currentMode = modes[b.name] ?? (b.paper ? 'paper' : 'live')
          const isPaper = currentMode === 'paper'
          const isToggling = togglingBroker === b.name
          return (
            <div key={b.name} className="flex items-center justify-between py-4 border-b border-dark-700 last:border-0">
              <div className="flex items-center gap-3">
                <StatusIcon ok={b.connected} />
                <div>
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-medium text-white capitalize">{b.name}</span>
                    {b.connected && b.total !== undefined && b.currency && (
                      <span className="text-xs text-gray-500">
                        ${b.total.toLocaleString('en-US', { maximumFractionDigits: 2 })} {b.currency}
                      </span>
                    )}
                  </div>
                  <span className={`text-xs ${b.connected ? 'text-brand-500' : 'text-gray-600'}`}>
                    {b.connected ? 'Connected' : 'Disconnected'}
                  </span>
                </div>
              </div>
              <div className="flex items-center gap-3">
                <span className={`text-xs font-medium ${isPaper ? 'text-blue-400' : 'text-yellow-400'}`}>
                  {isPaper ? 'Balance: Paper' : 'Balance: Live'}
                </span>
                <Toggle checked={isPaper} onChange={() => toggleBrokerMode(b.name, isPaper)} disabled={isToggling} />
                {!isPaper && (
                  <span className="flex items-center gap-1 text-xs text-yellow-500 bg-yellow-900/20 px-1.5 py-0.5 rounded border border-yellow-900/40">
                    <ShieldAlert size={10} /> Live
                  </span>
                )}
              </div>
            </div>
          )
        })}

        <div className="pt-1 space-y-1 text-xs text-gray-600">
          <p>{'- '}<span className="text-blue-400">Paper</span>{' — dashboard shows paper/testnet balance; orphan sync queries testnet positions.'}</p>
          <p>{'- '}<span className="text-yellow-400">Live</span>{' — dashboard shows live account balance; orphan sync queries live positions.'}</p>
          <p className="text-gray-700">{'Trade execution mode is set per-strategy on the Strategies page.'}</p>
        </div>
      </section>

      {/* Per-Broker Risk Parameters */}
      <section className="bg-dark-800 border border-dark-600 rounded-xl p-5 space-y-3">
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-semibold text-gray-300 uppercase tracking-wider">Risk Parameters — Per Broker</h2>
          <button onClick={resetGlobalCB} className="flex items-center gap-1 text-xs text-yellow-400 hover:text-yellow-300 bg-yellow-900/20 px-2 py-1 rounded border border-yellow-900/40 transition-colors">
            <AlertTriangle size={11} /> Reset Global CB
          </button>
        </div>
        <p className="text-xs text-gray-600">Each broker has independent risk limits. Leave a field blank to use the global default shown in the placeholder.</p>

        {BROKERS.map(broker => {
          const f = brokerRisk[broker]
          const bs: BrokerRiskState = brokerState[broker] ?? { circuit_breaker_active: false, consecutive_losses: 0 }
          const isExpanded = expandedBroker === broker
          return (
            <div key={broker} className="border border-dark-600 rounded-xl overflow-hidden">
              {/* Header row */}
              <button
                className="w-full flex items-center justify-between px-4 py-3 bg-dark-700 hover:bg-dark-600 transition-colors"
                onClick={() => setExpandedBroker(isExpanded ? null : broker)}
              >
                <div className="flex items-center gap-3">
                  <span className="text-sm font-semibold text-white uppercase">{broker}</span>
                  {bs.circuit_breaker_active && (
                    <span className="text-xs text-red-400 bg-red-900/30 px-1.5 py-0.5 rounded border border-red-800/40">
                      🔴 CB Active
                    </span>
                  )}
                  {bs.consecutive_losses > 0 && (
                    <span className="text-xs text-yellow-400 bg-yellow-900/20 px-1.5 py-0.5 rounded border border-yellow-800/40">
                      {bs.consecutive_losses} loss streak
                    </span>
                  )}
                </div>
                {isExpanded ? <ChevronUp size={14} className="text-gray-400" /> : <ChevronDown size={14} className="text-gray-400" />}
              </button>

              {isExpanded && (
                <div className="px-4 pb-4 pt-3 space-y-3 bg-dark-800">
                  {/* CB + loss streak resets */}
                  <div className="flex gap-2">
                    <button
                      onClick={() => resetBrokerCB(broker)}
                      disabled={!bs.circuit_breaker_active}
                      className="flex items-center gap-1.5 px-3 py-1.5 bg-dark-700 hover:bg-dark-600 disabled:opacity-30 border border-dark-500 text-yellow-400 text-xs rounded-lg transition-all"
                    >
                      <AlertTriangle size={11} /> Reset Circuit Breaker
                    </button>
                    <button
                      onClick={() => resetBrokerLosses(broker)}
                      disabled={bs.consecutive_losses === 0}
                      className="flex items-center gap-1.5 px-3 py-1.5 bg-dark-700 hover:bg-dark-600 disabled:opacity-30 border border-dark-500 text-gray-300 text-xs rounded-lg transition-all"
                    >
                      Reset Loss Streak ({bs.consecutive_losses})
                    </button>
                  </div>

                  {/* Risk fields */}
                  <div className="grid grid-cols-2 gap-3">
                    <RiskField label="Risk Per Trade (%)" value={f.risk_per_trade_pct}
                      placeholder={String(GLOBAL_DEFAULTS.risk_per_trade_pct)} step="0.1" min="0.1" max="10"
                      onChange={v => setField(broker, 'risk_per_trade_pct', v)} />
                    <RiskField label="Max Open Positions" value={f.max_open_positions}
                      placeholder={String(GLOBAL_DEFAULTS.max_open_positions)} step="1" min="1" max="50"
                      onChange={v => setField(broker, 'max_open_positions', v)} />
                    <RiskField label="Daily CB Limit (%)" value={f.daily_circuit_breaker_pct}
                      placeholder={String(GLOBAL_DEFAULTS.daily_circuit_breaker_pct)} step="0.5" min="1" max="20"
                      onChange={v => setField(broker, 'daily_circuit_breaker_pct', v)} />
                    <RiskField label="Max Consecutive Losses" value={f.max_consecutive_losses}
                      placeholder={String(GLOBAL_DEFAULTS.max_consecutive_losses)} step="1" min="1" max="20"
                      onChange={v => setField(broker, 'max_consecutive_losses', v)} />
                    <RiskField label="Max Asset Exposure (%)" value={f.max_exposure_per_asset_pct}
                      placeholder={String(GLOBAL_DEFAULTS.max_exposure_per_asset_pct)} step="1" min="1" max="100"
                      onChange={v => setField(broker, 'max_exposure_per_asset_pct', v)} />
                    <RiskField label="Max Class Exposure (%)" value={f.max_exposure_per_class_pct}
                      placeholder={String(GLOBAL_DEFAULTS.max_exposure_per_class_pct)} step="1" min="1" max="100"
                      onChange={v => setField(broker, 'max_exposure_per_class_pct', v)} />
                  </div>

                  <button
                    onClick={() => saveBrokerRisk(broker)}
                    disabled={savingBroker === broker}
                    className="w-full py-2 bg-brand-500 hover:bg-green-400 disabled:opacity-50 text-black text-sm font-semibold rounded-lg transition-all"
                  >
                    {savingBroker === broker ? 'Saving...' : `Save ${broker.toUpperCase()} Settings`}
                  </button>
                </div>
              )}
            </div>
          )
        })}
      </section>

      {/* Scalp Engine Circuit Breaker */}
      {brokerState['scalp'] && (
        <section className="bg-dark-800 border border-dark-600 rounded-xl p-5 space-y-3">
          <h2 className="text-sm font-semibold text-gray-300 uppercase tracking-wider">Scalp Engine Circuit Breaker</h2>
          <p className="text-xs text-gray-600">
            Scalping strategies share an isolated CB key ("scalp") that is independent of the Binance broker CB.
            Threshold and daily loss limit are configured in the Scalping page settings.
          </p>
          <div className="flex items-center justify-between bg-dark-700 rounded-lg px-4 py-3">
            <div className="flex items-center gap-3">
              {brokerState['scalp'].circuit_breaker_active
                ? <span className="text-xs text-red-400 bg-red-900/30 px-2 py-1 rounded border border-red-800/40">🔴 CB Active — all scalp signals blocked</span>
                : <span className="text-xs text-brand-500 bg-brand-900/20 px-2 py-1 rounded border border-brand-800/40">🟢 OK</span>
              }
              {brokerState['scalp'].consecutive_losses > 0 && (
                <span className="text-xs text-yellow-400">{brokerState['scalp'].consecutive_losses} consecutive scalp losses</span>
              )}
            </div>
            <div className="flex gap-2">
              <button
                onClick={() => resetBrokerCB('scalp')}
                disabled={!brokerState['scalp'].circuit_breaker_active}
                className="flex items-center gap-1 text-xs text-yellow-400 hover:text-yellow-300 bg-yellow-900/20 hover:bg-yellow-900/30 disabled:opacity-30 px-2 py-1 rounded border border-yellow-900/40 transition-all"
              >
                <AlertTriangle size={10} /> Reset CB
              </button>
              <button
                onClick={() => resetBrokerLosses('scalp')}
                disabled={brokerState['scalp'].consecutive_losses === 0}
                className="flex items-center gap-1 text-xs text-gray-300 hover:text-white bg-dark-600 hover:bg-dark-500 disabled:opacity-30 px-2 py-1 rounded border border-dark-500 transition-all"
              >
                Reset Streak ({brokerState['scalp'].consecutive_losses})
              </button>
            </div>
          </div>
        </section>
      )}

      {/* Per-Strategy Circuit Breakers */}
      {Object.keys(strategyState).length > 0 && (
        <section className="bg-dark-800 border border-dark-600 rounded-xl p-5 space-y-3">
          <h2 className="text-sm font-semibold text-gray-300 uppercase tracking-wider">Strategy Circuit Breakers</h2>
          <p className="text-xs text-gray-600">Per-strategy consecutive-loss counters. A tripped CB blocks new signals from that strategy only.</p>
          <div className="space-y-2">
            {Object.entries(strategyState).map(([name, s]) => (
              <div key={name} className="flex items-center justify-between bg-dark-700 rounded-lg px-3 py-2.5">
                <div className="flex items-center gap-2 min-w-0">
                  {s.circuit_breaker_active
                    ? <span className="text-red-400 shrink-0">🔴</span>
                    : <span className="text-brand-500 shrink-0">🟢</span>
                  }
                  <span className="text-sm text-white truncate">{name}</span>
                  <span className="text-xs text-gray-500 shrink-0">
                    {s.consecutive_losses}
                    {s.max_consecutive_losses_effective != null
                      ? ` / ${s.max_consecutive_losses_effective} losses`
                      : ' losses'}
                  </span>
                </div>
                <button
                  onClick={() => resetStrategyCB(name)}
                  disabled={!s.circuit_breaker_active && s.consecutive_losses === 0}
                  className="flex items-center gap-1 text-xs text-yellow-400 hover:text-yellow-300 bg-yellow-900/20 hover:bg-yellow-900/30 disabled:opacity-30 px-2 py-1 rounded border border-yellow-900/40 transition-all shrink-0 ml-2"
                >
                  <AlertTriangle size={10} /> Reset
                </button>
              </div>
            ))}
          </div>
        </section>
      )}

      {/* ML Models */}
      <section className="bg-dark-800 border border-dark-600 rounded-xl p-5 space-y-4">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Brain size={14} className="text-brand-500" />
            <h2 className="text-sm font-semibold text-gray-300 uppercase tracking-wider">ML Models</h2>
          </div>
          <button
            onClick={retrainNow}
            disabled={retraining}
            className="flex items-center gap-1.5 px-3 py-1.5 bg-brand-500 hover:bg-green-400 disabled:opacity-50 text-black text-xs font-semibold rounded-lg transition-all"
          >
            <RefreshCw size={11} className={retraining ? 'animate-spin' : ''} />
            {retraining ? 'Queuing...' : 'Retrain Now'}
          </button>
        </div>

        {mlStatus ? (
          <>
            {/* Stats row */}
            <div className="grid grid-cols-3 gap-3">
              <div className="bg-dark-700 rounded-lg p-3 text-center">
                <div className="text-lg font-bold text-white">{mlStatus.model_count}</div>
                <div className="text-xs text-gray-500 mt-0.5">Models trained</div>
              </div>
              <div className="bg-dark-700 rounded-lg p-3 text-center">
                <div className="text-lg font-bold text-white">
                  {mlStatus.win_rate_pct != null ? `${mlStatus.win_rate_pct}%` : '—'}
                </div>
                <div className="text-xs text-gray-500 mt-0.5">Signal win rate</div>
              </div>
              <div className="bg-dark-700 rounded-lg p-3 text-center">
                <div className={`text-lg font-bold ${
                  mlStatus.avg_pnl_pct == null ? 'text-white'
                  : mlStatus.avg_pnl_pct >= 0 ? 'text-brand-500' : 'text-red-400'
                }`}>
                  {mlStatus.avg_pnl_pct != null ? `${mlStatus.avg_pnl_pct > 0 ? '+' : ''}${mlStatus.avg_pnl_pct}%` : '—'}
                </div>
                <div className="text-xs text-gray-500 mt-0.5">Avg signal P&amp;L</div>
              </div>
            </div>

            {/* Feedback loop */}
            <div className="flex items-center justify-between text-xs">
              <span className="text-gray-500">Feedback loop</span>
              <span className={mlStatus.feedback_loop_active ? 'text-brand-500' : 'text-gray-600'}>
                {mlStatus.feedback_loop_active
                  ? `Active — ${mlStatus.outcomes_resolved} resolved / ${mlStatus.outcomes_total} total signals`
                  : `Inactive — ${mlStatus.outcomes_total} signals pending resolution`}
              </span>
            </div>

            {/* Last retrain */}
            <div className="flex items-center justify-between text-xs">
              <span className="text-gray-500">Last retrain</span>
              <span className="text-gray-400">
                {mlStatus.last_retrain
                  ? parseUtc(mlStatus.last_retrain)?.toLocaleString()
                  : 'Never — click Retrain Now'}
              </span>
            </div>

            {/* Per-symbol model list */}
            {mlStatus.models.length > 0 ? (
              <div className="space-y-1">
                <div className="text-xs text-gray-600 uppercase tracking-wider mb-1">Trained symbols</div>
                {mlStatus.models.map(m => (
                  <div key={m.symbol} className="flex items-center justify-between bg-dark-700 px-3 py-2 rounded-lg">
                    <span className="text-sm font-medium text-white">{m.symbol}</span>
                    <span className="text-xs text-gray-500">
                      {m.trained_date ? `Trained ${m.trained_date}` : m.model_path}
                    </span>
                  </div>
                ))}
              </div>
            ) : (
              <div className="text-center py-4 text-xs text-gray-600">
                No models trained yet. Click <span className="text-brand-500">Retrain Now</span> to train on the first 365 days of market data.
              </div>
            )}
          </>
        ) : (
          <div className="text-xs text-gray-600 text-center py-3">Loading ML status…</div>
        )}
      </section>

      {/* Regime Router */}
      <section className="bg-dark-800 border border-dark-600 rounded-xl p-5 space-y-4">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <ShieldAlert size={14} className="text-purple-400" />
            <h2 className="text-sm font-semibold text-gray-300 uppercase tracking-wider">Regime Router</h2>
          </div>
          <div className="flex items-center gap-2">
            <span className="text-xs text-gray-500">{regimeEnabled ? 'Enabled' : 'Disabled'}</span>
            <Toggle checked={regimeEnabled} onChange={() => setRegimeEnabled(v => !v)} />
          </div>
        </div>

        <p className="text-xs text-gray-500 leading-relaxed">
          When enabled, each candle the system classifies the current market regime (trending, ranging, volatile)
          and applies each strategy's <span className="text-gray-300">Regime Mode</span> setting.
          Strategies set to <span className="text-gray-300">Auto-Switch</span> will swap to a compatible algorithm;
          strategies set to <span className="text-gray-300">Fixed</span> will hold until the regime matches.
        </p>

        <div className="flex items-end gap-4">
          <div className="flex-1">
            <label className="text-xs text-gray-400 block mb-1">
              Hysteresis Candles <span className="text-gray-600">(1–20 — regime must be stable this many candles before switching)</span>
            </label>
            <input
              type="number"
              min="1"
              max="20"
              step="1"
              value={regimeHysteresis}
              onChange={e => setRegimeHysteresis(e.target.value)}
              className="w-full bg-dark-700 border border-dark-500 text-white text-sm rounded-lg px-3 py-2 focus:outline-none focus:border-brand-500"
            />
          </div>
          <button
            onClick={saveRegimeSettings}
            disabled={savingRegime}
            className="px-4 py-2 bg-brand-500 hover:bg-green-400 disabled:opacity-50 text-black text-sm font-semibold rounded-lg transition-all whitespace-nowrap"
          >
            {savingRegime ? 'Saving…' : 'Save'}
          </button>
        </div>

        <div className="text-xs text-gray-600">
          Per-strategy Regime Mode is configured on the{' '}
          <span className="text-purple-400">Strategies</span> page — edit any strategy to set Auto-Switch or Fixed.
        </div>
      </section>

      {/* System Info */}
      <section className="bg-dark-800 border border-dark-600 rounded-xl p-5 space-y-2">
        <h2 className="text-sm font-semibold text-gray-300 uppercase tracking-wider">System</h2>
        <div className="flex justify-between text-sm">
          <span className="text-gray-500">Backend</span>
          <a href="http://localhost:8000/docs" target="_blank" rel="noreferrer" className="text-brand-500 hover:underline text-xs">
            API Docs -&gt;
          </a>
        </div>
        <div className="flex justify-between text-sm">
          <span className="text-gray-500">Version</span>
          <span className="text-gray-400">1.0.0-alpha</span>
        </div>
      </section>
    </div>
  )

}
