import { useCallback, useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api } from '../api'
import Explained from '../components/Explained'
import { countLabel } from '../format'

// The chapter as a screenplay: page, panel, speaker, line, with the Japanese beside
// each English. Driven by the same payload as the reader, so the two cannot disagree
// about what was said or in what order.
//
// This is where speaker attribution is decided, and it is gated harder than the
// English is. A wrong speaker propagates — it is the kind of mistake that reads
// perfectly well and quietly makes two characters into one — so a model's suggestion
// stays visibly a suggestion until a human confirms it, and a name the glossary has
// never heard of is called out rather than accepted silently.

function Panel({ pid, page, panel, number, lines, onSave, busy, known }) {
  return (
    <div className="mt-3">
      {/* A line in no panel says so rather than borrowing a number. It used to take
          its position on the page, which belonged to a different, real panel. */}
      <p className="text-xs text-hint">
        Page {page.seq}{number ? ` · panel ${number}` : ''}
      </p>
      <ul className="mt-1 grid gap-2">
        {lines.map((line) => (
          <Line key={line.id} pid={pid} page={page} line={line} onSave={onSave}
                busy={busy} known={known} />
        ))}
      </ul>
    </div>
  )
}

function Line({ pid, page, line, onSave, busy, known }) {
  const [speaker, setSpeaker] = useState(line.speaker || '')
  useEffect(() => { setSpeaker(line.speaker || '') }, [line.speaker])

  const suggested = line.speaker && line.speaker_source !== 'user'
  const unknownName = line.speaker && !known.has(line.speaker)

  return (
    <li className="card p-3">
      <div className="flex flex-wrap items-center gap-2">
        <input
          className="field"
          style={{ maxWidth: '12rem' }}
          value={speaker}
          placeholder="who says it"
          aria-label={`Speaker for ${line.id}`}
          onChange={(e) => setSpeaker(e.target.value)}
          onBlur={() => {
            if (speaker !== (line.speaker || '')) onSave(line.id, { speaker })
          }}
        />
        {suggested && (
          <>
            <span className="pill text-hint">suggested</span>
            <button type="button" className="btn" disabled={busy}
                    onClick={() => onSave(line.id, { speaker: line.speaker })}>
              Confirm
            </button>
          </>
        )}
        {line.speaker_source === 'user' && (
          <span className="pill" style={{ color: 'var(--accent)' }}>confirmed</span>
        )}
        {unknownName && (
          <span className="text-xs" style={{ color: 'var(--warn)' }}>
            not a name you have approved
          </span>
        )}
        {line.stale && (
          <span className="pill" style={{ color: 'var(--warn)' }}>
            the Japanese changed
          </span>
        )}
        <span className="pill text-hint">{line.kind}</span>
      </div>

      <div className="mt-2 grid gap-2 sm:grid-cols-2">
        <p className="font-source text-sm text-muted">{line.text}</p>
        <p className="text-sm">
          {line.english || <span className="text-hint">not translated yet</span>}
        </p>
      </div>
    </li>
  )
}

export default function MangaScriptPage() {
  const { pid, index } = useParams()
  const [chapter, setChapter] = useState(null)
  const [known, setKnown] = useState(() => new Set())
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)

  const load = useCallback(async () => {
    try {
      setChapter(await api.mangaChapter(pid, Number(index)))
      setError(null)
    } catch (err) {
      setError(err)
    }
  }, [pid, index])

  useEffect(() => { load() }, [load])

  // The approved glossary, so a speaker nobody has agreed to can be called out. The
  // same gate `queue_new_terms` applies to readings, and for the same reason: a wrong
  // name propagates to every later chapter and is expensive to unpick.
  useEffect(() => {
    api.glossary(pid)
      .then((body) => setKnown(new Set(
        (body.entries || []).map((e) => e.english).filter(Boolean))))
      .catch(() => setKnown(new Set()))
  }, [pid])

  const save = async (page, regionId, body) => {
    setBusy(true)
    try {
      await api.setLine(pid, page.id, regionId, body)
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
      </div>
    )
  }
  if (!chapter) return <div className="page text-sm text-hint">Loading…</div>

  const counts = chapter.counts || {}

  return (
    <div className="page page-narrow">
      <div className="mb-5">
        <p className="text-xs text-hint">
          <Link to={`/work/${pid}/manga/${index}`} style={{ color: 'inherit' }}>
            Back to the reader
          </Link>
        </p>
        <h1 className="mt-1 font-source text-2xl font-medium">
          {chapter.title} — script
        </h1>
        <p className="mt-1 text-sm text-hint">
          {countLabel(counts.lines || 0, 'line')}
          {' · '}{counts.translated || 0} translated
          {counts.stale > 0 && ` · ${counts.stale} out of date`}
        </p>
      </div>

      {error && (
        <div className="mb-4">
          <Explained error={error} onDismiss={() => setError(null)} />
        </div>
      )}

      {chapter.pages.map((page) => {
        const lines = (page.regions || []).filter((r) => r.translatable)
        if (!lines.length) {
          return (
            <div key={page.id} className="mt-3">
              <p className="text-xs text-hint">Page {page.seq}</p>
              <p className="card mt-1 p-3 text-sm text-muted">
                Nothing is said on this page.
              </p>
            </div>
          )
        }
        // Grouped by the panel the geometry derived, in reading order. A wrong panel
        // groups two lines under one heading — visible and harmless, which is exactly
        // why it can be derived rather than paid for.
        const groups = []
        for (const line of lines) {
          const last = groups[groups.length - 1]
          if (last && last.panel === line.panel) last.lines.push(line)
          else groups.push({ panel: line.panel, lines: [line] })
        }
        return groups.map((group, i) => (
          <Panel
            key={`${page.id}-${i}`}
            pid={pid}
            page={page}
            panel={group.panel}
            number={group.panel}
            lines={group.lines}
            busy={busy}
            known={known}
            onSave={(regionId, body) => save(page, regionId, body)}
          />
        ))
      })}
    </div>
  )
}
