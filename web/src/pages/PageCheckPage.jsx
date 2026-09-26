import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api } from '../api'
import Explained from '../components/Explained'
import { PAGE_STATUS_LABEL, PAGE_STATUS_TONE, countLabel } from '../format'

// One page, checked against its photograph — Night Reader's page editor, carried.
//
// The photo on one side, what the reader read on the other, and a correction typed
// straight into it. Two things differ from Night Reader, both on purpose:
//
//  * The text is shown as the page's REGIONS in reading order, one box each, rather
//    than one block. The region contract is fixed for novels as well as manga, and a
//    correction to one region is what keeps a manga bubble's English tied to its words.
//  * Nothing can be typed while the page is being read. Night Reader allowed it, and
//    the read then landed on top and silently replaced the correction.

// What each kind of region is, in the reader's words.
const KIND_LABEL = {
  body: 'text', heading: 'heading', caption: 'caption', note: 'note',
  furigana: 'reading aid', 'page-number': 'page number', 'running-head': 'running head',
  watermark: 'site stamp', bubble: 'speech', thought: 'thought', narration: 'narration',
  sfx: 'sound effect', sign: 'sign', aside: 'aside',
}

// Printed on the page but not part of the text: never built into a novel, never
// translated on a manga page. Shown so nothing on the photo is unaccounted for.
const NOT_TEXT = new Set(['page-number', 'running-head', 'watermark', 'furigana'])

// How long typing has to pause before a correction is saved. Night Reader's figure.
const SAVE_AFTER_MS = 700

function RegionEditor({ index, region, draft, disabled, onChange, onRevert }) {
  const muted = NOT_TEXT.has(region.kind)
  return (
    <div className="mb-3">
      <div className="mb-1 flex items-center gap-2 text-xs text-hint">
        <span>{index + 1} · {KIND_LABEL[region.kind] || region.kind}</span>
        {muted && <span className="pill">not part of the text</span>}
        {region.corrected && (
          <>
            <span className="pill" style={{ color: 'var(--accent)' }}>your correction</span>
            <button type="button" className="btn" disabled={disabled}
                    title={`The reader read: ${region.original}`}
                    onClick={() => onRevert(region)}>
              Use the reader&rsquo;s version
            </button>
          </>
        )}
      </div>
      <textarea className="field font-source" value={draft} disabled={disabled}
                aria-label={`Region ${index + 1}`}
                rows={Math.min(8, Math.max(2, Math.ceil((draft || '').length / 40)))}
                style={muted ? { color: 'var(--hint)' } : undefined}
                onChange={(e) => onChange(region.id, e.target.value)} />
    </div>
  )
}

