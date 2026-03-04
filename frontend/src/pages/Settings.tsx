import { useState, useEffect } from 'react'
import { CheckCircle, XCircle, AlertTriangle, RefreshCw } from 'lucide-react'
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

export default function Settings() {
  const [brokers, setBrokers] = useState<BrokerStatus[]>([
    { name: 'binance', connected: false, paper: true },
    { name: 'alpaca', connected: false, paper: true },
    { name: 'ibkr', connected: false, paper: false },
  ])
  const [risk, setRisk] = useState({ risk_per_trade_pct: 2.0, daily_circuit_breaker_pct: 5.0, max_open_positions: 5 })
  const [saving, setSaving] = useState(false)

  useEffect(() => {
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
  }, [])

  const saveRisk = async () => {
    setSaving(true)
    try {
      await axios.post('/api/strategies/risk-params', risk)
      toast.success('Risk parameters saved')
    } catch {
      toast.error('Failed to save — backend endpoint not wired yet')
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
      <div>
        <h1 className="text-xl font-bold text-white">Settings</h1>
        <p className="text-sm text-gray-500 mt-0.5">Broker connections and risk configuration</p>
      </div>

      {/* Broker Connection Status */}
      <section className="bg-dark-800 border border-dark-600 rounded-xl p-5 space-y-3">
        <h2 className="text-sm font-semibold text-gray-300 uppercase tracking-wider">Broker Connections</h2>
        {brokers.map(b => (
          <div key={b.name} className="flex items-center justify-between py-2 border-b border-dark-700 last:border-0">
            <div className="flex items-center gap-2">
              <StatusIcon ok={b.connected} />
              <span className="text-sm text-white capitalize">{b.name}</span>
              {b.paper && <span className="text-xs text-yellow-500 bg-yellow-900/20 px-1.5 py-0.5 rounded">paper</span>}
              {!b.paper && b.connected && <span className="text-xs text-red-400 bg-red-900/20 px-1.5 py-0.5 rounded">live</span>}
            </div>
            <div className="text-right">
              <span className={`text-xs ${b.connected ? 'text-brand-500' : 'text-red-400'}`}>
                {b.connected ? 'Connected' : 'Disconnected'}
              </span>
              {b.connected && b.total !== undefined && b.currency && (
                <p className="text-xs text-gray-400 mt-0.5">
                  {(b.currency === 'USD' || b.currency === 'USDT' ? '$' : '') +
                    b.total.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                  {' '}{b.currency}
                </p>
              )}
            </div>
          </div>
        ))}
        <p className="text-xs text-gray-600 pt-1">
          Configure credentials in <code className="text-gray-400">.env</code> and restart the backend.
        </p>
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
            API Docs →
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
