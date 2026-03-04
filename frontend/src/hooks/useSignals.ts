import { useState, useEffect, useCallback, useRef } from 'react'
import axios from 'axios'

interface Signal {
  id: number
  symbol: string
  signal: string
  entry_price: number
  stop_loss: number
  take_profit: number
  confidence: number
  timeframe: string
  strategy_name: string
  broker: string
  reasons: string
  created_at: string
}

interface UseSignalsOptions {
  limit?: number
  interval?: number  // poll interval in ms
  broker?: string
  symbol?: string
}

export function useSignals(options: UseSignalsOptions = {}) {
  const { limit = 20, interval = 30_000, broker, symbol } = options
  const [signals, setSignals] = useState<Signal[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const fetch = useCallback(async () => {
    try {
      const params: Record<string, string | number> = { limit }
      if (broker) params.broker = broker
      if (symbol) params.symbol = symbol
      const resp = await axios.get('/api/signals/', { params })
      setSignals(resp.data.signals ?? [])
      setError(null)
    } catch (err: any) {
      setError(err?.response?.data?.detail ?? 'Failed to fetch signals')
    } finally {
      setLoading(false)
    }
  }, [limit, broker, symbol])

  useEffect(() => {
    fetch()
    timerRef.current = setInterval(fetch, interval)
    return () => {
      if (timerRef.current) clearInterval(timerRef.current)
    }
  }, [fetch, interval])

  return { signals, loading, error, refetch: fetch }
}
