import { useEffect, useRef } from 'react'
import { STATUS_LABEL } from '../format'

// The live console: what the worker is doing right now, and what it has done.
//
// It mirrors the terminal renderer on the server deliberately — the same events feed
// both — so someone watching either one sees the same run.

function Line({ event }) {
  if (event.type === 'item') {
    const tone = event.status === 'failed' ? 'var(--bad)'
      : event.refused ? 'var(--warn)'
      : event.aborted ? 'var(--hint)'
      : 'var(--ok)'
    const note = event.skipped ? ' (already done)'
      : event.refused ? ' (not applied)'
      : event.aborted ? ' (stopped)'
      : ''
    return (
      <div>
        <span style={{ color: tone }}>
          {event.status === 'failed' ? '✗' : event.aborted || event.skipped ? '·' : '✓'}
        </span>
        {' '}{event.index}. <span className="font-source">{event.title}</span>
        {' — '}
        <span style={{ color: tone }}>
          {STATUS_LABEL[event.status] ?? event.status}{note}
        </span>
        {event.error && <span style={{ color: 'var(--bad)' }}> · {event.error}</span>}
      </div>
    )
  }
  if (event.type === 'queued') {
    return <div className="text-hint">+ queued {event.added?.join(', ')}</div>
  }
  if (event.type === 'waiting') {
    return (
      <div style={{ color: 'var(--warn)' }}>
        ⏸ waiting out a usage limit — {event.message}
      </div>
    )
  }
  if (event.type === 'resumed') return <div style={{ color: 'var(--ok)' }}>▶ resumed</div>
  if (event.type === 'paused') {
    return <div style={{ color: 'var(--warn)' }}>paused — {event.message}</div>
  }
  if (event.type === 'done') return <div className="text-hint">— finished —</div>
  return null
}

export default function JobConsole({ events, queue, live, running, onResume }) {
  const endRef = useRef(null)

  useEffect(() => {
    // Follow the tail, but only inside the console — scrollIntoView would drag the
    // whole page down every time an item finished.
    const node = endRef.current
    if (!node) return
    const box = node.parentElement
    if (box) box.scrollTop = box.scrollHeight
  }, [events.length, live?.done])

  const hasAnything = events.length > 0 || running
  if (!hasAnything) {
    return (
      <p className="text-sm text-hint">
        Nothing has run yet. Press Prepare and the progress appears here and on Activity.
      </p>
    )
  }

  const total = live?.total || 0
  const done = live?.done || 0
  const pct = total ? Math.round((done / total) * 100) : 0

  return (
    <div>
      {running && (
        <div className="mb-2">
          <div className="mb-1 flex flex-wrap items-baseline gap-2 text-sm">
            <span className="inline-block h-2 w-2 shrink-0 rounded-full"
                  style={{ background: 'var(--accent)' }} />
            {queue.waiting ? (
              <span style={{ color: 'var(--warn)' }}>
                Waiting for the usage window to refresh
                {queue.waiting.resume_at && (
                  <> · retries ~{new Date(queue.waiting.resume_at * 1000)
                    .toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</>
                )}
              </span>
            ) : live ? (
              <span>
                Chapter {live.index}
                {live.title && <span className="font-source"> · {live.title}</span>}
              </span>
            ) : (
              <span>Starting…</span>
            )}
            <span className="ml-auto text-xs text-hint">
              {queue.pending?.length ? `${queue.pending.length} waiting` : ''}
            </span>
            {queue.waiting && onResume && (
              <button type="button" className="btn" onClick={onResume}>Try now</button>
            )}
          </div>
          {total > 0 && (
            <div className="bar" role="progressbar" aria-valuenow={pct}
                 aria-valuemin={0} aria-valuemax={100}>
              <span style={{ width: `${pct}%` }} />
            </div>
          )}
        </div>
      )}

      <div className="console">
        {events.map((event, i) => <Line key={i} event={event} />)}
        <div ref={endRef} />
      </div>
    </div>
  )
}
