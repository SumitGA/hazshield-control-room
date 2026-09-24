import React, { useState, useEffect } from 'react'

const PRESET_INFO = {
  gentle:    { label: 'Gentle',    desc: 'A stirring — mostly warnings, few criticals.' },
  realistic: { label: 'Realistic', desc: 'The real demo — warnings, criticals, AI plans.' },
  severe:    { label: 'Severe',    desc: 'Heavy cascade — many criticals, plans queueing.' },
}
const PHASE_TEXT = {
  starting:   'Spinning up the plume simulator...',
  baseline:   'Plant running normally. Thousands of sensor readings flowing every second — none dangerous yet.',
  igniting:   'A methane leak is starting in one zone. Gas sensors there are climbing toward their warning line.',
  escalating: 'The leak is now critical. Sensors have crossed the danger threshold — the system is opening incidents and the AI is writing isolation plans.',
  draining:   'Leak contained. The last isolation plans are finishing generation.',
}

export function SimLauncher({ operator }) {
  const [status, setStatus] = useState(null)
  const [open, setOpen] = useState(false)
  const [advanced, setAdvanced] = useState(false)
  const [preset, setPreset] = useState('realistic')
  const [rate, setRate] = useState(1200)
  const [duration, setDuration] = useState(90)
  const [scenario, setScenario] = useState('plume')
  const [msg, setMsg] = useState(null)

  useEffect(() => {
    let alive = true
    const poll = async () => {
      try {
        const s = await fetch('/api/sim/status').then(r => r.json())
        if (alive) setStatus(s)
      } catch { /* keep last */ }
    }
    poll()
    const t = setInterval(poll, 1500)
    return () => { alive = false; clearInterval(t) }
  }, [])

  const state = status?.state || 'ready'
  const bounds = status?.bounds || { rate: [200, 2000], duration: [30, 120] }

  async function launch() {
    setMsg(null)
    const body = advanced ? { rate, duration, scenario } : { preset }
    try {
      const res = await fetch('/api/sim/start', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      const data = await res.json()
      if (res.status === 202) { setOpen(false); setMsg(null) }
      else if (data.error === 'cooldown') setMsg(`Cooling down — try again in ${data.retry_after_s}s.`)
      else if (data.error === 'already_running') setMsg('A simulation is already running.')
      else setMsg('Could not start.')
    } catch { setMsg('Network error.') }
  }

  // RUNNING — visible to everyone (watch the story unfold)
  if (state === 'running') {
    const p = status.progress || {}
    const pct = p.duration_s ? Math.min(100, Math.round((p.elapsed_s / p.duration_s) * 100)) : 0
    return (
      <div className="sim-narrate">
        <div className="sim-narrate-head">
          <span className="sim-live-dot" />
          SIMULATION RUNNING
          <span className="sim-phase-tag">{(p.phase || 'running').toUpperCase()}</span>
        </div>
        <p className="sim-story">{PHASE_TEXT[p.phase] || 'Simulation in progress...'}</p>
        <div className="sim-bar"><div className="sim-bar-fill" style={{ width: `${pct}%` }} /></div>
        <div className="sim-meta">{p.elapsed_s || 0}s / {p.duration_s || '?'}s · {p.scenario} · {p.rate}/s</div>
      </div>
    )
  }

  if (state === 'cooldown') {
    return (
      <div className="sim-cooldown">
        <span className="sim-cd-dot" />
        Plant recovering — new simulation available in {status.cooldown_s}s
      </div>
    )
  }

  // READY — the START action is gated to logged-in operators
  if (!operator) {
    return (
      <div className="sim-launcher">
        <span className="sim-gated">Log in to run a simulation</span>
      </div>
    )
  }

  return (
    <div className="sim-launcher">
      {!open ? (
        <button className="sim-cta" onClick={() => setOpen(true)}>▶ Run a Simulation</button>
      ) : (
        <div className="sim-form">
          <div className="sim-form-head">
            <span>Run a plume simulation</span>
            <button className="sim-x" onClick={() => setOpen(false)}>✕</button>
          </div>
          {!advanced ? (
            <div className="sim-presets">
              {Object.entries(PRESET_INFO).map(([key, info]) => (
                <button key={key} className={`sim-preset ${preset === key ? 'sel' : ''}`}
                        onClick={() => setPreset(key)}>
                  <span className="sim-preset-label">{info.label}</span>
                  <span className="sim-preset-desc">{info.desc}</span>
                </button>
              ))}
            </div>
          ) : (
            <div className="sim-advanced">
              <label className="sim-slider">
                <span>Intensity (readings/sec): <b>{rate}</b></span>
                <input type="range" min={bounds.rate[0]} max={bounds.rate[1]} step="100"
                       value={rate} onChange={e => setRate(+e.target.value)} />
                <span className="sim-slider-ends">{bounds.rate[0]} … {bounds.rate[1]}</span>
              </label>
              <label className="sim-slider">
                <span>Duration (seconds): <b>{duration}</b></span>
                <input type="range" min={bounds.duration[0]} max={bounds.duration[1]} step="5"
                       value={duration} onChange={e => setDuration(+e.target.value)} />
                <span className="sim-slider-ends">{bounds.duration[0]} … {bounds.duration[1]}</span>
              </label>
              <label className="sim-scenario">
                <span>Scenario</span>
                <select value={scenario} onChange={e => setScenario(e.target.value)}>
                  <option value="plume">Gas plume (one zone)</option>
                  <option value="plume+flood">Plume + sensor flood</option>
                </select>
              </label>
              <p className="sim-bound-note">Values are capped to what the system can safely handle.</p>
            </div>
          )}
          <div className="sim-form-foot">
            <button className="sim-adv-toggle" onClick={() => setAdvanced(a => !a)}>
              {advanced ? '← Simple presets' : 'Advanced ⚙'}
            </button>
            <button className="sim-go" onClick={launch}>Launch</button>
          </div>
          {msg && <div className="sim-msg">{msg}</div>}
        </div>
      )}
    </div>
  )
}

export function Legend() {
  return (
    <div className="legend">
      <span className="legend-item"><i className="lg lg-quiet" /> quiet</span>
      <span className="legend-item"><i className="lg lg-warn" /> warning</span>
      <span className="legend-item"><i className="lg lg-crit" /> critical</span>
      <span className="legend-item"><i className="lg lg-plan" /> planning</span>
    </div>
  )
}
