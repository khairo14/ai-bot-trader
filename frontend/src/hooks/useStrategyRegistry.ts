import { useState, useEffect } from 'react'
import axios from 'axios'

interface StrategyRegistry {
  by_broker: Record<string, string[]>
  all: string[]
  options_strategies: string[]
}

// Fallback keeps the UI functional when the backend is unreachable on first load.
const FALLBACK: StrategyRegistry = {
  by_broker: {
    binance: ['hybrid_macd_rsi', 'momentum_breakout', 'mean_reversion_bb', 'scalp_ema_vwap'],
    alpaca:  ['hybrid_macd_rsi', 'momentum_breakout', 'mean_reversion_bb', 'scalp_ema_vwap'],
    ibkr:    ['hybrid_macd_rsi', 'momentum_breakout', 'mean_reversion_bb',
               'iron_condor', 'covered_call', 'bull_call_spread'],
  },
  all: ['hybrid_macd_rsi', 'momentum_breakout', 'mean_reversion_bb', 'scalp_ema_vwap',
        'iron_condor', 'covered_call', 'bull_call_spread'],
  options_strategies: ['iron_condor', 'covered_call', 'bull_call_spread'],
}

// Module-level cache — fetched once, shared across all consumers to avoid
// redundant requests when multiple pages mount simultaneously.
let _cache: StrategyRegistry | null = null
let _promise: Promise<StrategyRegistry> | null = null

function fetchRegistry(): Promise<StrategyRegistry> {
  if (_cache) return Promise.resolve(_cache)
  if (!_promise) {
    _promise = axios
      .get<StrategyRegistry>('/api/strategies/registry')
      .then(res => { _cache = res.data; return res.data })
      .catch(() => { _promise = null; return FALLBACK })
  }
  return _promise
}

export function useStrategyRegistry() {
  const [registry, setRegistry] = useState<StrategyRegistry>(_cache ?? FALLBACK)
  const [loading, setLoading] = useState(!_cache)

  useEffect(() => {
    if (_cache) { setLoading(false); return }
    setLoading(true)
    fetchRegistry().then(r => {
      setRegistry(r)
      setLoading(false)
    })
  }, [])

  return {
    brokerStrategies: registry.by_broker,
    allStrategies: registry.all,
    optionsStrategies: registry.options_strategies,
    loading,
  }
}
