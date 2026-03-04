import { useEffect, useRef, useState, useCallback } from 'react'

type MessageHandler = (data: unknown) => void

interface UseWebSocketOptions {
  onMessage?: MessageHandler
  reconnectDelay?: number
}

export function useWebSocket(url: string, options: UseWebSocketOptions = {}) {
  const { onMessage, reconnectDelay = 3000 } = options
  const wsRef = useRef<WebSocket | null>(null)
  const [connected, setConnected] = useState(false)
  const reconnectTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const mountedRef = useRef(true)

  const connect = useCallback(() => {
    if (!mountedRef.current) return
    try {
      const ws = new WebSocket(url)
      wsRef.current = ws

      ws.onopen = () => {
        if (mountedRef.current) setConnected(true)
      }

      ws.onmessage = (event) => {
        if (!mountedRef.current) return
        try {
          const data = JSON.parse(event.data)
          onMessage?.(data)
        } catch {}
      }

      ws.onerror = () => {}

      ws.onclose = () => {
        if (!mountedRef.current) return
        setConnected(false)
        reconnectTimer.current = setTimeout(connect, reconnectDelay)
      }
    } catch {}
  }, [url, onMessage, reconnectDelay])

  useEffect(() => {
    mountedRef.current = true
    connect()
    return () => {
      mountedRef.current = false
      if (reconnectTimer.current) clearTimeout(reconnectTimer.current)
      wsRef.current?.close()
    }
  }, [connect])

  const send = useCallback((data: unknown) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify(data))
    }
  }, [])

  return { connected, send }
}
