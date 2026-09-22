import { useCallback, useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api } from '../api'
import Explained from '../components/Explained'
import JobConsole from '../components/JobConsole'
import { CLASS_LABEL, STATUS_LABEL, STATUS_TONE, countLabel, percent } from '../format'
import { useJobStream } from '../useJobStream'

export default function ProjectPage() {
  const { pid } = useParams()
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [selected, setSelected] = useState(() => new Set())
  const { events, queue, live, running, refresh } = useJobStream(pid)

  const load = useCallback(async () => {
    try {
      setData(await api.getProject(pid))
      setError(null)
    } catch (err) {
      setError(err)
    }
  }, [pid])

  useEffect(() => { load() }, [load])

  // Reload the rows whenever an item finishes: the stream says WHAT happened, the
  // API says what the chapter now looks like, and only the second one knows about
  // staleness and counts.
  const finishedCount = events.filter((e) => e.type === 'item').length
  useEffect(() => { if (finishedCount) load() }, [finishedCount, load])
  useEffect(() => { if (!running) load() }, [running, load])

  const start = async (body) => {
    try {
      await api.run(pid, body)
      refresh()
    } catch (err) {
      setError(err)
    }
  }

  const stop = async (stopCurrent) => {
    try {
      await api.cancel(pid, stopCurrent)
      refresh()
    } catch (err) {
      setError(err)
    }
  }

  if (error && !data) {
    return (
      <div className="page page-narrow">
        <Explained error={error} onRetry={load} />
        <p className="mt-4 text-sm"><Link to="/" style={{ color: 'var(--accent)' }}>Back to the library</Link></p>
      </div>
    )
  }
  if (!data) return <div className="page text-sm text-hint">Loading…</div>

  const { project, chapters, totals } = data
  const queued = new Set([...(queue.pending || []), queue.current].filter((v) => v != null))
  const anySelected = selected.size > 0
  const staleCount = chapters.filter((c) => c.stale).length

  const toggle = (index) => setSelected((previous) => {
    const next = new Set(previous)
    if (next.has(index)) next.delete(index)
    else next.add(index)
    return next
  })

  return (
    <div className="page">
      <div className="mb-5">
        <p className="text-xs text-hint">
          <Link to="/" style={{ color: 'inherit' }}>Library</Link> · {project.kind}
        </p>
        <h1 className="mt-1 text-2xl font-medium tracking-tight">{project.title}</h1>
        <p className="mt-1 text-sm text-hint">
          {countLabel(chapters.length, 'chapter')}
          {staleCount > 0 && ` · ${staleCount} changed since it was last looked at`}
          {totals?.cost_usd ? ` · $${totals.cost_usd.toFixed(4)}` : ''}
        </p>
      </div>

      {error && (
        <div className="mb-5">
          <Explained error={error} onRetry={load} onDismiss={() => setError(null)} />
        </div>
      )}

      <div className="mb-5 flex flex-wrap items-center gap-2">
        <button type="button" className="btn btn-primary"
                onClick={() => start(anySelected ? { indices: [...selected] } : {})}>
          {anySelected ? `Prepare ${countLabel(selected.size, 'chapter')}` : 'Prepare everything'}
        </button>
        <button type="button" className="btn"
                onClick={() => start(anySelected ? { indices: [...selected], force: true } : { force: true })}>
          Redo {anySelected ? 'selected' : 'everything'}
        </button>
        {anySelected && (
          <button type="button" className="btn" onClick={() => setSelected(new Set())}>
            Clear selection
          </button>
        )}
        <span className="flex-1" />
        {running && (
          <>
            <button type="button" className="btn" onClick={() => stop(false)}>
              Clear the queue
            </button>
            <button type="button" className="btn" onClick={() => stop(true)}>
              Stop now
            </button>
          </>
        )}
      </div>

      <JobConsole events={events} queue={queue} live={live} running={running}
                  onResume={() => api.resume(pid).then(refresh)} />

      <div className="card mt-5 overflow-hidden">
        <table className="w-full text-sm">
          <thead>
            <tr style={{ borderBottom: '1px solid var(--line)' }}>
              <th className="w-8 p-2" />
              <th className="p-2 text-left font-normal text-hint">#</th>
              <th className="p-2 text-left font-normal text-hint">Title</th>
              <th className="p-2 text-left font-normal text-hint">What</th>
              <th className="p-2 text-right font-normal text-hint">Paragraphs</th>
              <th className="p-2 text-left font-normal text-hint">Status</th>
            </tr>
          </thead>
          <tbody>
            {chapters.map((chapter) => (
              <tr key={chapter.index} style={{ borderBottom: '1px solid var(--line)' }}>
                <td className="p-2">
                  <input type="checkbox" checked={selected.has(chapter.index)}
                         aria-label={`Select chapter ${chapter.index}`}
                         onChange={() => toggle(chapter.index)} />
                </td>
                <td className="p-2 text-hint">{chapter.index}</td>
                <td className="max-w-0 truncate p-2 font-source">{chapter.title}</td>
                <td className="p-2">
                  <span className="pill text-hint">
                    {CLASS_LABEL[chapter.class] || chapter.class}
                  </span>
                </td>
                <td className="p-2 text-right text-hint">{chapter.paragraph_count}</td>
                <td className="p-2">
                  <span style={{ color: STATUS_TONE[chapter.status] || 'var(--hint)' }}>
                    {queued.has(chapter.index) && running
                      ? (queue.current === chapter.index ? 'Working…' : 'Queued')
                      : STATUS_LABEL[chapter.status] ?? chapter.status}
                  </span>
                  {chapter.stale && (
                    <span className="ml-2 pill" style={{ color: 'var(--warn)' }}>changed</span>
                  )}
                  {chapter.error && (
                    <span className="ml-2 text-xs" style={{ color: 'var(--bad)' }}>
                      {chapter.error}
                    </span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {live?.total ? (
        <p className="mt-2 text-xs text-hint">
          {percent(live.done, live.total)}% of chapter {live.index}
        </p>
      ) : null}
    </div>
  )
}
