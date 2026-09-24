import React, { useEffect, useState } from 'react'
import { AreaChart, Area, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid } from 'recharts'

// Live violations-rate chart: warn vs critical per second over the last
// ~90s. Shows the PLUME'S SHAPE — baseline flat, ignition ramp, critical
// spike, decay — which the wall and feed don't convey. Operators only.
export function RateChart() {
  const [data, setData] = useState([])
  useEffect(() => {
    let alive = true
    const poll = async () => {
      try {
        const d = await fetch('/api/violations/rate').then((r) => r.json())
        if (alive) setData(d)
      } catch { /* keep last */ }
    }
    poll()
    const t = setInterval(poll, 1500)
    return () => { alive = false; clearInterval(t) }
  }, [])

  const total = data.reduce((s, p) => s + p.warn + p.critical, 0)

  return (
    <section className="rate-panel">
      <div className="rate-head">
        <h2 className="panel-title">violation rate</h2>
        <span className="rate-sub">warn vs critical · per second · last 90s</span>
      </div>
      {total === 0 ? (
        <div className="empty">quiet. run a simulation to see the plume take shape.</div>
      ) : (
        <ResponsiveContainer width="100%" height={200}>
          <AreaChart data={data} margin={{ top: 8, right: 12, left: -18, bottom: 0 }}>
            <defs>
              <linearGradient id="gWarn" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor="#f2a54a" stopOpacity={0.5} />
                <stop offset="100%" stopColor="#f2a54a" stopOpacity={0.05} />
              </linearGradient>
              <linearGradient id="gCrit" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor="#f2634e" stopOpacity={0.6} />
                <stop offset="100%" stopColor="#f2634e" stopOpacity={0.05} />
              </linearGradient>
            </defs>
            <CartesianGrid strokeDasharray="3 3" stroke="#2a2833" vertical={false} />
            <XAxis dataKey="t" tick={{ fill: '#8b8794', fontSize: 10 }}
                   tickFormatter={(t) => (t === 0 ? 'now' : `${t}s`)}
                   interval={14} axisLine={{ stroke: '#3a3745' }} />
            <YAxis tick={{ fill: '#8b8794', fontSize: 10 }} axisLine={false} tickLine={false}
                   allowDecimals={false} width={34} />
            <Tooltip contentStyle={{ background: '#1e1c24', border: '1px solid #3a3745',
                     borderRadius: 8, fontSize: 12 }}
                     labelFormatter={(t) => (t === 0 ? 'now' : `${t}s ago`)} />
            <Area type="monotone" dataKey="warn" stackId="1" stroke="#f2a54a"
                  fill="url(#gWarn)" strokeWidth={1.5} name="warn" isAnimationActive={false} />
            <Area type="monotone" dataKey="critical" stackId="1" stroke="#f2634e"
                  fill="url(#gCrit)" strokeWidth={2} name="critical" isAnimationActive={false} />
          </AreaChart>
        </ResponsiveContainer>
      )}
    </section>
  )
}
