import { useEffect, useRef, useState, useCallback } from 'react'

type MessageHandler = (data: unknown) => void

interface UseWebSocketOptions {
  onMessage?: MessageHandler
  maxReconnectDelay?: number
}

export function useWebSocket(url: string, options: UseWebSocketOptions = {}) {
  const { onMessage, maxReconnectDelay = 30000 } = options
  const wsRef = useRef<WebSocket | null>(null)
  const [connected, setConnected] = useState(false)
  const reconnectTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const reconnectDelay = useRef(1000)  // starts at 1s, doubles up to maxReconnectDelay
  const mountedRef = useRef(true)
  // Keep onMessage in a ref so changing the callback never triggers a reconnect
  const onMessageRef = useRef<MessageHandler | undefined>(onMessage)
  useEffect(() => { onMessageRef.current = onMessage })

  const connect = useCallback(() => {
    if (!mountedRef.current) return
    try {
      const ws = new WebSocket(url)
      wsRef.current = ws

      ws.onopen = () => {
        if (mountedRef.current) {
          setConnected(true)
          reconnectDelay.current = 1000  // reset backoff on successful connect
        }
      }

      ws.onmessage = (event) => {
        if (!mountedRef.current) return
        try {
          const data = JSON.parse(event.data)
          onMessageRef.current?.(data)
        } catch {}
      }

      ws.onerror = (event) => {
        console.error('[WebSocket] connection error:', event)
      }

      ws.onclose = () => {
        if (!mountedRef.current) return
        setConnected(false)
        const delay = reconnectDelay.current
        reconnectTimer.current = setTimeout(connect, delay)
        // Exponential backoff: double the delay each attempt, cap at maxReconnectDelay
        reconnectDelay.current = Math.min(delay * 2, maxReconnectDelay)
      }
    } catch {}
  }, [url, maxReconnectDelay])  // onMessage intentionally excluded — stored in ref above

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
