import { Routes, Route, NavLink } from 'react-router-dom'
import { Toaster } from 'react-hot-toast'
import {
  LayoutDashboard, FlaskConical, Play, Settings,
  Layers, Zap, ShieldAlert
} from 'lucide-react'
import clsx from 'clsx'

import Dashboard from './pages/Dashboard'
import Backtest from './pages/Backtest'
import ForwardTest from './pages/ForwardTest'
import Strategies from './pages/Strategies'
import SettingsPage from './pages/Settings'

const navItems = [
  { to: '/', label: 'Dashboard', icon: LayoutDashboard, end: true },
  { to: '/strategies', label: 'Strategies', icon: Layers },
  { to: '/backtest', label: 'Backtest', icon: FlaskConical },
  { to: '/forward-test', label: 'Forward Test', icon: Play },
  { to: '/settings', label: 'Settings', icon: Settings },
]

export default function App() {
  return (
    <div className="flex h-screen overflow-hidden">
      {/* Sidebar */}
      <aside className="w-56 bg-dark-800 border-r border-dark-600 flex flex-col">
        {/* Logo */}
        <div className="px-4 py-5 border-b border-dark-600">
          <div className="flex items-center gap-2">
            <Zap className="text-brand-500" size={22} />
            <span className="font-bold text-white text-sm tracking-wide">AI Bot Trader</span>
          </div>
          <p className="text-xs text-gray-500 mt-0.5">v0.1.0 — Phase 1</p>
        </div>

        {/* Nav */}
        <nav className="flex-1 px-2 py-4 space-y-1">
          {navItems.map(({ to, label, icon: Icon, end }) => (
            <NavLink
              key={to}
              to={to}
              end={end}
              className={({ isActive }) => clsx(
                'flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm transition-all',
                isActive
                  ? 'bg-brand-500/10 text-brand-500 font-medium'
                  : 'text-gray-400 hover:bg-dark-700 hover:text-gray-200'
              )}
            >
              <Icon size={16} />
              {label}
            </NavLink>
          ))}
        </nav>

        {/* Emergency Stop */}
        <div className="px-2 pb-4">
          <button
            className="w-full flex items-center gap-2 px-3 py-2.5 rounded-lg bg-red-900/30 text-red-400 hover:bg-red-900/50 hover:text-red-300 text-sm font-medium transition-all border border-red-900/50"
            onClick={() => {
              if (confirm('⚠️ Emergency Stop: Close ALL positions?')) {
                fetch('/api/positions/emergency-stop', { method: 'POST' })
                  .then(() => alert('Emergency stop executed.'))
              }
            }}
          >
            <ShieldAlert size={16} />
            Emergency Stop
          </button>
        </div>
      </aside>

      {/* Main content */}
      <main className="flex-1 overflow-y-auto">
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/strategies" element={<Strategies />} />
          <Route path="/backtest" element={<Backtest />} />
          <Route path="/forward-test" element={<ForwardTest />} />
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
