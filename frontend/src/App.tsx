import { useState } from 'react'
import { Routes, Route, NavLink } from 'react-router-dom'
import { Toaster } from 'react-hot-toast'
import toast from 'react-hot-toast'
import {
  LayoutDashboard, FlaskConical, Play, Settings,
  Layers, Zap, ShieldAlert, BookOpen, ChevronLeft, ChevronRight, BarChart2
} from 'lucide-react'
import clsx from 'clsx'

import NotificationBell from './components/NotificationBell'
import Dashboard from './pages/Dashboard'
import Backtest from './pages/Backtest'
import ForwardTest from './pages/ForwardTest'
import Strategies from './pages/Strategies'
import StrategyLibrary from './pages/StrategyLibrary'
import SettingsPage from './pages/Settings'
import ChartPage from './pages/Chart'

const navItems = [
  { to: '/', label: 'Dashboard', icon: LayoutDashboard, end: true },
  { to: '/strategies', label: 'Strategies', icon: Layers },
  { to: '/strategy-library', label: 'Library', icon: BookOpen },
  { to: '/backtest', label: 'Backtest', icon: FlaskConical },
  { to: '/forward-test', label: 'Forward Test', icon: Play },
  { to: '/chart', label: 'Chart', icon: BarChart2 },
  { to: '/settings', label: 'Settings', icon: Settings },
]

export default function App() {
  const [collapsed, setCollapsed] = useState<boolean>(() => {
    try { return localStorage.getItem('nav-collapsed') === 'true' } catch { return false }
  })

  const toggleCollapsed = () => {
    setCollapsed(prev => {
      const next = !prev
      try { localStorage.setItem('nav-collapsed', String(next)) } catch {}
      return next
    })
  }

  return (
    <div className="flex h-screen overflow-hidden">
      {/* Sidebar */}
      <aside
        className={clsx(
          'bg-dark-800 border-r border-dark-600 flex flex-col transition-all duration-200 shrink-0',
          collapsed ? 'w-14' : 'w-56'
        )}
      >
        {/* Logo + collapse toggle */}
        <div className={clsx(
          'border-b border-dark-600 flex items-center',
          collapsed ? 'px-2 py-4 justify-center' : 'px-4 py-5 justify-between'
        )}>
          {!collapsed && (
            <div>
              <div className="flex items-center gap-2">
                <Zap className="text-brand-500 shrink-0" size={22} />
                <span className="font-bold text-white text-sm tracking-wide">AI Bot Trader</span>
              </div>
              <p className="text-xs text-gray-500 mt-0.5">v0.1.0 — Phase 1</p>
            </div>
          )}
          {collapsed && <Zap className="text-brand-500" size={22} />}
          <button
            onClick={toggleCollapsed}
            title={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
            className={clsx(
              'rounded-lg p-1.5 text-gray-500 hover:text-gray-200 hover:bg-dark-700 transition-all',
              collapsed ? 'mt-4' : ''
            )}
          >
            {collapsed ? <ChevronRight size={14} /> : <ChevronLeft size={14} />}
          </button>
        </div>

        {/* Nav */}
        <nav className="flex-1 px-2 py-4 space-y-1">
          {navItems.map(({ to, label, icon: Icon, end }) => (
            <NavLink
              key={to}
              to={to}
              end={end}
              title={collapsed ? label : undefined}
              className={({ isActive }) => clsx(
                'w-full flex items-center rounded-lg text-sm transition-all',
                collapsed ? 'justify-center py-2.5' : 'gap-3 px-3 py-2.5',
                isActive
                  ? 'bg-brand-500/10 text-brand-500 font-medium'
                  : 'text-gray-400 hover:bg-dark-700 hover:text-gray-200'
              )}
            >
              <Icon size={16} className="shrink-0" />
              {!collapsed && label}
            </NavLink>
          ))}
        </nav>

        {/* Notification Bell */}
        <div className="px-2 pb-2">
          <NotificationBell collapsed={collapsed} />
        </div>

        {/* Emergency Stop */}
        <div className="px-2 pb-4">
          <button
            title="Emergency Stop"
            className={clsx(
              'w-full flex items-center rounded-lg bg-red-900/30 text-red-400 hover:bg-red-900/50 hover:text-red-300 text-sm font-medium transition-all border border-red-900/50',
              collapsed ? 'justify-center py-2.5' : 'gap-2 px-3 py-2.5'
            )}
            onClick={() => {
              toast.custom((t) => (
                <div className="flex flex-col gap-3 p-4 bg-dark-800 border border-red-900/50 rounded-xl text-sm shadow-xl">
                  <p className="font-semibold text-red-400">Emergency Stop</p>
                  <p className="text-gray-300 text-xs leading-relaxed">Close ALL open positions across all brokers immediately?</p>
                  <div className="flex gap-2">
                    <button
                      className="flex-1 py-1.5 rounded-lg bg-red-600 hover:bg-red-500 text-white text-xs font-medium transition-all"
                      onClick={() => {
                        toast.dismiss(t.id)
                        fetch('/api/positions/emergency-stop', { method: 'POST' })
                          .then(() => toast.success('Emergency stop executed.'))
                          .catch(() => toast.error('Emergency stop failed.'))
                      }}
                    >Yes, stop all</button>
                    <button
                      className="flex-1 py-1.5 rounded-lg bg-dark-700 hover:bg-dark-600 text-gray-300 text-xs transition-all border border-dark-500"
                      onClick={() => toast.dismiss(t.id)}
                    >Cancel</button>
                  </div>
                </div>
              ), { duration: Infinity })
            }}
          >
            <ShieldAlert size={16} className="shrink-0" />
            {!collapsed && 'Emergency Stop'}
          </button>
        </div>
      </aside>

      {/* Main content */}
      <main className="flex-1 overflow-y-auto">
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/strategies" element={<Strategies />} />
          <Route path="/strategy-library" element={<StrategyLibrary />} />
          <Route path="/backtest" element={<Backtest />} />
          <Route path="/forward-test" element={<ForwardTest />} />
          <Route path="/chart" element={<ChartPage />} />
          <Route path="/settings" element={<SettingsPage />} />
        </Routes>
      </main>

      <Toaster
        position="top-right"
        toastOptions={{
          style: { background: '#1a1a1a', color: '#f0fdf4', border: '1px solid #333' },
        }}
      />
    </div>
  )
}
