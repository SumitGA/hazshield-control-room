import React, { useEffect, useState } from 'react'
import { useLive } from './useLive.js'
import { SimLauncher, Legend } from './SimLauncher.jsx'
import { PlansPanel } from './PlansPanel.jsx'
import { RateChart } from './RateChart.jsx'
import { AuthBar } from './AuthBar.jsx'
import { useAuth } from './useAuth.js'

// severity/state -> annunciator lamp class
function lamp(zoneEpisodes) {
  if (!zoneEpisodes.length) return 'quiet'
  if (zoneEpisodes.some((e) => e.state === 'escalated' || e.severity === 'critical'))
    return 'critical'
  return 'warn'
}

function Cascade({ stats }) {
  if (!stats) return null
  const cells = [
    ['violations', stats.stream_len],
    ['episodes', stats.episodes_total],
    ['plans', stats.plans_total],
    ['generations', stats.generations],
  ]
  return (
    <div className="cascade" title="the compression cascade, live">
      {cells.map(([label, n], i) => (
        <React.Fragment key={label}>
          {i > 0 && <span className="cascade-arrow">→</span>}
          <div className="cascade-cell">
            <div className="cascade-n">{Number(n).toLocaleString()}</div>
            <div className="cascade-label">{label}</div>
          </div>
        </React.Fragment>
      ))}
    </div>
  )
}

function Wall({ topology, episodes, flashes, lampTest, onPick }) {
  if (!topology) return <div className="empty">loading plant topology…</div>
  const bySite = {}
  topology.zones.forEach((z) => { (bySite[z.site] ||= []).push(z) })
  const epByZone = {}
  episodes.filter((e) => !e.cleared).forEach((e) => { (epByZone[e.zone_id] ||= []).push(e) })
  return (
    <div className="wall">
      {Object.entries(bySite).map(([site, zones]) => (
        <section key={site} className="site">
	  <div className="site-head">
            <h2 className="site-name">{site}</h2>
            <Legend />
          </div>
          <div className="tiles">
            {zones.map((z) => {
              const eps = epByZone[z.zone_id] || []
              const hit = flashes[z.zone_id] && Date.now() - flashes[z.zone_id] < 400
              const planning = eps.some((e) => e.plan_status === 'pending' || e.plan_status === 'generating')
              return (
                <button
                  key={z.zone_id}
                  className={`tile ${lampTest ? 'lamp-test' : lamp(eps)} ${hit ? 'hit' : ''} ${planning ? 'planning' : ''}`}
                  onClick={() => eps.length && onPick(eps[0])}
                  title={`${z.name} · ${z.sensors} sensors`}
                >
                  <span className="tile-name">{z.name}</span>
                  <span className="tile-meta">
                    {eps.length
                      ? `${eps.length} open · peak ${Math.round(Math.max(...eps.map((e) => e.peak_value)))}`
                      : `${z.sensors} sensors`}
                  </span>
                </button>
              )
            })}
          </div>
        </section>
      ))}
    </div>
  )
}

function Feed({ feed }) {
  return (
    <aside className="feed">
      <h2 className="panel-title">live violations</h2>
      {feed.length === 0 && <div className="empty">quiet. the plant is behaving.</div>}
      <ul>
        {feed.map((v) => (
          <li key={v._k} className={`feed-row ${v.severity}`}>
            <span className="feed-sev">{v.severity === 'critical' ? 'CRIT' : 'WARN'}</span>
            <span className="feed-kind">{v.sensor_kind || 'sensor'}</span>
            <span className="feed-val">{typeof v.value === 'number' ? v.value.toFixed(1) : v.value}</span>
          </li>
        ))}
      </ul>
    </aside>
  )
}

