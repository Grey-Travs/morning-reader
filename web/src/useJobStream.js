import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from './api'

// Follow one project's job over Server-Sent Events.
//
// Three things this has to get right, all of them learned from the shape of the
// server's stream:
//
//  * It reattaches. The job id is discovered from /active-job rather than remembered,
//    so a reload, a second tab, or navigating away and back all land on the running
//    job instead of showing an empty console.
//  * It replays. The server sends its whole history on connect, so events that
//    happened between pressing Run and the stream opening are not lost. That means
//    this hook must be able to receive the same event twice and stay correct, which
//    is why it rebuilds state from scratch on (re)connect rather than appending.
//  * It closes. The server only releases a subscriber queue when the connection goes
//    away, so failing to close on unmount leaks one per navigation.

const TERMINAL = new Set(['done', 'paused'])

export function useJobStream(pid) {
  const [events, setEvents] = useState([])
  const [queue, setQueue] = useState({ current: null, pending: [], kind: '', waiting: null })
  const [live, setLive] = useState(null)
  const [running, setRunning] = useState(false)
  const sourceRef = useRef(null)
  const [reconnect, setReconnect] = useState(0)

  // Bumped by callers after starting work, so the hook looks for the new job at once
  // rather than waiting for the next poll.
  const refresh = useCallback(() => setReconnect((n) => n + 1), [])

  useEffect(() => {
    if (!pid) return undefined
    let alive = true
    let timer = null

    const close = () => {
      if (sourceRef.current) {
        sourceRef.current.close()
        sourceRef.current = null
      }
    }

    const attach = async () => {
      if (!alive || sourceRef.current) return
      let jobId = null
      try {
        jobId = (await api.activeJob(pid))?.job_id || null
      } catch {
        // The server may simply not be up yet; the poll below tries again.
      }
      if (!alive) return
      if (!jobId) {
        setRunning(false)
        return
      }

      setRunning(true)
      // Rebuild from the replay rather than appending to what is already here: the
      // stream repeats its whole history on connect, so appending would duplicate
      // every event on a reconnect.
      const replayed = []
      const source = new EventSource(api.streamUrl(pid, jobId))
      sourceRef.current = source

      source.onmessage = (message) => {
        if (!alive) return
        let event
        try {
          event = JSON.parse(message.data)
        } catch {
          return // a keep-alive comment, or a frame we do not understand
        }

        if (event.type === 'live') {
          setLive({ index: event.index, title: event.title, done: event.done, total: event.total })
        } else if (event.type === 'progress') {
          setLive((previous) => ({ ...(previous || {}), index: event.index, done: event.done, total: event.total }))
        } else if (event.type === 'start') {
          setLive({ index: event.index, title: event.title, done: 0, total: event.units || 0 })
        } else {
          if (event.type === 'item') setLive(null)
          replayed.push(event)
          setEvents([...replayed])
        }

        if ('current' in event) {
          setQueue({
            current: event.current ?? null,
            pending: event.pending || [],
            kind: event.kind || '',
            waiting: event.waiting ?? null,
          })
        }

        if (TERMINAL.has(event.type)) {
          setRunning(false)
          setLive(null)
          close()
        }
      }

      source.onerror = () => {
        // EventSource reconnects by itself, but a job that finished while the tab was
        // hidden leaves it retrying against a stream that will never speak again.
        close()
        setRunning(false)
      }
    }

    attach()
    // Poll for a job started from somewhere else — another tab, or the library page.
    timer = setInterval(() => { if (!sourceRef.current) attach() }, 3000)

    return () => {
      alive = false
      if (timer) clearInterval(timer)
      close()
    }
  }, [pid, reconnect])

  return { events, queue, live, running, refresh }
}
