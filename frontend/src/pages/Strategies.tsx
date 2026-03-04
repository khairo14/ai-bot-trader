import { useEffect, useState } from 'react'
import { Plus, Layers, ToggleLeft, ToggleRight } from 'lucide-react'
import axios from 'axios'
import toast from 'react-hot-toast'

interface Strategy {
  id: number
  name: string
  description: string
  asset_class: string
  broker: string
  execution_mode: string
  is_active: boolean
  is_paper: boolean
}

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

export default function Strategies() {
  const [strategies, setStrategies] = useState<Strategy[]>([])

  const load = () => {
    axios.get('/api/strategies/').then(r => setStrategies(r.data.strategies || []))
  }

  useEffect(() => { load() }, [])

  const toggleActive = async (s: Strategy) => {
    await axios.patch(`/api/strategies/${s.id}`, { is_active: !s.is_active })
    toast.success(`Strategy ${s.is_active ? 'paused' : 'activated'}`)
    load()
  }

  const changeMode = async (s: Strategy, mode: string) => {
    await axios.patch(`/api/strategies/${s.id}`, { execution_mode: mode })
    toast.success(`Mode changed to ${mode}`)
    load()
  }

  return (
    <div className="p-6 space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-bold text-white">Strategies</h1>
          <p className="text-sm text-gray-500 mt-0.5">Manage and configure your trading strategies</p>
        </div>
        <button className="flex items-center gap-1.5 px-3 py-2 bg-brand-500 hover:bg-green-400 text-black text-sm font-semibold rounded-lg transition-all">
          <Plus size={16} /> New Strategy
        </button>
      </div>

      {strategies.length === 0 ? (
        <div className="bg-dark-800 border border-dark-600 rounded-xl p-12 text-center">
          <Layers size={40} className="text-gray-600 mx-auto mb-4 opacity-30" />
          <p className="text-gray-500 text-sm">No strategies configured yet.</p>
          <p className="text-gray-600 text-xs mt-1">Create a strategy to get started.</p>
        </div>
      ) : (
        <div className="space-y-3">
          {strategies.map(s => (
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
                  <p className="text-xs text-gray-500">{s.broker} · {s.asset_class} · {s.is_paper ? '📄 Paper' : '💰 Live'}</p>
                </div>
              </div>
              <div className="flex items-center gap-3">
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
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
