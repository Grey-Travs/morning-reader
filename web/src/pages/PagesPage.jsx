import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api } from '../api'
import Explained from '../components/Explained'
import JobConsole from '../components/JobConsole'
import { JOIN_LABEL, PAGE_STATUS_LABEL, PAGE_STATUS_TONE, countLabel } from '../format'
import { useJobStream } from '../useJobStream'

// Scanned pages: upload, read, check, fix the seams, build.
//
// The screen is ordered the way the work is: you cannot build until pages are read,
// and you should not build until the ones the reader was unsure about have been
// looked at. So the build button says what is missing rather than failing when
// pressed.

// The seam choices, in the order a person is most likely to want them.
const JOINS = ['sentence', 'paragraph', 'chapter', 'gap']

function PageCard({ pid, page, selected, onToggle, onJoin, onStatus, onMove, busy,
                   first, last }) {
  const tone = PAGE_STATUS_TONE[page.status] || 'var(--hint)'
  return (
    <div className="card overflow-hidden">
      <label className="block cursor-pointer">
        <img src={api.pageImageUrl(pid, page.id)} alt={page.name || `Page ${page.seq}`}
             loading="lazy"
             style={{ width: '100%', aspectRatio: '3 / 4', objectFit: 'cover',
                      display: 'block', background: 'var(--surface-2)',
                      outline: selected ? '3px solid var(--accent)' : 'none',
                      outlineOffset: '-3px' }}
             onClick={(e) => { e.preventDefault(); onToggle(page.id) }} />
      </label>

      <div className="p-3">
        <div className="flex items-baseline justify-between gap-2">
          <span className="truncate text-sm">{page.seq}. {page.name || 'Page'}</span>
          <span className="shrink-0 text-xs" style={{ color: tone }}>
            {PAGE_STATUS_LABEL[page.status] ?? page.status}
          </span>
        </div>

        <div className="mt-1 text-xs text-hint">
          {page.width && page.height
            ? `${page.width}×${page.height}`
            : <span style={{ color: 'var(--warn)' }}>size unknown</span>}
          {page.regions > 0 && ` · ${countLabel(page.regions, 'region')}`}
          {page.chars > 0 && ` · ${page.chars} chars`}
          {page.corrections > 0 && ` · ${countLabel(page.corrections, 'correction')}`}
        </div>

        {page.looks_reversed && page.order_source !== 'user' && (
          <p className="mt-1 text-xs" style={{ color: 'var(--warn)' }}>
            reads left-to-right — probably backwards
          </p>
        )}

        {page.error && (
          <p className="mt-1 text-xs" style={{ color: 'var(--bad)' }}>{page.error}</p>
        )}

        {/* By POSITION in the book, not by seq. Seq is upload order: after a reorder
            the first card offered a seam to nothing, and the page with seq 1 — now
            somewhere in the middle — offered none. */}
        {!first && (
          <label className="mt-2 block">
            <span className="mb-1 block text-xs text-muted">
              Follows the page before it as
              {page.join_prev_source === 'user' && (
                <span className="ml-1 pill" style={{ color: 'var(--accent)' }}>
                  your choice
                </span>
              )}
            </span>
            <select className="field" value={page.join_prev || ''} disabled={busy}
                    onChange={(e) => onJoin(page.id, e.target.value)}>
              <option value="" disabled>not decided</option>
              {JOINS.map((value) => (
                <option key={value} value={value}>{JOIN_LABEL[value]}</option>
              ))}
            </select>
            {page.join_reason && page.join_prev_source !== 'user' && (
              <span className="mt-1 block text-xs text-hint">{page.join_reason}</span>
            )}
          </label>
        )}

        <div className="mt-2 flex flex-wrap gap-1">
          <button type="button" className="btn" disabled={busy || first}
                  aria-label={`Move page ${page.seq} earlier`}
                  onClick={() => onMove(page.id, -1)}>←</button>
          <button type="button" className="btn" disabled={busy || last}
                  aria-label={`Move page ${page.seq} later`}
                  onClick={() => onMove(page.id, 1)}>→</button>
          {page.read && (
            <Link to={`/work/${pid}/pages/${page.id}`}
                  className={`btn no-underline${page.status === 'needs-check'
                    ? ' btn-primary' : ''}`}>
              {page.status === 'needs-check' ? 'Check it' : 'Open'}
            </Link>
          )}
          {/* A failed RE-read keeps the good reading it had before. Offered only on
              "to check" pages, that page was stranded: out of the build, skipped by
              the sweep because it has a read, and with nothing to press. */}
          {(page.status === 'needs-check' || (page.status === 'failed' && page.read)) && (
            <button type="button" className="btn" disabled={busy}
                    onClick={() => onStatus(page.id, 'edited')}>
              Looks right
            </button>
          )}
          {page.status !== 'skipped' ? (
            <button type="button" className="btn" disabled={busy}
                    title="Leave this page out of the text"
                    onClick={() => onStatus(page.id, 'skipped')}>
              Not text
            </button>
          ) : (
            <button type="button" className="btn" disabled={busy}
                    onClick={() => onStatus(page.id, 'new')}>
              Put it back
            </button>
          )}
        </div>
      </div>
    </div>
  )
}

