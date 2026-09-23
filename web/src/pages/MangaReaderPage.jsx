import { useCallback, useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { api } from '../api'
import Explained from '../components/Explained'
import PageOverlay from '../components/PageOverlay'
import { countLabel } from '../format'

// The manga reader. Nothing publishes, so this is the product.
//
// Two things it must get right, and both are invisible when wrong:
//
//  * **A page read left-to-right is backwards sentence by sentence**, and every
//    sentence is still fluent English. The geometry catches it for free, so the fix is
//    one click and no model call.
//  * **English whose Japanese has since changed must not look finished.** A stale line
//    is marked in both this view and the script view, and the English is kept — never
//    deleted — because nothing in this app deletes prose.
//
// Navigation is right-to-left, like the book. On a manga page the LEFT arrow goes
// forward; getting that backwards would be a small daily annoyance for anyone who
// actually reads manga.

function LineRow({ line, index, selected, onSelect, onMove, onSave, busy, first, last }) {
  const [editing, setEditing] = useState(false)
  const [english, setEnglish] = useState(line.english || '')
  const [speaker, setSpeaker] = useState(line.speaker || '')

  useEffect(() => {
    setEnglish(line.english || '')
    setSpeaker(line.speaker || '')
  }, [line.english, line.speaker])

  return (
    <li
      className="card p-3"
      style={{ borderColor: selected ? 'var(--accent)' : undefined }}
      onClick={() => onSelect(line.id)}
    >
      <div className="flex items-baseline justify-between gap-2">
        <span className="text-xs text-hint">
          {index + 1} · panel {line.panel || '?'} · {line.kind}
        </span>
        <span className="flex items-center gap-1">
          {line.stale && (
            <span className="pill" style={{ color: 'var(--warn)' }}>
              the Japanese changed
            </span>
          )}
          {line.english_source === 'user' && (
            <span className="pill" style={{ color: 'var(--accent)' }}>yours</span>
          )}
          <button type="button" className="btn" disabled={busy || first}
                  aria-label={`Move line ${index + 1} earlier`}
                  onClick={(e) => { e.stopPropagation(); onMove(line.id, -1) }}>←</button>
          <button type="button" className="btn" disabled={busy || last}
                  aria-label={`Move line ${index + 1} later`}
                  onClick={(e) => { e.stopPropagation(); onMove(line.id, 1) }}>→</button>
        </span>
      </div>

      <p className="mt-1 font-source text-sm text-muted">{line.text}</p>

      {editing ? (
        <div className="mt-2" onClick={(e) => e.stopPropagation()}>
          <label className="block">
            <span className="mb-1 block text-xs text-muted">Speaker</span>
            <input className="field" value={speaker}
                   aria-label={`Speaker for line ${index + 1}`}
                   onChange={(e) => setSpeaker(e.target.value)} />
          </label>
          <label className="mt-2 block">
            <span className="mb-1 block text-xs text-muted">English</span>
            <textarea className="field" rows={2} value={english}
                      aria-label={`English for line ${index + 1}`}
                      onChange={(e) => setEnglish(e.target.value)} />
          </label>
          <div className="mt-2 flex gap-2">
            <button type="button" className="btn btn-primary" disabled={busy}
                    onClick={() => { onSave(line.id, { english, speaker }); setEditing(false) }}>
              Save
            </button>
            <button type="button" className="btn" onClick={() => setEditing(false)}>
              Cancel
            </button>
          </div>
        </div>
      ) : (
        <div className="mt-1">
          {line.speaker && (
            <span className="mr-2 text-sm font-medium">
              {line.speaker}
              {line.speaker_source !== 'user' && (
                <span className="ml-1 text-xs text-hint">(suggested)</span>
              )}
            </span>
          )}
          <span className="text-sm">
            {line.english || <span className="text-hint">not translated yet</span>}
          </span>
          <button type="button" className="btn ml-2" disabled={busy}
                  onClick={(e) => { e.stopPropagation(); setEditing(true) }}>
            Edit
          </button>
        </div>
      )}
    </li>
  )
}

export default function MangaReaderPage() {
  const { pid, index } = useParams()
  const navigate = useNavigate()
  const [chapter, setChapter] = useState(null)
  const [error, setError] = useState(null)
  const [at, setAt] = useState(0)          // which page of the chapter
  const [selected, setSelected] = useState(null)
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState('')

  const load = useCallback(async () => {
    try {
      setChapter(await api.mangaChapter(pid, Number(index)))
      setError(null)
    } catch (err) {
      setError(err)
    }
  }, [pid, index])

  useEffect(() => { load() }, [load])
  useEffect(() => { setAt(0); setSelected(null) }, [index])

  const page = chapter?.pages?.[at] || null
  const lines = (page?.regions || []).filter((r) => r.translatable)

  const act = async (fn, message = '') => {
    setBusy(true)
    setNotice('')
    try {
      await fn()
      if (message) setNotice(message)
      await load()
    } catch (err) {
      setError(err)
    } finally {
      setBusy(false)
    }
  }

  // The whole permutation is always sent, never a delta — the server refuses anything
  // that is not one, so a delta could not corrupt the page, but it would look like a
  // broken button.
  const move = (regionId, delta) => {
    const ids = lines.map((l) => l.id)
    const from = ids.indexOf(regionId)
    const to = from + delta
    if (from < 0 || to < 0 || to >= ids.length) return
    ids.splice(to, 0, ...ids.splice(from, 1))
    // Every region on the page, not just the translatable ones: the server requires a
    // permutation of ALL of them.
    const rest = (page.regions || []).filter((r) => !r.translatable).map((r) => r.id)
    return act(() => api.setPageOrder(pid, page.id, [...ids, ...rest]))
  }

  const go = useCallback((delta) => {
    setAt((n) => {
      const next = n + delta
      return next < 0 || next >= (chapter?.pages?.length || 0) ? n : next
    })
    setSelected(null)
  }, [chapter])

  // Right-to-left, like the book: LEFT goes forward.
  useEffect(() => {
    const onKey = (event) => {
      if (event.target.matches('input, textarea, select')) return
      if (event.key === 'ArrowLeft') go(1)
      if (event.key === 'ArrowRight') go(-1)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [go])

  const translate = async () => {
    const ok = window.confirm(
      `Translate ${chapter.title || `chapter ${index}`}?\n\n`
      + 'This uses your Claude plan. The whole chapter goes in one call, so every '
      + 'bubble is translated knowing what was said on the pages around it.')
    if (!ok) return
    await act(() => api.translateManga(pid, { indices: [Number(index)], force: true }),
      'Queued. Watch it in Activity.')
  }

  if (error && !chapter) {
    return (
      <div className="page page-narrow">
        <Explained error={error} onRetry={load} />
        <p className="mt-4 text-sm">
          <Link to={`/work/${pid}`} style={{ color: 'var(--accent)' }}>
            Back to the work
          </Link>
        </p>
      </div>
    )
  }
  if (!chapter) return <div className="page text-sm text-hint">Loading…</div>

  const counts = chapter.counts || {}
  const reversed = page?.order_check?.looks_reversed
    && page?.order_check?.order_source !== 'user'

  return (
    <div className="page">
      <div className="mb-4">
        <p className="text-xs text-hint">
          <Link to={`/work/${pid}`} style={{ color: 'inherit' }}>Back to the work</Link>
          {' · '}chapter {chapter.index}
          {' · '}
          <Link to={`/work/${pid}/manga/${index}/script`}
                style={{ color: 'var(--accent)' }}>Script</Link>
        </p>
        <h1 className="mt-1 font-source text-2xl font-medium">{chapter.title}</h1>
        <p className="mt-1 text-sm text-hint">
          {countLabel(counts.pages || 0, 'page')}
          {' · '}{counts.translated || 0} of {counts.lines || 0} lines translated
          {counts.stale > 0 && ` · ${counts.stale} out of date`}
          {counts.unchecked_pages > 0
            && ` · ${counts.unchecked_pages} page${counts.unchecked_pages === 1 ? '' : 's'} you have not checked`}
        </p>
      </div>

      {error && (
        <div className="mb-4">
          <Explained error={error} onDismiss={() => setError(null)} />
        </div>
      )}
      {notice && <div className="card mb-4 p-3 text-sm text-muted">{notice}</div>}

      <div className="mb-4 flex flex-wrap items-center gap-2">
        <button type="button" className="btn btn-primary" disabled={busy}
                onClick={translate}>
          {counts.translated ? 'Translate this chapter again' : 'Translate this chapter'}
        </button>
        <span className="flex-1" />
        <button type="button" className="btn" disabled={at === 0}
                onClick={() => go(-1)}>Previous page →</button>
        <span className="text-xs text-hint">
          {at + 1} / {chapter.pages.length}
        </span>
        <button type="button" className="btn"
                disabled={at >= chapter.pages.length - 1}
                onClick={() => go(1)}>← Next page</button>
      </div>

      {chapter.order_changed && (
        <div className="card mb-4 p-3 text-sm text-muted">
          The reading order changed after this chapter was translated. Nothing has gone
          out of date — no bubble&rsquo;s Japanese changed — but a line translated
          believing it followed another one now follows a different one, which can
          change the English. Translate it again if it reads oddly.
        </div>
      )}

      {reversed && (
        <div className="card mb-4 p-4" style={{ borderColor: 'var(--warn)' }}>
          <p className="font-medium" style={{ color: 'var(--warn)' }}>
            This page&rsquo;s reading order looks left-to-right
          </p>
          <p className="mt-1 text-sm text-muted">
            Japanese reads right-to-left, so this page is probably backwards — which is
            invisible, because every line is still fluent English. The page&rsquo;s own
            layout disagrees with it, and using that costs nothing.
          </p>
          <button type="button" className="btn btn-primary mt-3" disabled={busy}
                  onClick={() => act(
                    () => api.setPageOrder(pid, page.id, page.order_proposal),
                    'Reading order taken from the layout.')}>
            Use the layout&rsquo;s order
          </button>
        </div>
      )}

      {page?.order_note && (
        <div className="card mb-4 p-3 text-sm" style={{ borderColor: 'var(--warn)' }}>
          {page.order_note}
        </div>
      )}

      {page && (
        <div className="grid gap-5 lg:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
          <div>
            <PageOverlay pid={pid} page={page} selected={selected}
                         onSelect={setSelected} />
            <p className="mt-2 text-xs text-hint">
              Page {page.seq}
              {page.status === 'needs-check'
                && ' · you have not checked this page yet'}
              {page.order_source === 'user' && ' · reading order is yours'}
            </p>
          </div>

          <div>
            {lines.length === 0 ? (
              <p className="card p-4 text-sm text-muted">
                Nothing is said on this page. That is ordinary in a manga — a splash or
                an action beat — and the page is still part of the chapter.
              </p>
            ) : (
              <ul className="grid gap-2">
                {lines.map((line, i) => (
                  <LineRow
                    key={line.id}
                    line={line}
                    index={i}
                    first={i === 0}
                    last={i === lines.length - 1}
                    busy={busy}
                    selected={selected === line.id}
                    onSelect={setSelected}
                    onMove={move}
                    onSave={(regionId, body) =>
                      act(() => api.setLine(pid, page.id, regionId, body))}
                  />
                ))}
              </ul>
            )}
          </div>
        </div>
      )}

      <nav className="mt-8 flex items-center justify-between gap-3"
           style={{ borderTop: '1px solid var(--line)', paddingTop: '1rem' }}>
        <button type="button" className="btn" disabled={chapter.prev == null}
                onClick={() => navigate(`/work/${pid}/manga/${chapter.prev}`)}>
          ← Previous chapter
        </button>
        <span className="text-xs text-hint">
          {chapter.cost_usd ? `$${chapter.cost_usd.toFixed(4)}` : ''}
        </span>
        <button type="button" className="btn" disabled={chapter.next == null}
                onClick={() => navigate(`/work/${pid}/manga/${chapter.next}`)}>
          Next chapter →
        </button>
      </nav>
    </div>
  )
}
