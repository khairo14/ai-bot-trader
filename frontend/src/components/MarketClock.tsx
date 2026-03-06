/**
 * MarketClock
 * -----------
 * Shows:
 *  - Live ticking clock in ET and UTC
 *  - Per-broker market session status (OPEN / CLOSED / 24/7)
 *
 * The session status is pulled from GET /api/forward/market-status every 60 s.
 * The clock ticks every second entirely client-side.
 */

import { useEffect, useRef, useState } from 'react'
import { Clock } from 'lucide-react'
import axios from 'axios'

interface SessionStatus {
  broker: string
  open: boolean
  label: string                  // "Open" | "Closed" | "24 / 7"
  next_event: string | null      // "09:30 ET"
  next_event_label: string | null // "Opens in 42 min"
}

interface MarketStatus {
  server_utc: string             // YYYY-MM-DDTHH:MM:SSZ
  et_offset: string              // "-0500" | "-0400"
  sessions: SessionStatus[]
}

const BROKER_LABEL: Record<string, string> = {
  binance: 'Binance',
  alpaca: 'Alpaca',
  ibkr: 'IBKR',
}

function formatBrokerLabel(broker: string) {
  return BROKER_LABEL[broker] ?? broker
}

function etOffsetToMinutes(offset: string): number {
  // offset like "-0500" or "-0400"
  const sign   = offset[0] === '-' ? -1 : 1
  const hours  = parseInt(offset.slice(1, 3), 10)
  const mins   = parseInt(offset.slice(3, 5), 10)
  return sign * (hours * 60 + mins)
}

function formatTime(d: Date, offsetMin: number): string {
  // Shift UTC date by offsetMin to get ET wall clock
  const utcMs = d.getTime() + offsetMin * 60_000
  const et    = new Date(utcMs)
  const h     = et.getUTCHours()
  const m     = et.getUTCMinutes().toString().padStart(2, '0')
  const s     = et.getUTCSeconds().toString().padStart(2, '0')
  const ampm  = h >= 12 ? 'PM' : 'AM'
  const h12   = ((h % 12) || 12).toString()
  return `${h12}:${m}:${s} ${ampm}`
}

function formatUTC(d: Date): string {
  const h = d.getUTCHours().toString().padStart(2, '0')
  const m = d.getUTCMinutes().toString().padStart(2, '0')
  const s = d.getUTCSeconds().toString().padStart(2, '0')
  return `${h}:${m}:${s} UTC`
}

function dayLabel(d: Date, offsetMin: number): string {
  const days = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']
  const utcMs = d.getTime() + offsetMin * 60_000
  const et    = new Date(utcMs)
  return days[et.getUTCDay()]
}

export default function MarketClock() {
  const [now, setNow]           = useState<Date>(new Date())
  const [status, setStatus]     = useState<MarketStatus | null>(null)
  const [etOffsetMin, setEtOffsetMin] = useState<number>(-300) // default EST
  const tickRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const fetchStatus = async () => {
    try {
      const res = await axios.get<MarketStatus>('/api/forward/market-status')
      setStatus(res.data)
      setEtOffsetMin(etOffsetToMinutes(res.data.et_offset))
    } catch {
      // silently ignore — clock still ticks, session pills stay stale
    }
  }

  useEffect(() => {
    fetchStatus()
    // tick clock every second
    tickRef.current = setInterval(() => setNow(new Date()), 1_000)
    // refresh market status every 60 s
    pollRef.current = setInterval(fetchStatus, 60_000)
    return () => {
      if (tickRef.current) clearInterval(tickRef.current)
      if (pollRef.current) clearInterval(pollRef.current)
    }
  }, [])

  const etTime   = formatTime(now, etOffsetMin)
  const utcTime  = formatUTC(now)
  const day      = dayLabel(now, etOffsetMin)
  const isDST    = etOffsetMin === -240  // -4h = EDT

  return (
    <div className="flex items-center gap-3 text-xs">
      {/* Clock */}
      <div className="flex items-center gap-1.5 bg-dark-800 border border-dark-600 rounded-lg px-3 py-1.5">
        <Clock size={12} className="text-gray-500" />
        <span className="text-white font-mono font-medium">{etTime}</span>
        <span className="text-gray-600">{isDST ? 'EDT' : 'EST'}</span>
        <span className="text-dark-500">·</span>
        <span className="text-gray-500 font-mono">{utcTime}</span>
        <span className="text-gray-600 ml-0.5">{day}</span>
      </div>

      {/* Session pills */}
      {status && status.sessions.map((s) => (
        <div
          key={s.broker}
          title={s.next_event_label ?? undefined}
          className={`flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 border transition-colors ${
            s.label === '24 / 7'
              ? 'bg-blue-500/10 border-blue-500/20 text-blue-300'
              : s.open
              ? 'bg-green-500/10 border-green-500/20 text-green-300'
              : 'bg-dark-800 border-dark-600 text-gray-500'
          }`}
        >
          {/* status dot */}
          <span
            className={`w-1.5 h-1.5 rounded-full ${
              s.label === '24 / 7'
                ? 'bg-blue-400 animate-pulse'
                : s.open
                ? 'bg-green-400 animate-pulse'
                : 'bg-gray-600'
            }`}
          />
          <span className="font-medium">{formatBrokerLabel(s.broker)}</span>
          <span className={`${s.open || s.label === '24 / 7' ? 'opacity-70' : 'opacity-50'}`}>
            {s.label === '24 / 7' ? '24/7' : s.label}
          </span>
          {/* next event hint — only show when closed with a time available */}
          {!s.open && s.next_event && (
            <span className="text-gray-600 text-[10px]">· {s.next_event}</span>
          )}
        </div>
      ))}
    </div>
  )
}