export default function PageCheckPage() {
  const { pid, pageId } = useParams()
  const [data, setData] = useState(null)
  const [drafts, setDrafts] = useState({})
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)
  const [hint, setHint] = useState('')
  const [zoom, setZoom] = useState(1)
  const [notice, setNotice] = useState('')

  // Corrections waiting on the pause, keyed by region, each remembering WHICH page it
  // belongs to — so one typed just before moving to the next page is saved against the
  // page it was typed on, not flushed onto the new one.
  const pending = useRef({})
  const timers = useRef({})
  // The page on screen NOW. A load started on page A can land after the arrows moved
  // to page B — through "Looks right" and a quick arrow, say — and it used to paint
  // A's words under B's address, where typing saved A's text onto B and approved it.
  const showing = useRef(pageId)
  showing.current = pageId

  const load = useCallback(async () => {
    try {
      const body = await api.page(pid, pageId)
      if (showing.current !== pageId) return
      setData(body)
      setDrafts(Object.fromEntries(body.regions.map((r) => [r.id, r.text])))
      setHint(body.page.hint || '')
      setError(null)
    } catch (err) {
      if (showing.current === pageId) setError(err)
    }
  }, [pid, pageId])

  useEffect(() => {
    setData(null)
    setZoom(1)
    setNotice('')
    load()
  }, [load])

  const save = useCallback(async (forPage, regionId, text) => {
    try {
      const result = await api.correctRegion(pid, forPage, regionId, text)
      // Only onto the page still on screen: a save landing after the reader moved on
      // must not put page A's region beside page B's photograph. Checked against the
      // data being updated, not this closure's page id — a request sent from page A
      // resolves inside page A's closure even when page B is showing.
      setData((current) => (current && current.page.id === forPage ? {
        ...current,
        note: result.note,
        page: { ...current.page, status: result.status },
        regions: current.regions.map((r) => (r.id === regionId
          ? { ...r, text: result.region.text, original: result.region.original,
              corrected: result.region.corrected }
          : r)),
        corrected_at: new Date().toISOString(),
      } : current))
    } catch (err) {
      setError(err)
    }
  }, [pid, pageId])

  // FLUSH what is waiting, never drop it: Night Reader learned that a correction typed
  // inside the pause and then navigated away from was silently lost — no warning, and
  // the typo still there on return.
  const flush = useCallback(async () => {
    const waiting = Object.entries(pending.current)
    pending.current = {}
    Object.values(timers.current).forEach(clearTimeout)
    timers.current = {}
    await Promise.all(waiting.map(([rid, p]) => save(p.page, rid, p.text)))
  }, [save])

  useEffect(() => () => {
    // Leaving this page (or the screen): send what is waiting, against its own page.
    const waiting = Object.entries(pending.current)
    pending.current = {}
    Object.values(timers.current).forEach(clearTimeout)
    timers.current = {}
    for (const [rid, p] of waiting) {
      api.correctRegion(pid, p.page, rid, p.text).catch(() => {})
    }
  }, [pid, pageId])

  const change = (regionId, text) => {
    // Against the page whose words are ON SCREEN, which is what was typed into.
    const forPage = data?.page?.id
    if (!forPage || forPage !== pageId) return
    setDrafts((d) => ({ ...d, [regionId]: text }))
    pending.current[regionId] = { page: forPage, text }
    clearTimeout(timers.current[regionId])
    timers.current[regionId] = setTimeout(() => {
      delete pending.current[regionId]
      delete timers.current[regionId]
      save(forPage, regionId, text)
    }, SAVE_AFTER_MS)
  }

  const revert = (region) => {
    setDrafts((d) => ({ ...d, [region.id]: region.original }))
    clearTimeout(timers.current[region.id])
    delete timers.current[region.id]
    delete pending.current[region.id]
    save(pageId, region.id, region.original)
  }

  const status = data?.page?.status
  // From the server, which asks the worker: a read waiting out a usage limit leaves
  // the page at its old status for hours and still replaces it when it lands.
  // Not from the status alone, either: a page left "reading" by a run that died holds
  // nothing, and judged by status it was locked — and polled — for good.
  const inFlight = Boolean(data?.in_flight)
  const locked = inFlight || status === 'skipped'

  // While the page is being read, look again until it is not. Nothing can be typed in
  // the meantime, so replacing the boxes with the new reading loses nothing. An
  // interval, not a timer re-armed by each answer: one failed look (a restart, a
  // dropped connection) used to stop it for good, leaving everything disabled.
  useEffect(() => {
    if (!inFlight) return undefined
    const timer = setInterval(load, 1500)
    return () => clearInterval(timer)
  }, [inFlight, load])

  const act = async (fn, message = '') => {
    setBusy(true)
    setNotice('')
    try {
      await flush()
      await fn()
      await load()
      if (message) setNotice(message)
    } catch (err) {
      setError(err)
    } finally {
      setBusy(false)
    }
  }

  const readAgain = () => {
    // Including ones still waiting on the pause: they are saved first, and then the
    // new reading replaces them, so they count.
    const corrections = data.regions.filter(
      (r) => r.corrected || (drafts[r.id] ?? r.text) !== r.text).length
    const ok = window.confirm(
      'Read this page again?\n\nThis uses your Claude plan.'
      + (corrections
        ? `\n\nYour ${countLabel(corrections, 'correction')} on this page will be `
          + 'replaced by the new reading.'
        : ''))
    if (!ok) return
    return act(() => api.readPages(pid, { ids: [pageId], force: true, hint }),
      'Reading it again. This page updates when it is done.')
  }

  if (error && !data) {
    return (
      <div className="page page-narrow">
        <Explained error={error} onRetry={load} />
      </div>
    )
  }
  if (!data) return <div className="page text-sm text-hint">Loading…</div>

  const { page, regions, position } = data
  const isManga = data.kind === 'manga'
  const corrected = regions.some((r) => r.corrected)
  const builtBefore = data.built_at && data.corrected_at
    && data.corrected_at > data.built_at
  const tone = PAGE_STATUS_TONE[status] || 'var(--hint)'

  return (
    <div className="page">
      <div className="mb-4 flex flex-wrap items-end justify-between gap-3">
        <div>
          <p className="text-xs text-hint">
            <Link to={`/work/${pid}/pages`} style={{ color: 'inherit' }}>
              Back to the pages
            </Link>
          </p>
          <h1 className="mt-1 text-2xl font-medium tracking-tight">
            Page {page.seq}{page.name ? ` · ${page.name}` : ''}
          </h1>
          <p className="mt-1 text-sm">
            <span style={{ color: tone }}>{PAGE_STATUS_LABEL[status] ?? status}</span>
            {page.ocr?.confidence && (
              <span className="text-hint"> · {page.ocr.confidence} confidence</span>
            )}
          </p>
        </div>
        <div className="flex items-center gap-2 text-sm">
          {position.prev
            ? <Link className="btn no-underline" to={`/work/${pid}/pages/${position.prev}`}
                    aria-label="Previous page">←</Link>
            : <button type="button" className="btn" disabled aria-label="Previous page">
                ←</button>}
          <span className="text-hint">{position.index + 1} / {position.total}</span>
          {position.next
            ? <Link className="btn no-underline" to={`/work/${pid}/pages/${position.next}`}
                    aria-label="Next page">→</Link>
            : <button type="button" className="btn" disabled aria-label="Next page">
                →</button>}
        </div>
      </div>

      {error && (
        <div className="mb-4">
          <Explained error={error} onDismiss={() => setError(null)} />
        </div>
      )}
      {notice && <div className="card mb-4 p-3 text-sm text-muted">{notice}</div>}

      <div className="grid gap-4 lg:grid-cols-2">
        <div className="card p-2">
          <div className="mb-2 flex items-center gap-1 text-xs">
            <button type="button" className="btn" aria-label="Zoom out"
                    onClick={() => setZoom((z) => Math.max(0.5, z - 0.25))}>−</button>
            <button type="button" className="btn" onClick={() => setZoom(1)}
                    title="Back to fit">{Math.round(zoom * 100)}%</button>
            <button type="button" className="btn" aria-label="Zoom in"
                    onClick={() => setZoom((z) => Math.min(4, z + 0.25))}>+</button>
          </div>
          {/* Scaled by WIDTH, not a transform: a transform's overflow to the left of
              the box cannot be scrolled to, which hid half a zoomed page. */}
          <div style={{ maxHeight: '75vh', overflow: 'auto' }}>
            <img src={api.pageImageUrl(pid, page.id)} alt={`Page ${page.seq}`}
                 style={{ width: `${zoom * 100}%`, maxWidth: 'none', display: 'block' }} />
          </div>
        </div>

        <div>
          {page.error && (
            <div className="card mb-3 p-3 text-sm" style={{ borderColor: 'var(--bad)' }}>
              <p style={{ color: 'var(--bad)' }}>The last read failed.</p>
              <p className="mt-1 text-xs text-muted">{page.error}</p>
            </div>
          )}

          {data.notes.length > 0 && (
            <div className="card mb-3 p-3 text-sm" style={{ borderColor: 'var(--warn)' }}>
              <p style={{ color: 'var(--warn)' }}>What the reader was unsure about</p>
              <ul className="mt-1 text-xs text-muted">
                {data.notes.map((note) => <li key={note}>· {note}</li>)}
              </ul>
            </div>
          )}

          {data.note && <p className="mb-3 text-xs" style={{ color: 'var(--warn)' }}>
            {data.note}
          </p>}

          {inFlight && (
            <p className="mb-3 text-sm text-muted">
              This page is waiting to be read again, or being read. It can be corrected
              once that finishes.
            </p>
          )}
          {status === 'skipped' && (
            <p className="mb-3 text-sm text-muted">
              This page is marked as not part of the text. Put it back to correct it.
            </p>
          )}

          {!page.read ? (
            <p className="mb-3 text-sm text-muted">
              This page has not been read yet, so there is nothing to check.
            </p>
          ) : regions.length === 0 ? (
            <p className="mb-3 text-sm text-muted">
              The reader found no text on this page.
            </p>
          ) : (
            regions.map((region, i) => (
              <RegionEditor key={region.id} index={i} region={region}
                            draft={drafts[region.id] ?? ''}
                            disabled={locked || busy}
                            onChange={change} onRevert={revert} />
            ))
          )}

          {corrected && (isManga ? (
            <p className="mb-3 text-xs text-hint">
              Lines translated from words you corrected are marked out of date in the
              reader until they are translated again.
            </p>
          ) : builtBefore && (
            <p className="mb-3 text-xs" style={{ color: 'var(--warn)' }}>
              You corrected this page after the chapters were built. Build them again to
              use it — chapters already translated from the old text will show as out of
              date.
            </p>
          ))}

          <div className="mt-2 flex flex-wrap gap-2">
            {page.read && !inFlight && status !== 'edited' && status !== 'skipped' && (
              <button type="button" className="btn btn-primary" disabled={busy}
                      onClick={() => act(() => api.setPageStatus(pid, page.id, 'edited'),
                        'Marked as checked.')}>
                Looks right
              </button>
            )}
            {status !== 'skipped' ? (
              <button type="button" className="btn" disabled={busy || inFlight}
                      title="Leave this page out of the text"
                      onClick={() => act(() => api.setPageStatus(pid, page.id, 'skipped'))}>
                Not text
              </button>
            ) : (
              <button type="button" className="btn" disabled={busy}
                      onClick={() => act(() => api.setPageStatus(pid, page.id, 'new'))}>
                Put it back
              </button>
            )}
          </div>

          {status !== 'skipped' && (
            <div className="card mt-4 p-3">
              <label className="block">
                <span className="mb-1 block text-xs text-muted">
                  A note for the reader, if it went wrong (optional)
                </span>
                <input className="field" value={hint} maxLength={500}
                       disabled={busy || inFlight}
                       placeholder="e.g. the bottom two lines are cut off"
                       onChange={(e) => setHint(e.target.value)} />
              </label>
              <button type="button" className="btn mt-2" disabled={busy || inFlight}
                      onClick={readAgain}>
                Read again
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
