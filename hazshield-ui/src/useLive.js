// The control room's nervous system: one SSE subscription for the
// violation firehose, slow polls for the durable truth (episodes,
// stats). Live paints the tiles; polls keep the board honest.
import { useEffect, useRef, useState } from 'react'

export function useLive() {
  const [connected, setConnected] = useState(false)
  const [feed, setFeed] = useState([])            // capped ticker
  const [flashes, setFlashes] = useState({})      // zone_id -> ts of last hit
  const [episodes, setEpisodes] = useState([])
  const [stats, setStats] = useState(null)
  const [topology, setTopology] = useState(null)
  const seq = useRef(0)

  useEffect(() => {                               // SSE
    const es = new EventSource('/events')
    es.onopen = () => setConnected(true)
    es.onerror = () => setConnected(false)        // EventSource retries itself
    es.addEventListener('violation', (e) => {
      try {
        const v = JSON.parse(e.data)
        v._k = ++seq.current
        setFeed((f) => [v, ...f].slice(0, 50))
        if (v.zone_id) setFlashes((z) => ({ ...z, [v.zone_id]: Date.now() }))
      } catch { /* malformed relay entries are ignorable here */ }
    })
    return () => es.close()
  }, [])

  useEffect(() => {                               // durable truth
    let alive = true
    const load = async () => {
      try {
        const [ep, st] = await Promise.all([
          fetch('/api/episodes').then((r) => r.json()),
          fetch('/api/stats').then((r) => r.json()),
        ])
        if (alive) { setEpisodes(ep); setStats(st) }
      } catch { /* poll again next tick */ }
    }
    fetch('/api/topology').then((r) => r.json()).then((t) => alive && setTopology(t))
    load()
    const t = setInterval(load, 3000)
    return () => { alive = false; clearInterval(t) }
  }, [])

  return { connected, feed, flashes, episodes, stats, topology }
}