function Board({ episodes, onPick, picked }) {
  const open = episodes.filter((e) => !e.cleared)
  const recent = episodes.filter((e) => e.cleared)
  return (
    <section className="board">
      <h2 className="panel-title">episodes</h2>
      {open.length === 0 && <div className="empty">no open episodes.</div>}
      <table>
        <tbody>
          {open.map((e) => (
            <tr key={e.alarm_id}
                className={`ep ${e.severity} ${picked?.alarm_id === e.alarm_id ? 'picked' : ''}`}
                onClick={() => onPick(e)}>
              <td className="ep-state">{e.state}</td>
              <td>{e.zone_name}</td>
              <td>{e.sensor_kind}</td>
              <td className="num">peak {Math.round(e.peak_value)}</td>
              <td className="num">×{e.n_readings}</td>
              <td className="num">{e.age_s}s</td>
              <td className={`ep-plan ${e.plan_model || ''}`}>{e.plan_model ? `plan: ${e.plan_model}` : ''}</td>
            </tr>
          ))}
          {recent.slice(0, 6).map((e) => (
            <tr key={e.alarm_id} className="ep cleared" onClick={() => onPick(e)}>
              <td className="ep-state">cleared</td>
              <td>{e.zone_name}</td>
              <td>{e.sensor_kind}</td>
              <td className="num">peak {Math.round(e.peak_value)}</td>
              <td className="num">×{e.n_readings}</td>
              <td className="num" colSpan="2"></td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  )
}

function PlanDrawer({ episode, onClose }) {
  const [plan, setPlan] = useState(null)
  const [state, setState] = useState('loading')
  useEffect(() => {
    if (!episode) return
    setState('loading'); setPlan(null)
    fetch(`/api/plans/${episode.alarm_id}`)
      .then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
      .then((p) => { setPlan(p); setState('ok') })
      .catch(() => setState('none'))
  }, [episode])
  if (!episode) return null
  return (
    <div className="drawer" role="dialog" aria-label="isolation plan">
      <div className="drawer-head">
        <h2 className="panel-title">isolation plan · {episode.zone_name}</h2>
        <button className="close" onClick={onClose} aria-label="close">✕</button>
      </div>
      {state === 'loading' && <div className="empty">fetching plan…</div>}
      {state === 'none' && <div className="empty">no plan for this episode. warn-level episodes are watchlist only.</div>}
      {plan && (
        <>
          <div className="plan-meta">
            <span className={`model-badge ${plan.model}`}>{plan.model}</span>
            <span className="plan-status">{plan.status}</span>
            {plan.latency_ms != null && <span className="plan-lat">{plan.latency_ms.toLocaleString()} ms</span>}
          </div>
          {plan.plan?.summary && <p className="plan-summary">{plan.plan.summary}</p>}
          <ol className="steps">
            {(plan.plan?.steps || []).map((s) => (
              <li key={s.order}>
                <span className="step-action">{s.action}</span>
                {s.target && <span className="step-target">{s.target}</span>}
                {s.reason && <span className="step-reason">{s.reason}</span>}
              </li>
            ))}
          </ol>
          {plan.plan?.evacuation_route && (
            <p className="plan-note"><b>evacuate:</b> {plan.plan.evacuation_route}</p>
          )}
          {plan.plan?.ventilation_note && (
            <p className="plan-note"><b>ventilation:</b> {plan.plan.ventilation_note}</p>
          )}
        </>
      )}
    </div>
  )
}

export default function App() {
  const { connected, feed, flashes, episodes, stats, topology } = useLive()
  const { operator, login, logout } = useAuth()
  const [picked, setPicked] = useState(null)
  const [lampTest, setLampTest] = useState(true)
  useEffect(() => {                       // power-up lamp test, once
    const t = setTimeout(() => setLampTest(false), 900)
    return () => clearTimeout(t)
  }, [])
  return (
    <div className="room">
      <header className="head">
        <h1 className="wordmark">HAZSHIELD <span className="wordmark-sub">CONTROL ROOM</span></h1>
        <Cascade stats={stats} />
        <SimLauncher operator={operator} />
        <AuthBar operator={operator} login={login} logout={logout} />
        <div className={`link ${connected ? 'up' : 'down'}`}>
          <span className="link-dot" />{connected ? 'LIVE' : 'RECONNECTING'}
        </div>
      </header>
      <main className="main">
        <Wall topology={topology} episodes={episodes} flashes={flashes}
              lampTest={lampTest} onPick={setPicked} />
        <Feed feed={feed} />
      </main>
      {operator && <RateChart />}
      <PlansPanel onPick={setPicked} />
      <Board episodes={episodes} onPick={setPicked} picked={picked} />
      <PlanDrawer episode={picked} onClose={() => setPicked(null)} />
      {stats?.dlq_len > 0 && (
        <div className="dlq-strip">DLQ holds {stats.dlq_len} quarantined entries · inspect with dlq.py</div>
      )}
    </div>
  )
}
