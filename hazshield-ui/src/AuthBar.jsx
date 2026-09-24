import React, { useState } from 'react'

export function AuthBar({ operator, login, logout }) {
  const [open, setOpen] = useState(false)
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [err, setErr] = useState(null)
  const [busy, setBusy] = useState(false)

  async function submit(e) {
    e?.preventDefault()
    setErr(null); setBusy(true)
    const r = await login(username.trim(), password)
    setBusy(false)
    if (r.ok) { setOpen(false); setUsername(''); setPassword('') }
    else setErr(r.error)
  }

  if (operator) {
    return (
      <div className="authbar">
        <span className="auth-who">
          <span className="auth-name">{operator.display_name}</span>
          <span className={`auth-role ${operator.role}`}>{operator.role}</span>
        </span>
        <button className="auth-logout" onClick={logout}>log out</button>
      </div>
    )
  }

  return (
    <div className="authbar">
      <button className="auth-login-cta" onClick={() => setOpen(true)}>log in</button>
      {open && (
        <div className="auth-modal-backdrop" onClick={() => setOpen(false)}>
          <form className="auth-modal" onClick={(e) => e.stopPropagation()} onSubmit={submit}>
            <div className="auth-modal-head">
              <span>Operator sign-in</span>
              <button type="button" className="auth-x" onClick={() => setOpen(false)}>✕</button>
            </div>
            <label className="auth-field">
              <span>Username</span>
              <input autoFocus value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" />
            </label>
            <label className="auth-field">
              <span>Password</span>
              <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" />
            </label>
            {err && <div className="auth-err">{err}</div>}
            <button className="auth-submit" type="submit" disabled={busy}>
              {busy ? 'signing in...' : 'Sign in'}
            </button>
            <p className="auth-note">Accounts are provisioned by an administrator.</p>
          </form>
        </div>
      )}
    </div>
  )
}
