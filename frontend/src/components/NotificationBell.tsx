import { useEffect, useRef, useState, useCallback } from 'react'
import { Bell, X, CheckCheck, AlertTriangle, TrendingUp, Activity, Cpu, Info } from 'lucide-react'
import axios from 'axios'

interface Notif {
  id: number
  level: 'info' | 'success' | 'warning' | 'error'
  category: 'signal' | 'trade' | 'emergency' | 'system' | 'ml'
  title: string
  message: string
  is_read: boolean
  metadata: Record<string, unknown>
  created_at: string
}

const LEVEL_COLORS: Record<string, string> = {
  info:    'bg-blue-500/15 text-blue-400 border-blue-900/30',
  success: 'bg-green-500/15 text-green-400 border-green-900/30',
  warning: 'bg-yellow-500/15 text-yellow-400 border-yellow-900/30',
  error:   'bg-red-500/15 text-red-400 border-red-900/30',
}

const LEVEL_DOT: Record<string, string> = {
  info: 'bg-blue-400', success: 'bg-green-400',
  warning: 'bg-yellow-400', error: 'bg-red-400',
}

function CategoryIcon({ cat }: { cat: string }) {
  const cls = 'shrink-0'
  const sz = 13
  switch (cat) {
    case 'trade':     return <TrendingUp size={sz} className={cls} />
    case 'signal':    return <Activity size={sz} className={cls} />
    case 'emergency': return <AlertTriangle size={sz} className={cls} />
    case 'ml':        return <Cpu size={sz} className={cls} />
    default:          return <Info size={sz} className={cls} />
  }
}

