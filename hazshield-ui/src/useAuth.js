import { useState, useEffect, useCallback } from 'react'

// Tracks the logged-in operator via /api/auth/me (reads the httpOnly
// session cookie server-side). The whole UI uses this to know who's
// acting — Phase B's Acknowledge button will gate on operator != null.
export function useAuth() {
  const [operator, setOperator] = useState(null)
  const [checked, setChecked] = useState(false)

  const refresh = useCallback(async () => {
    try {
      const r = await fetch('/api/auth/me').then(r => r.json())
      setOperator(r.operator || null)
    } catch { setOperator(null) }
    finally { setChecked(true) }
  }, [])

  useEffect(() => { refresh() }, [refresh])

  const login = useCallback(async (username, password) => {
    const res = await fetch('/api/auth/login', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
    })
    if (res.status === 200) { await refresh(); return { ok: true } }
    const data = await res.json().catch(() => ({}))
    return { ok: false, error: data.error || 'login failed' }
  }, [refresh])

  const logout = useCallback(async () => {
    await fetch('/api/auth/logout', { method: 'POST' })
    setOperator(null)
  }, [])

  return { operator, checked, login, logout }
}
