import React, { useEffect, useState } from 'react'

// A live panel showing the AI-generated isolation plans as they land.
// Polls /api/plans/recent. Click a plan to open the full drawer.
export function PlansPanel({ onPick }) {
  const [plans, setPlans] = useState([])
  useEffect(() => {
    let alive = true
    const poll = async () => {
      try {
        const r = await fetch('/api/plans/recent').then(r => r.json())
        if (alive) setPlans(r)
      } catch { /* keep last */ }
    }
    poll()
    const t = setInterval(poll, 2500)
    return () => { alive = false; clearInterval(t) }
  }, [])

  return (
    <section className="plans-panel">
      <h2 className="panel-title">isolation plans</h2>
      {plans.length === 0 && (
        <div className="empty">no plans yet. run a plume and watch the AI respond to criticals.</div>
      )}
      <div className="plan-cards">
        {plans.map((p) => (
          <button key={p.plan_id} className={`plan-card ${p.model}`}
                  onClick={() => onPick && onPick({ alarm_id: p.alarm_id, zone_name: p.zone_name })}>
            <div className="plan-card-head">
              <span className={`model-badge ${p.model}`}>{p.model}</span>
              <span className="plan-card-zone">{p.zone_name}</span>
              {p.latency_ms != null && (
                <span className="plan-card-lat">
                  {p.model === 'cache' ? 'cached' : `${(p.latency_ms / 1000).toFixed(0)}s`}
                </span>
              )}
            </div>
            {p.plan?.summary && <p className="plan-card-summary">{p.plan.summary}</p>}
            {p.plan?.steps && (
              <span className="plan-card-steps">{p.plan.steps.length} isolation steps →</span>
            )}
          </button>
        ))}
      </div>
    </section>
  )
}
