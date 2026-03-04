import { Play, Pause, StopCircle, Activity } from 'lucide-react'

export default function ForwardTest() {
  return (
    <div className="p-6 space-y-6">
      <div>
        <h1 className="text-xl font-bold text-white">Forward Testing</h1>
        <p className="text-sm text-gray-500 mt-0.5">Paper trading on live market data. Validates strategy before real execution.</p>
      </div>

      {/* Status Banner */}
      <div className="bg-dark-800 border border-dark-600 rounded-xl p-4 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div className="w-2 h-2 bg-gray-600 rounded-full" />
          <div>
            <p className="text-sm font-medium text-white">Forward Test: Inactive</p>
            <p className="text-xs text-gray-500">No active strategies in paper mode</p>
          </div>
        </div>
        <div className="flex gap-2">
          <button className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-brand-500/10 text-brand-500 hover:bg-brand-500/20 text-sm font-medium transition-all">
            <Play size={14} /> Start
          </button>
          <button className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-dark-700 text-gray-400 hover:text-gray-200 text-sm transition-all" disabled>
            <Pause size={14} /> Pause
          </button>
          <button className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-dark-700 text-gray-400 hover:text-red-400 text-sm transition-all" disabled>
            <StopCircle size={14} /> Stop
          </button>
        </div>
      </div>

      {/* Paper Performance */}
      <div className="grid grid-cols-4 gap-4">
        {[
          { label: 'Paper Balance', value: '$10,000.00' },
          { label: 'Open Positions', value: '0' },
          { label: 'Total Paper P&L', value: '$0.00' },
          { label: 'Days Running', value: '0' },
        ].map(({ label, value }) => (
          <div key={label} className="bg-dark-800 border border-dark-600 rounded-xl p-4">
            <p className="text-xs text-gray-500 uppercase tracking-wider mb-1">{label}</p>
            <p className="text-xl font-bold text-white">{value}</p>
          </div>
        ))}
      </div>

      {/* Empty state */}
      <div className="bg-dark-800 border border-dark-600 rounded-xl p-12 text-center">
        <Activity size={40} className="text-gray-600 mx-auto mb-4 opacity-30" />
        <p className="text-gray-500 text-sm">No paper trades yet.</p>
        <p className="text-gray-600 text-xs mt-1">Enable a strategy in paper mode and start the forward test to see trades here.</p>
      </div>
    </div>
  )
}