export default function PagesPage() {
  const { pid } = useParams()
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [selected, setSelected] = useState(() => new Set())
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState('')
  const fileRef = useRef(null)
  const { events, queue, live, running, refresh } = useJobStream(pid)

  const load = useCallback(async () => {
    try {
      setData(await api.pages(pid))
      setError(null)
    } catch (err) {
      setError(err)
    }
  }, [pid])

  useEffect(() => { load() }, [load])

  // Reload as pages finish, so the grid fills in rather than sitting still until the
  // whole run ends.
  const finished = events.filter((e) => e.type === 'item').length
  useEffect(() => { if (finished) load() }, [finished, load])
  useEffect(() => { if (!running) load() }, [running, load])

  const act = async (fn, message = '') => {
    setBusy(true)
    setNotice('')
    try {
      const result = await fn()
      if (message) setNotice(typeof message === 'function' ? message(result) : message)
      await load()
      return result
    } catch (err) {
      setError(err)
    } finally {
      setBusy(false)
    }
  }

  const upload = async (event) => {
    const files = [...(event.target.files || [])]
    if (!files.length) return
    await act(async () => {
      const result = await api.uploadPages(pid, files)
      const parts = [`${countLabel(result.added.length, 'page')} added`]
      if (result.duplicates.length) parts.push(`${result.duplicates.length} already here`)
      if (result.rejected.length) {
        parts.push(`${result.rejected.length} could not be used — `
          + result.rejected.map((r) => r.reason).join(' '))
      }
      setNotice(parts.join(' · '))
      return result
    })
    if (fileRef.current) fileRef.current.value = ''
  }

  // Reordering sends the WHOLE new order, never a delta: the server refuses a list
  // that is not a permutation, which is what stops a partial list from silently
  // dropping pages out of the book.
  const move = (pageId, delta) => {
    const ids = (data?.pages || []).map((p) => p.id)
    const from = ids.indexOf(pageId)
    const to = from + delta
    if (from < 0 || to < 0 || to >= ids.length) return
    ids.splice(to, 0, ...ids.splice(from, 1))
    return act(() => api.reorderPages(pid, ids))
  }

  const read = async () => {
    const ids = [...selected]
    // By `read`, not by region count — the server's sweep keys on `read`. A page that
    // came back with no text is fully read and fully billed; counting it as unread
    // offered it forever and the press did nothing.
    const count = ids.length || (data?.pages || []).filter(
      (p) => !p.read && p.status !== 'skipped').length
    if (!count) {
      setNotice('Every page has been read already. Select some to read them again.')
      return
    }
    // A new reading replaces a human's corrections, as it does in Night Reader. Said
    // BEFORE anything is spent, rather than discovered after.
    const corrected = (data?.pages || []).filter(
      (p) => selected.has(p.id) && p.corrections > 0).length
    const ok = window.confirm(
      `Read ${countLabel(count, 'page')}?\n\n`
      + 'This uses your Claude plan. You can stop it at any time, and a page that has '
      + 'already been read is never read again unless you select it.'
      + (corrected
        ? `\n\nYour corrections on ${countLabel(corrected, 'page')} will be replaced by `
          + 'the new reading.'
        : ''))
    if (!ok) return
    const result = await act(
      () => api.readPages(pid, ids.length ? { ids, force: true } : {}))
    // The server may legitimately queue nothing. Saying so beats a button that
    // appears to do nothing, which invites paying to "retry" work already done.
    if (result && !result.job_id) {
      setNotice('Those pages have all been read already.')
    }
    refresh()
  }

  if (error && !data) {
    return (
      <div className="page page-narrow">
        <Explained error={error} onRetry={load} />
      </div>
    )
  }
  if (!data) return <div className="page text-sm text-hint">Loading…</div>

  const { pages, summary } = data
  // A manga's chapters are runs of pages and its unchecked pages are still part of the
  // book, so two things below read differently. The kind travels with this payload
  // precisely so the screen can branch.
  const isManga = data.kind === 'manga'
  const needChecking = (summary.by_status || {})['needs-check'] || 0
  const firstToCheck = pages.find((p) => p.status === 'needs-check')
  const unread = pages.filter((p) => !p.read && p.status !== 'skipped').length
  // A novel builds from approved pages only; a manga builds from every page that is
  // not explicitly "not text", because its art is the content.
  const canBuild = summary.ready > 0 || (isManga && summary.total > 0)

  return (
    <div className="page">
      <div className="mb-5">
        <p className="text-xs text-hint">
          <Link to={`/work/${pid}`} style={{ color: 'inherit' }}>Back to the work</Link>
        </p>
        <h1 className="mt-1 text-2xl font-medium tracking-tight">Pages</h1>
        <p className="mt-1 text-sm text-hint">
          {countLabel(summary.total, 'page')}
          {summary.ready > 0 && ` · ${summary.ready} ready`}
          {needChecking > 0 && ` · ${needChecking} to check`}
          {summary.unmeasured > 0 && ` · ${summary.unmeasured} with unknown size`}
          {summary.totals?.cost_usd ? ` · $${summary.totals.cost_usd.toFixed(4)}` : ''}
        </p>
      </div>

      {error && (
        <div className="mb-5">
          <Explained error={error} onDismiss={() => setError(null)} />
        </div>
      )}

      {notice && (
        <div className="card mb-5 p-3 text-sm text-muted">{notice}</div>
      )}

      <div className="mb-5 flex flex-wrap items-center gap-2">
        <label className="btn btn-primary" style={{ cursor: 'pointer' }}>
          Add pages
          <input ref={fileRef} type="file" multiple hidden accept="image/jpeg,image/png,image/webp"
                 onChange={upload} />
        </label>
        <button type="button" className="btn" onClick={read} disabled={busy}>
          {selected.size
            ? `Read ${countLabel(selected.size, 'page')} again`
            : `Read ${unread ? countLabel(unread, 'page') : 'pages'}`}
        </button>
        <button type="button" className="btn" disabled={busy || !summary.ready}
                title="Work out how each page follows the one before it"
                onClick={() => act(() => api.proposeJoins(pid),
                  (r) => `${countLabel(r.proposed, 'seam')} worked out. Yours are left alone.`)}>
          Work out the seams
        </button>
        {selected.size > 0 && (
          <>
            <button type="button" className="btn" onClick={() => setSelected(new Set())}>
              Clear selection
            </button>
            <button type="button" className="btn" disabled={busy}
                    onClick={() => {
                      if (window.confirm(`Delete ${countLabel(selected.size, 'page')}?`)) {
                        act(() => api.deletePages(pid, [...selected]))
                        setSelected(new Set())
                      }
                    }}>
              Delete
            </button>
          </>
        )}
        <span className="flex-1" />
        {running && (
          <button type="button" className="btn"
                  onClick={() => api.cancel(pid, true).then(refresh)}>
            Stop
          </button>
        )}
        {isManga && summary.chapters > 0 && (
          <Link to={`/work/${pid}/manga/1`} className="btn no-underline">
            Open the reader
          </Link>
        )}
        <button type="button" className="btn btn-primary" disabled={busy || !canBuild}
                title={canBuild ? 'Turn the read pages into chapters'
                                : 'Read some pages first'}
                onClick={() => act(() => api.buildFromPages(pid), (r) => {
                  const base = `Built ${countLabel(r.chapters, 'chapter')} from `
                    + `${countLabel(r.pages_used, 'page')}.`
                  return r.warnings?.length ? `${base} ${r.warnings.join(' ')}` : base
                })}>
          Build the chapters
        </button>
      </div>

      {needChecking > 0 && (
        <div className="card mb-5 p-4" style={{ borderColor: 'var(--warn)' }}>
          <p className="text-sm" style={{ color: 'var(--warn)' }}>
            {countLabel(needChecking, 'page')} the reader was unsure about.
          </p>
          <p className="mt-1 text-sm text-muted">
            {isManga
              ? 'They are still part of the book — the art on an unchecked page is '
                + 'correct, and leaving it out would put a hole in the middle of a '
                + 'scene. Their lines are labelled in the reader instead.'
              : 'They are left out of the build until you look — putting un-checked '
                + 'transcription into the novel is the same mistake as reading a '
                + 'translation nobody accepted.'}
          </p>
          {firstToCheck && (
            <Link to={`/work/${pid}/pages/${firstToCheck.id}`}
                  className="btn btn-primary mt-3 no-underline">
              Check the first one
            </Link>
          )}
        </div>
      )}

      <JobConsole events={events} queue={queue} live={live} running={running}
                  onResume={() => api.resume(pid).then(refresh)} />

      {pages.length === 0 ? (
        <div className="card mt-5 p-10 text-center text-muted"
             style={{ borderStyle: 'dashed', borderColor: 'var(--line-strong)' }}>
          <p>No pages yet. Add photographs or scans — JPEG, PNG or WebP.</p>
          <p className="mt-1 text-xs text-hint">
            iPhone HEIC photos cannot be displayed by browsers; set Camera → Formats →
            Most Compatible and re-export.
          </p>
        </div>
      ) : (
        <div className="mt-5 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {pages.map((page, i) => (
            <PageCard
              key={page.id}
              pid={pid}
              page={page}
              busy={busy}
              first={i === 0}
              last={i === pages.length - 1}
              onMove={move}
              selected={selected.has(page.id)}
              onToggle={(id) => setSelected((prev) => {
                const next = new Set(prev)
                if (next.has(id)) next.delete(id)
                else next.add(id)
                return next
              })}
              onJoin={(id, kind) => act(() => api.setPageJoin(pid, id, kind))}
              onStatus={(id, status) => act(() => api.setPageStatus(pid, id, status))}
            />
          ))}
        </div>
      )}
    </div>
  )
}
