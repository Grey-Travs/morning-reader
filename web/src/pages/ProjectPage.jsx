import { useCallback, useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api } from '../api'
import Explained from '../components/Explained'
import JobConsole from '../components/JobConsole'
import {
  CLASS_LABEL, MANGA_CHAPTER_STATUS_LABEL, MANGA_CHAPTER_STATUS_TONE,
  STATUS_LABEL, STATUS_TONE, countLabel, percent,
} from '../format'
import { useJobStream } from '../useJobStream'

// Statuses whose chapter has English worth opening. `needs-review` is included on
// purpose: that prose exists and was paid for, and the reader is explicit that it has
// not been accepted.
const READABLE = new Set(['translated', 'validated', 'needs-review'])

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

  // Translating spends the Claude plan's allowance, and a sweep over a long novel
  // spends a lot of it. Confirm first — and say HOW MUCH work is about to start,
  // because "are you sure?" with no number is a dialog people learn to click through.
  const translate = async (body) => {
    const count = body.indices
      ? body.indices.length
      : chapters.filter((c) => c.class === 'source'
          && !['validated', 'translated'].includes(c.status)).length
    if (count === 0) {
      setError({ explained: {
        code: 'nothing-to-do',
        title: 'There is nothing left to translate',
        what: 'Every chapter that needs translating has been done already.',
        fixes: ['Select specific chapters to redo them.'],
      } })
      return
    }
    const ok = window.confirm(
      `Translate ${countLabel(count, 'chapter')}?\n\n`
      + 'This uses your Claude plan. You can stop it at any time, and anything '
      + 'already finished is never redone.')
    if (!ok) return
    await start({ ...body, kind: 'translate' })
  }

  // A manga chapter is one call for the whole script, so the warning says what that
  // buys rather than just what it costs.
  const translateManga = async () => {
    const count = selected.size || chapters.length
    if (count === 0) return
    const ok = window.confirm(
      `Translate ${countLabel(count, 'chapter')}?

`
      + 'This uses your Claude plan. Each chapter goes in one call, so every bubble '
      + 'is translated knowing what was said on the pages around it.')
    if (!ok) return
    try {
      await api.translateManga(pid, selected.size ? { indices: [...selected] } : {})
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
  // A manga forks below the region contract: its chapters are runs of pages, its
  // English is per region, and the server refuses the prose routes for it by name.
  // The UI has to fork with it, or it offers buttons that 400.
  const isManga = project.kind === 'manga'
  const queued = new Set([...(queue.pending || []), queue.current].filter((v) => v != null))
  const anySelected = selected.size > 0
  // For a manga `stale` is a COUNT of out-of-date lines, not a boolean.
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
          {' · '}
          <Link to={`/work/${pid}/glossary`} style={{ color: 'var(--accent)' }}>
            Glossary
          </Link>
          {project.ingest === 'images' && (
            <>
              {' · '}
              <Link to={`/work/${pid}/pages`} style={{ color: 'var(--accent)' }}>
                Pages
              </Link>
            </>
          )}
        </p>
      </div>

      {error && (
        <div className="mb-5">
          <Explained error={error} onRetry={load} onDismiss={() => setError(null)} />
        </div>
      )}

      <div className="mb-5 flex flex-wrap items-center gap-2">
        {isManga ? (
          <button type="button" className="btn btn-primary" onClick={translateManga}>
            {anySelected
              ? `Translate ${countLabel(selected.size, 'chapter')}`
              : 'Translate every chapter'}
          </button>
        ) : (
          <>
            <button type="button" className="btn btn-primary"
                    onClick={() => translate(anySelected ? { indices: [...selected] } : {})}>
              {anySelected
                ? `Translate ${countLabel(selected.size, 'chapter')}`
                : 'Translate everything'}
            </button>
            <button type="button" className="btn"
                    onClick={() => start(anySelected ? { indices: [...selected] } : {})}>
              {anySelected ? 'Prepare selected' : 'Prepare everything'}
            </button>
            <button type="button" className="btn"
                    onClick={() => start(anySelected ? { indices: [...selected], force: true } : { force: true })}>
              Re-prepare {anySelected ? 'selected' : 'everything'}
            </button>
          </>
        )}
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

      {chapters.length === 0 && project.ingest === 'images' && (
        <div className="card mt-5 p-8 text-center text-muted">
          <p>This work is built from photographs, and none have been read yet.</p>
          <p className="mt-2">
            <Link to={`/work/${pid}/pages`} className="btn btn-primary no-underline">
              Go to the pages
            </Link>
          </p>
        </div>
      )}

      <div className="card mt-5 overflow-hidden">
        <table className="w-full text-sm">
          <thead>
            <tr style={{ borderBottom: '1px solid var(--line)' }}>
              <th className="w-8 p-2" />
              <th className="p-2 text-left font-normal text-hint">#</th>
              <th className="p-2 text-left font-normal text-hint">Title</th>
              <th className="p-2 text-left font-normal text-hint">
                {isManga ? 'Pages' : 'What'}
              </th>
              <th className="p-2 text-right font-normal text-hint">
                {isManga ? 'Lines' : 'Paragraphs'}
              </th>
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
                <td className="max-w-0 truncate p-2 font-source">
                  {isManga ? (
                    <Link to={`/work/${pid}/manga/${chapter.index}`}
                          style={{ color: 'inherit' }}>{chapter.title}</Link>
                  ) : READABLE.has(chapter.status) ? (
                    <Link to={`/work/${pid}/read/${chapter.index}`}
                          style={{ color: 'inherit' }}>{chapter.title}</Link>
                  ) : chapter.title}
                </td>
                <td className="p-2">
                  {isManga ? (
                    <span className="text-hint">
                      {chapter.pages}
                      {chapter.silent_pages > 0
                        && ` (${chapter.silent_pages} with no text)`}
                    </span>
                  ) : (
                    <span className="pill text-hint">
                      {CLASS_LABEL[chapter.class] || chapter.class}
                    </span>
                  )}
                </td>
                <td className="p-2 text-right text-hint">
                  {isManga
                    ? `${chapter.translated} / ${chapter.lines}`
                    : chapter.paragraph_count}
                </td>
                <td className="p-2">
                  <span style={{
                    color: (isManga ? MANGA_CHAPTER_STATUS_TONE : STATUS_TONE)[
                      chapter.status] || 'var(--hint)',
                  }}>
                    {queued.has(chapter.index) && running
                      ? (queue.current === chapter.index ? 'Working…' : 'Queued')
                      : (isManga ? MANGA_CHAPTER_STATUS_LABEL : STATUS_LABEL)[
                          chapter.status] ?? chapter.status}
                  </span>
                  {isManga && chapter.stale > 0 && (
                    <span className="ml-2 pill" style={{ color: 'var(--warn)' }}>
                      {chapter.stale} out of date
                    </span>
                  )}
                  {isManga && chapter.unchecked_pages > 0 && (
                    <span className="ml-2 pill" style={{ color: 'var(--warn)' }}>
                      {chapter.unchecked_pages} unchecked
                    </span>
                  )}
                  {!isManga && chapter.stale && (
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
