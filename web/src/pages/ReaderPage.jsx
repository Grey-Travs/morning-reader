import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { api } from '../api'
import Explained from '../components/Explained'
import { STATUS_LABEL } from '../format'
import { inlineRuns, isDivider, splitParagraphs } from '../prose'

// The reader. Nothing is published, so this is the product — not a convenience view
// of something else.
//
// The one thing it must never do is present un-reviewed prose as finished. A chapter
// that failed its checks has its translation only in the audit copy; the server says
// so with `from_audit`, and this page says so loudly and offers the accept.

function Paragraph({ text }) {
  if (isDivider(text)) {
    return <p className="my-8 text-center text-hint" aria-hidden="true">{text}</p>
  }
  return (
    <p className="mb-5">
      {inlineRuns(text).map((run, i) =>
        run.em ? <em key={i}>{run.text}</em> : <span key={i}>{run.text}</span>)}
    </p>
  )
}

export default function ReaderPage() {
  const { pid, index } = useParams()
  const navigate = useNavigate()
  const [chapter, setChapter] = useState(null)
  const [error, setError] = useState(null)
  const [showSource, setShowSource] = useState(false)
  const [busy, setBusy] = useState(false)
  const topRef = useRef(null)

  const load = useCallback(async () => {
    try {
      setChapter(await api.read(pid, Number(index)))
      setError(null)
    } catch (err) {
      setError(err)
    }
  }, [pid, index])

  useEffect(() => { load() }, [load])

  // A new chapter starts at the top. Without this, moving on from halfway down one
  // chapter drops you halfway down the next.
  useEffect(() => { topRef.current?.scrollIntoView({ block: 'start' }) }, [index])

  const go = useCallback((to) => {
    if (to != null) navigate(`/work/${pid}/read/${to}`)
  }, [navigate, pid])

  // Arrow keys page through, which is what anyone reading a novel reaches for.
  useEffect(() => {
    const onKey = (event) => {
      if (event.target.matches('input, textarea, select')) return
      if (event.key === 'ArrowRight') go(chapter?.next)
      if (event.key === 'ArrowLeft') go(chapter?.prev)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [chapter, go])

  const accept = async () => {
    setBusy(true)
    try {
      await api.accept(pid, Number(index))
      await load()
    } catch (err) {
      setError(err)
    } finally {
      setBusy(false)
    }
  }

  if (error && !chapter) {
    return (
      <div className="page page-narrow">
        <Explained error={error} onRetry={load} />
        <p className="mt-4 text-sm">
          <Link to={`/work/${pid}`} style={{ color: 'var(--accent)' }}>Back to the work</Link>
        </p>
      </div>
    )
  }
  if (!chapter) return <div className="page text-sm text-hint">Loading…</div>

  const paragraphs = splitParagraphs(chapter.english)

  return (
    <div className="page page-narrow" ref={topRef}>
      <div className="mb-6 flex flex-wrap items-baseline justify-between gap-2">
        <div className="min-w-0">
          <p className="text-xs text-hint">
            <Link to={`/work/${pid}`} style={{ color: 'inherit' }}>Back to the work</Link>
            {' · '}chapter {chapter.index}
          </p>
          <h1 className="mt-1 font-source text-2xl font-medium">{chapter.title}</h1>
        </div>
        <button type="button" className="btn" onClick={() => setShowSource((v) => !v)}
                aria-pressed={showSource}>
          {showSource ? 'Hide the Japanese' : 'Show the Japanese'}
        </button>
      </div>

      {error && (
        <div className="mb-5">
          <Explained error={error} onDismiss={() => setError(null)} />
        </div>
      )}

      {chapter.from_audit && (
        <div className="card mb-6 p-4" style={{ borderColor: 'var(--warn)' }}>
          <p className="font-medium" style={{ color: 'var(--warn)' }}>
            This translation has not been accepted yet
          </p>
          <p className="mt-1 text-sm text-muted">
            It did not pass its checks, so it is being kept aside rather than treated as
            finished. Read it, and accept it if it is fine.
          </p>
          {chapter.failures?.length > 0 && (
            <ul className="mt-2 list-disc space-y-1 pl-5 text-sm text-muted">
              {chapter.failures.map((failure, i) => <li key={i}>{failure}</li>)}
            </ul>
          )}
          {chapter.leak_findings?.length > 0 && (
            <div className="mt-2 text-sm text-muted">
              Japanese still in the text:{' '}
              {chapter.leak_findings.slice(0, 4).map((finding, i) => (
                <span key={i} className="font-source">
                  {i > 0 && ' · '}
                  {finding.text} <span className="text-hint">(line {finding.line})</span>
                </span>
              ))}
            </div>
          )}
          <button type="button" className="btn btn-primary mt-3" onClick={accept}
                  disabled={busy}>
            {busy ? 'Accepting…' : 'Accept this chapter'}
          </button>
        </div>
      )}

      {chapter.english == null ? (
        <div className="card p-10 text-center text-muted"
             style={{ borderStyle: 'dashed', borderColor: 'var(--line-strong)' }}>
          <p>This chapter has not been translated yet.</p>
          <Link to={`/work/${pid}`} className="btn btn-primary mt-4 no-underline">
            Back to the work
          </Link>
        </div>
      ) : (
        <article className={showSource ? 'grid gap-8 lg:grid-cols-2' : ''}>
          <div className="leading-relaxed" style={{ fontSize: '1.05rem' }}>
            {paragraphs.map((text, i) => <Paragraph key={i} text={text} />)}
          </div>
          {showSource && (
            <div className="font-source text-muted"
                 style={{ borderLeft: '1px solid var(--line)', paddingLeft: '1.25rem' }}>
              {chapter.source.map((text, i) => <p key={i} className="mb-5">{text}</p>)}
            </div>
          )}
        </article>
      )}

      <nav className="mt-10 flex items-center justify-between gap-3"
           style={{ borderTop: '1px solid var(--line)', paddingTop: '1rem' }}>
        <button type="button" className="btn" disabled={chapter.prev == null}
                onClick={() => go(chapter.prev)}>
          ← Previous
        </button>
        <span className="text-xs text-hint">
          {STATUS_LABEL[chapter.status] ?? chapter.status}
          {chapter.accepted && ' · accepted'}
        </span>
        <button type="button" className="btn" disabled={chapter.next == null}
                onClick={() => go(chapter.next)}>
          Next →
        </button>
      </nav>
    </div>
  )
}