function timeAgo(iso: string): string {
  const diff = (Date.now() - new Date(iso).getTime()) / 1000
  if (diff < 60) return 'just now'
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`
  return `${Math.floor(diff / 86400)}d ago`
}

interface Props {
  /** WebSocket data — parent passes WS messages down so the bell reacts instantly */
  wsMessage?: { type: string; data: unknown } | null
  /** When true the sidebar is collapsed — render icon-only button */
  collapsed?: boolean
}

export default function NotificationBell({ wsMessage, collapsed }: Props) {
  const [open, setOpen] = useState(false)
  const [notifications, setNotifications] = useState<Notif[]>([])
  const [unread, setUnread] = useState(0)
  const dropdownRef = useRef<HTMLDivElement>(null)

  const fetchNotifications = useCallback(async () => {
    try {
      const [listRes, countRes] = await Promise.all([
        axios.get('/api/notifications/?limit=25'),
        axios.get('/api/notifications/unread-count'),
      ])
      setNotifications(listRes.data.notifications || [])
      setUnread(countRes.data.unread_count ?? 0)
    } catch { /* silent */ }
  }, [])

  // Initial load + poll every 30 s
  useEffect(() => {
    fetchNotifications()
    const tid = setInterval(fetchNotifications, 30_000)
    return () => clearInterval(tid)
  }, [fetchNotifications])

  // React to live WS "notification" events
  useEffect(() => {
    if (wsMessage?.type === 'notification') {
      fetchNotifications()
    }
  }, [wsMessage, fetchNotifications])

  // Close dropdown on outside click
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (dropdownRef.current && !dropdownRef.current.contains(e.target as Node)) {
        setOpen(false)
      }
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [])

  const markRead = async (id: number) => {
    await axios.post(`/api/notifications/${id}/read`).catch(() => null)
    setNotifications(prev => prev.map(n => n.id === id ? { ...n, is_read: true } : n))
    setUnread(prev => Math.max(0, prev - 1))
  }

  const markAllRead = async () => {
    await axios.post('/api/notifications/read-all').catch(() => null)
    setNotifications(prev => prev.map(n => ({ ...n, is_read: true })))
    setUnread(0)
  }

  const deleteOne = async (e: React.MouseEvent, id: number) => {
    e.stopPropagation()
    await axios.delete(`/api/notifications/${id}`).catch(() => null)
    setNotifications(prev => prev.filter(n => n.id !== id))
    setUnread(prev => {
      const wasUnread = notifications.find(n => n.id === id && !n.is_read)
      return wasUnread ? Math.max(0, prev - 1) : prev
    })
  }

  return (
    <div className="relative" ref={dropdownRef}>
      {/* Bell button */}
      <button
        onClick={() => { setOpen(o => !o); if (!open) fetchNotifications() }}
        title={collapsed ? 'Notifications' : undefined}
        className={`relative w-full flex items-center rounded-lg text-gray-400 hover:bg-dark-700 hover:text-gray-200 text-sm transition-all ${
          collapsed ? 'justify-center px-3 py-2.5' : 'gap-2 px-3 py-2.5'
        }`}
      >
        <Bell size={16} className="shrink-0" />
        {!collapsed && <span>Notifications</span>}
        {unread > 0 && (
          <span className={`min-w-[18px] h-[18px] flex items-center justify-center rounded-full bg-brand-500 text-white text-[10px] font-bold px-1 ${
            collapsed ? 'absolute -top-1 -right-1' : 'ml-auto'
          }`}>
            {unread > 99 ? '99+' : unread}
          </span>
        )}
      </button>

      {/* Dropdown */}
      {open && (
        <div className={`absolute w-80 bg-dark-800 border border-dark-600 rounded-xl shadow-2xl z-50 overflow-hidden ${
          collapsed ? 'left-full ml-2 bottom-0' : 'bottom-full left-0 mb-2'
        }`}>
          {/* Header */}
          <div className="flex items-center justify-between px-4 py-3 border-b border-dark-600">
            <span className="text-sm font-semibold text-white">Notifications</span>
            {unread > 0 && (
              <button
                onClick={markAllRead}
                className="flex items-center gap-1 text-xs text-gray-400 hover:text-brand-400 transition-colors"
              >
                <CheckCheck size={13} />
                Mark all read
              </button>
            )}
          </div>

          {/* List */}
          <div className="max-h-80 overflow-y-auto">
            {notifications.length === 0 ? (
              <div className="p-6 text-center text-gray-500 text-sm">
                <Bell size={24} className="mx-auto mb-2 opacity-30" />
                No notifications yet
              </div>
            ) : (
              notifications.map(n => (
                <div
                  key={n.id}
                  onClick={() => !n.is_read && markRead(n.id)}
                  className={`group relative flex items-start gap-3 px-4 py-3 border-b border-dark-700 last:border-0 cursor-pointer transition-colors ${
                    n.is_read ? 'hover:bg-dark-700/50' : 'bg-dark-700/40 hover:bg-dark-700/70'
                  }`}
                >
                  {/* Unread dot */}
                  {!n.is_read && (
                    <div className={`absolute left-2 top-4 w-1.5 h-1.5 rounded-full ${LEVEL_DOT[n.level] ?? 'bg-gray-400'}`} />
                  )}

                  {/* Icon */}
                  <div className={`mt-0.5 p-1.5 rounded-lg border text-xs ${LEVEL_COLORS[n.level] ?? LEVEL_COLORS.info}`}>
                    <CategoryIcon cat={n.category} />
                  </div>

                  {/* Content */}
                  <div className="flex-1 min-w-0">
                    <p className={`text-xs font-semibold truncate ${n.is_read ? 'text-gray-400' : 'text-white'}`}>
                      {n.title}
                    </p>
                    <p className="text-xs text-gray-500 mt-0.5 line-clamp-2">{n.message}</p>
                    <p className="text-[10px] text-gray-600 mt-1">{timeAgo(n.created_at)}</p>
                  </div>

                  {/* Delete */}
                  <button
                    onClick={e => deleteOne(e, n.id)}
                    className="opacity-0 group-hover:opacity-100 p-1 rounded hover:bg-dark-600 text-gray-500 hover:text-gray-300 transition-all"
                  >
                    <X size={11} />
                  </button>
                </div>
              ))
            )}
          </div>

          {/* Footer */}
          {notifications.length > 0 && (
            <div className="px-4 py-2 border-t border-dark-600 text-center">
              <button
                onClick={async () => {
                  await axios.delete('/api/notifications/clear/read').catch(() => null)
                  await fetchNotifications()
                }}
                className="text-xs text-gray-500 hover:text-gray-300 transition-colors"
              >
                Clear read notifications
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
