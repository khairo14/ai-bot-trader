import { useState, useEffect } from 'react'
import { CheckCircle, XCircle, AlertTriangle, RefreshCw, ShieldAlert } from 'lucide-react'
import axios from 'axios'
import toast from 'react-hot-toast'

interface BrokerStatus {
  name: string
  connected: boolean
  paper: boolean
  total?: number
  currency?: string
}

const StatusIcon = ({ ok }: { ok: boolean }) =>
  ok
    ? <CheckCircle size={16} className="text-brand-500" />
    : <XCircle size={16} className="text-red-500" />

// Inline toggle switch
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

export default function Settings() {
  const [brokers, setBrokers] = useState<BrokerStatus[]>([
    { name: 'binance', connected: false, paper: true },
    { name: 'alpaca', connected: false, paper: true },
    { name: 'ibkr', connected: false, paper: false },
  ])
  const [modes, setModes] = useState<Record<string, string>>({})
  const [togglingBroker, setTogglingBroker] = useState<string | null>(null)
  const [risk, setRisk] = useState({ risk_per_trade_pct: 2.0, daily_circuit_breaker_pct: 5.0, max_open_positions: 5 })
  const [saving, setSaving] = useState(false)

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
  }

  useEffect(() => { loadAll() }, [])

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
              Ensure live credentials are configured in .env first.
            </p>
            <div style={{ display: 'flex', gap: '8px', marginTop: '4px' }}>
              <button
                onClick={() => { toast.dismiss(t.id); doSwitch(broker, nextMode) }}
                style={{ flex: 1, padding: '5px 0', background: '#eab308', color: '#000', borderRadius: '6px', fontWeight: 700, fontSize: '12px', cursor: 'pointer', border: 'none' }}
              >
                Yes, go Live
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

  const saveRisk = async () => {
    setSaving(true)
    try {
      await axios.post('/api/strategies/risk-params', risk)
      toast.success('Risk parameters saved')
    } catch {
      toast.error('Failed to save - backend endpoint not wired yet')
    } finally {
      setSaving(false)
    }
  }

  const resetCircuitBreaker = async () => {
    try {
      await axios.post('/api/positions/reset-circuit-breaker')
      toast.success('Circuit breaker reset')
    } catch {
      toast.error('Reset failed')
    }
  }

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
        <h2 className="text-sm font-semibold text-gray-300 uppercase tracking-wider">Broker Accounts</h2>

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

              {/* Paper / Live mode toggle */}
              <div className="flex items-center gap-3">
                <span className={`text-xs font-medium ${isPaper ? 'text-blue-400' : 'text-yellow-400'}`}>
                  {isPaper ? 'Paper' : 'Live'}
                </span>
                <Toggle
                  checked={isPaper}
                  onChange={() => toggleBrokerMode(b.name, isPaper)}
                  disabled={isToggling}
                />
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
          <p>{'- '}<span className="text-blue-400">Paper</span>{' \u2014 uses paper/testnet credentials. No real money at risk.'}</p>
          <p>{'- '}<span className="text-yellow-400">Live</span>{' \u2014 uses live credentials from '}<code className="text-gray-400">.env</code>{'. Real orders are placed.'}</p>
          <p>{'- Mode change takes effect on the next signal run. Set live keys in '}<code className="text-gray-400">.env</code>{' first.'}</p>
        </div>
      </section>

      {/* Risk Parameters */}
      <section className="bg-dark-800 border border-dark-600 rounded-xl p-5 space-y-4">
        <h2 className="text-sm font-semibold text-gray-300 uppercase tracking-wider">Risk Parameters</h2>

        <div className="space-y-3">
          <div>
            <label className="text-xs text-gray-400 block mb-1">Risk Per Trade (%)</label>
            <input
              type="number" step="0.1" min="0.1" max="10"
              value={risk.risk_per_trade_pct}
              onChange={e => setRisk({ ...risk, risk_per_trade_pct: parseFloat(e.target.value) })}
              className="w-full bg-dark-700 border border-dark-500 text-white text-sm rounded-lg px-3 py-2 focus:outline-none focus:border-brand-500"
            />
          </div>

          <div>
            <label className="text-xs text-gray-400 block mb-1">Daily Circuit Breaker (%)</label>
            <input
              type="number" step="0.5" min="1" max="20"
              value={risk.daily_circuit_breaker_pct}
              onChange={e => setRisk({ ...risk, daily_circuit_breaker_pct: parseFloat(e.target.value) })}
              className="w-full bg-dark-700 border border-dark-500 text-white text-sm rounded-lg px-3 py-2 focus:outline-none focus:border-brand-500"
            />
          </div>

          <div>
            <label className="text-xs text-gray-400 block mb-1">Max Open Positions</label>
            <input
              type="number" step="1" min="1" max="50"
              value={risk.max_open_positions}
              onChange={e => setRisk({ ...risk, max_open_positions: parseInt(e.target.value) })}
              className="w-full bg-dark-700 border border-dark-500 text-white text-sm rounded-lg px-3 py-2 focus:outline-none focus:border-brand-500"
            />
          </div>
        </div>

        <div className="flex gap-3 pt-1">
          <button
            onClick={saveRisk}
            disabled={saving}
            className="flex-1 py-2 bg-brand-500 hover:bg-green-400 disabled:opacity-50 text-black text-sm font-semibold rounded-lg transition-all"
          >
            {saving ? 'Saving...' : 'Save Parameters'}
          </button>
          <button
            onClick={resetCircuitBreaker}
            className="flex items-center gap-1.5 px-4 py-2 bg-dark-700 hover:bg-dark-600 border border-dark-500 text-yellow-400 text-sm rounded-lg transition-all"
          >
            <AlertTriangle size={14} /> Reset Circuit Breaker
          </button>
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

