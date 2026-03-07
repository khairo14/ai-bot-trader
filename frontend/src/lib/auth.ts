/**
 * Auth utilities — token storage and axios interceptor setup.
 *
 * F-056: JWT is now stored in an httpOnly cookie set by the backend.
 * The frontend only stores the non-sensitive username in sessionStorage for
 * UI display. The cookie is sent automatically by the browser on every request.
 * The backend still returns `access_token` in the login response body for
 * Swagger UI compatibility; the frontend ignores it.
 */
import axios from 'axios'

const USERNAME_KEY = 'auth_username'

/** @deprecated JWT is now in httpOnly cookie — do not call directly */
export function getToken(): string | null {
  return null  // token lives in httpOnly cookie, not accessible to JS
}

export function getUsername(): string | null {
  try { return localStorage.getItem(USERNAME_KEY) } catch { return null }
}

export function setAuth(_token: string, username: string): void {
  try {
    // token is set as httpOnly cookie by the backend — store only username
    // localStorage is shared across tabs; sessionStorage was per-tab only
    localStorage.setItem(USERNAME_KEY, username)
  } catch { /* ignore in SSR / private browsing */ }
}

export function clearAuth(): void {
  try {
    localStorage.removeItem(USERNAME_KEY)
  } catch { /* ignore */ }
}

export function isAuthenticated(): boolean {
  // Rely on server-side cookie validation; use username presence as UI hint
  return !!getUsername()
}

/**
 * Install axios interceptors once on app startup.
 * - Request: cookies are sent automatically by the browser (httpOnly cookie, F-056)
 * - Response: force logout on 401
 */
export function setupAxiosInterceptors(): void {
  // Ensure cookies are sent cross-origin in development (same-origin in production)
  axios.defaults.withCredentials = true

  axios.interceptors.response.use(
    res => res,
    err => {
      if (err?.response?.status === 401) {
        clearAuth()
        if (typeof window !== 'undefined' && window.location.pathname !== '/login') {
          window.location.href = '/login'
        }
      }
      return Promise.reject(err)
    }
  )
}
