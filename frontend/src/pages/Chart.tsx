import { useState } from 'react'
import { ChartPanel } from './ChartPanel'

type Layout = 1 | 2 | 4

/** Default symbol+broker per panel slot (1→4 panels) */
const PANEL_DEFAULTS = [
  { broker: 'binance', symbol: 'BTC/USDT' },
  { broker: 'binance', symbol: 'ETH/USDT' },
  { broker: 'binance', symbol: 'SOL/USDT' },
  { broker: 'binance', symbol: 'BNB/USDT' },
]

export default function Chart() {
  const [layout, setLayout] = useState<Layout>(1)

  const gridStyle = {
    gridTemplateColumns: layout === 1 ? '1fr' : '1fr 1fr',
    gridTemplateRows: layout === 4 ? '1fr 1fr' : '1fr',
  }

  return (
    <div className="flex flex-col h-full bg-dark-900">
      {/* Layout picker */}
      <div className="flex items-center gap-2 px-4 py-2 bg-dark-800 border-b border-dark-700 shrink-0">
        <span className="text-xs text-gray-500 mr-1">Layout</span>
        {([1, 2, 4] as Layout[]).map(n => (
          <button key={n} onClick={() => setLayout(n)}
            className={`text-xs px-3 py-1.5 rounded-lg transition-all border ${
              layout === n
                ? 'bg-brand-500 border-brand-500 text-white'
                : 'border-dark-600 text-gray-400 hover:text-gray-200 hover:border-dark-400'
            }`}>
            {n === 1 ? '1x1' : n === 2 ? '1x2' : '2x2'}
          </button>
        ))}
        <span className="ml-3 text-[10px] text-gray-600">
          {layout === 1 ? 'Single chart' : layout === 2 ? '2 charts side-by-side' : '4-chart grid'}
        </span>
      </div>

      {/* Panel grid */}
      <div className="flex-1 min-h-0 grid gap-0.5 bg-dark-900 p-0.5" style={gridStyle}>
        {PANEL_DEFAULTS.slice(0, layout).map((def, i) => (
          <ChartPanel
            key={`${layout}-${i}`}
            defaultBroker={def.broker}
            defaultSymbol={def.symbol}
            compact={layout === 4}
          />
        ))}
      </div>
    </div>
  )
}