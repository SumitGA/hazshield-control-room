// The control room's nervous system. Live feed polls /api/violations/recent
// (SSE is buffered by Cloudflare, so we poll — reliable through the CDN).
// Slow polls for the durable truth (episodes, stats, topology).
import { useEffect, useRef, useState } from 'react'

export function useLive() {
  const [connected, setConnected] = useState(true)
  const [feed, setFeed] = useState([])            // capped ticker
  const [flashes, setFlashes] = useState({})      // zone_id -> ts of last hit
  const [episodes, setEpisodes] = useState([])
  const [stats, setStats] = useState(null)
  const [topology, setTopology] = useState(null)
  const seenIds = useRef(new Set())               // de-dupe by stream id

  // ---- live feed: poll recent violations ----
  useEffect(() => {
    let alive = true
    const poll = async () => {
      try {
        const rows = await fetch('/api/violations/recent').then((r) => r.json())
        if (!alive) return
        setConnected(true)
        // rows are newest-first; find ones we haven't shown yet
        const fresh = []
        for (const v of rows) {
          if (!seenIds.current.has(v._id)) {
            seenIds.current.add(v._id)
            fresh.push(v)
          }
        }
        if (fresh.length) {
          setFeed((f) => [...fresh, ...f].slice(0, 50))
          setFlashes((z) => {
            const next = { ...z }
            for (const v of fresh) if (v.zone_id) next[v.zone_id] = Date.now()
            return next
          })
          // cap the seen-set so it doesn't grow forever
          if (seenIds.current.size > 500) {
            seenIds.current = new Set([...seenIds.current].slice(-200))
          }
        }
      } catch { if (alive) setConnected(false) }
    }
    poll()
    const t = setInterval(poll, 1500)
    return () => { alive = false; clearInterval(t) }
  }, [])

  // ---- durable truth: episodes, stats, topology ----
  useEffect(() => {
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
