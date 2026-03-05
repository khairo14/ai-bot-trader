import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import axios from 'axios'
import { Zap, Eye, EyeOff } from 'lucide-react'
import { setAuth } from '../lib/auth'

interface LoginProps {
  onLogin?: () => void
}

export default function Login({ onLogin }: LoginProps) {
  const navigate = useNavigate()
  const [mode, setMode]         = useState<'login' | 'register'>('login')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [confirm,  setConfirm]  = useState('')
  const [showPwd,  setShowPwd]  = useState(false)
  const [loading,  setLoading]  = useState(false)
  const [error,    setError]    = useState<string | null>(null)
  const [success,  setSuccess]  = useState<string | null>(null)

  const switchMode = (m: 'login' | 'register') => {
    setMode(m); setError(null); setSuccess(null)
    setUsername(''); setPassword(''); setConfirm('')
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setError(null)
    setSuccess(null)

    if (mode === 'register' && password !== confirm) {
      setError('Passwords do not match')
      return
    }

    setLoading(true)
    try {
      if (mode === 'login') {
        const res = await axios.post('/auth/login', {
          username: username.trim().toLowerCase(),
          password,
        })
        setAuth(res.data.access_token, res.data.username)
        onLogin?.()
        navigate('/', { replace: true })
      } else {
        await axios.post('/auth/register', {
          username: username.trim().toLowerCase(),
          password,
        })
        setSuccess('Account created! You can now sign in.')
        switchMode('login')
      }
    } catch (err: any) {
      const detail = err?.response?.data?.detail
      setError(
        Array.isArray(detail)
          ? detail.map((d: any) => d.msg ?? d).join(', ')
          : (detail ?? (mode === 'login' ? 'Login failed — check your credentials' : 'Registration failed'))
      )
    } finally {
      setLoading(false)
    }
  }

  const isLogin = mode === 'login'

  return (
    <div className="min-h-screen bg-dark-900 flex items-center justify-center px-4">
      <div className="w-full max-w-sm">

        {/* Logo */}
        <div className="flex flex-col items-center mb-8">
          <div className="p-3 rounded-2xl bg-brand-500/10 border border-brand-500/20 mb-4">
            <Zap className="text-brand-500" size={30} />
          </div>
          <h1 className="text-2xl font-bold text-white tracking-tight">AI Bot Trader</h1>
          <p className="text-sm text-gray-500 mt-1">{isLogin ? 'Sign in to your dashboard' : 'Create a new account'}</p>
        </div>

        {/* Mode tabs */}
        <div className="flex bg-dark-800 border border-dark-600 rounded-xl p-1 mb-4">
          {(['login', 'register'] as const).map(m => (
            <button
              key={m}
              type="button"
              onClick={() => switchMode(m)}
              className={`flex-1 py-2 text-sm font-medium rounded-lg transition-all ${
                mode === m
                  ? 'bg-brand-500 text-white shadow'
                  : 'text-gray-400 hover:text-gray-200'
              }`}
            >
              {m === 'login' ? 'Sign in' : 'Create account'}
            </button>
          ))}
        </div>

        {/* Success banner */}
        {success && (
          <p className="text-sm text-green-400 bg-green-900/20 border border-green-900/40 rounded-xl px-3 py-2 mb-4 text-center">
            {success}
          </p>
        )}

        {/* Card */}
        <div className="bg-dark-800 border border-dark-600 rounded-2xl p-6 shadow-2xl">
          <form onSubmit={handleSubmit} className="space-y-4">

            {/* Username */}
            <div>
              <label className="block text-xs font-medium text-gray-400 mb-1.5">Username</label>
              <input
                type="text"
                value={username}
                onChange={e => setUsername(e.target.value)}
                required
                autoComplete="username"
                autoFocus
                placeholder="your-username"
                className="w-full bg-dark-700 border border-dark-500 text-gray-100 text-sm rounded-xl px-4 py-3 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent placeholder-gray-600 transition-all"
              />
            </div>

            {/* Password */}
            <div>
              <label className="block text-xs font-medium text-gray-400 mb-1.5">Password</label>
              <div className="relative">
                <input
                  type={showPwd ? 'text' : 'password'}
                  value={password}
                  onChange={e => setPassword(e.target.value)}
                  required
                  autoComplete={isLogin ? 'current-password' : 'new-password'}
                  placeholder="••••••••"
                  className="w-full bg-dark-700 border border-dark-500 text-gray-100 text-sm rounded-xl px-4 py-3 pr-11 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent placeholder-gray-600 transition-all"
                />
                <button
                  type="button"
                  onClick={() => setShowPwd(v => !v)}
                  className="absolute right-3 top-1/2 -translate-y-1/2 text-gray-500 hover:text-gray-300 transition-colors"
                >
                  {showPwd ? <EyeOff size={16} /> : <Eye size={16} />}
                </button>
              </div>
              {!isLogin && <p className="text-[11px] text-gray-600 mt-1">Minimum 8 characters</p>}
            </div>

            {/* Confirm password (register only) */}
            {!isLogin && (
              <div>
                <label className="block text-xs font-medium text-gray-400 mb-1.5">Confirm password</label>
                <input
                  type={showPwd ? 'text' : 'password'}
                  value={confirm}
                  onChange={e => setConfirm(e.target.value)}
                  required
                  autoComplete="new-password"
                  placeholder="••••••••"
                  className="w-full bg-dark-700 border border-dark-500 text-gray-100 text-sm rounded-xl px-4 py-3 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent placeholder-gray-600 transition-all"
                />
              </div>
            )}

            {/* Error */}
            {error && (
              <p className="text-sm text-red-400 bg-red-900/20 border border-red-900/40 rounded-xl px-3 py-2">
                {error}
              </p>
            )}

            {/* Submit */}
            <button
              type="submit"
              disabled={loading}
              className="w-full py-3 rounded-xl bg-brand-500 hover:bg-brand-600 active:scale-[0.99] text-white text-sm font-semibold disabled:opacity-50 transition-all mt-2"
            >
              {loading
                ? (isLogin ? 'Signing in…' : 'Creating account…')
                : (isLogin ? 'Sign in' : 'Create account')}
            </button>
          </form>
        </div>

        {!isLogin && (
          <p className="text-xs text-gray-600 text-center mt-4">
            The first registered account is automatically granted admin access.
          </p>
        )}
      </div>
    </div>
  )
}
