import { useCallback, useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api } from '../api'
import Explained from '../components/Explained'
import { countLabel } from '../format'

// The human gate. Nothing the model proposes reaches the glossary without passing
// through here, because a wrong reading propagates to every later chapter and
// Japanese makes wrong readings easy — most kanji have several.
//
// So the queue is EDITABLE, not just approve/reject. Correcting the reading before
// locking it is the main thing this screen is for.

const TYPES = ['name', 'place', 'skill', 'term', 'other']
const PRONOUNS = ['', 'he', 'she', 'they']
const REGISTERS = ['', 'plain', 'polite', 'honorific', 'humble', 'rough', 'archaic']

function PendingTerm({ term, onApprove, onReject, busy }) {
  const [draft, setDraft] = useState(() => ({
    source: term.source || '',
    english: term.english || '',
    reading: term.reading || '',
    variants: (term.variants || []).join(', '),
    type: TYPES.includes(term.type) ? term.type : 'other',
    note: term.note || '',
    pronoun: PRONOUNS.includes(term.pronoun) ? term.pronoun : '',
    register: term.register || '',
    first_person: term.first_person || '',
  }))

  const set = (key) => (event) => setDraft((d) => ({ ...d, [key]: event.target.value }))

  return (
    <div className="card p-4">
      <div className="mb-3 flex flex-wrap items-baseline gap-2">
        <span className="font-source text-lg">{term.source}</span>
        {term.note && <span className="text-xs text-hint">{term.note}</span>}
      </div>

      <div className="grid gap-3 sm:grid-cols-2">
        <label className="block">
          <span className="mb-1 block text-xs text-muted">English spelling</span>
          <input className="field" value={draft.english} onChange={set('english')} />
        </label>
        <label className="block">
          <span className="mb-1 block text-xs text-muted">
            Reading (kana) — leave blank if the chapter did not show one
          </span>
          <input className="field font-source" value={draft.reading}
                 onChange={set('reading')} placeholder="ささき" />
        </label>
        <label className="block">
          <span className="mb-1 block text-xs text-muted">Type</span>
          <select className="field" value={draft.type} onChange={set('type')}>
            {TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
          </select>
        </label>
        <label className="block">
          <span className="mb-1 block text-xs text-muted">
            Other spellings, comma separated
          </span>
          <input className="field font-source" value={draft.variants}
                 onChange={set('variants')} placeholder="ササキ, ささき" />
        </label>
        {draft.type === 'name' && (
          <>
            <label className="block">
              <span className="mb-1 block text-xs text-muted">Pronoun</span>
              <select className="field" value={draft.pronoun} onChange={set('pronoun')}>
                {PRONOUNS.map((p) => (
                  <option key={p || 'unknown'} value={p}>{p || 'not known'}</option>
                ))}
              </select>
            </label>
            <label className="block">
              <span className="mb-1 block text-xs text-muted">
                Refers to self as — English only has “I”, so this has to carry in tone
              </span>
              <input className="field font-source" value={draft.first_person}
                     onChange={set('first_person')} placeholder="俺 / 僕 / 私" />
            </label>
            <label className="block">
              <span className="mb-1 block text-xs text-muted">How they speak</span>
              <select className="field" value={draft.register} onChange={set('register')}>
                {REGISTERS.map((r) => (
                  <option key={r || 'unset'} value={r}>{r || 'not set'}</option>
                ))}
              </select>
            </label>
          </>
        )}
      </div>

      <div className="mt-4 flex flex-wrap items-center gap-2">
        <button type="button" className="btn btn-primary" disabled={busy}
                onClick={() => onApprove({
                  ...draft,
                  variants: draft.variants.split(',').map((v) => v.trim()).filter(Boolean),
                })}>
          Lock this spelling
        </button>
        <button type="button" className="btn" disabled={busy}
                onClick={() => onReject(term.source)}>
          Not this one
        </button>
      </div>
    </div>
  )
}

export default function GlossaryPage() {
  const { pid } = useParams()
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)

  const load = useCallback(async () => {
    try {
      setData(await api.glossary(pid))
      setError(null)
    } catch (err) {
      setError(err)
    }
  }, [pid])

  useEffect(() => { load() }, [load])

  const act = async (fn) => {
    setBusy(true)
    try {
      await fn()
      await load()
    } catch (err) {
      setError(err)
    } finally {
      setBusy(false)
    }
  }

  if (error && !data) {
    return (
      <div className="page page-narrow">
        <Explained error={error} onRetry={load} />
      </div>
    )
  }
  if (!data) return <div className="page text-sm text-hint">Loading…</div>

  const { entries, pending } = data

  return (
    <div className="page page-narrow">
      <div className="mb-6">
        <p className="text-xs text-hint">
          <Link to={`/work/${pid}`} style={{ color: 'inherit' }}>Back to the work</Link>
        </p>
        <h1 className="mt-1 text-2xl font-medium tracking-tight">Glossary</h1>
        <p className="mt-1 text-sm text-hint">
          {countLabel(entries.length, 'locked term')}
          {pending.length > 0 && ` · ${countLabel(pending.length, 'waiting')}`}
        </p>
      </div>

      {error && (
        <div className="mb-5">
          <Explained error={error} onDismiss={() => setError(null)} />
        </div>
      )}

      {pending.length > 0 && (
        <section className="mb-10">
          <h2 className="mb-1 text-lg font-medium">Waiting on you</h2>
          <p className="mb-4 text-sm text-muted">
            Nothing here is in use yet. Check the reading before locking it — most kanji
            have several, and a wrong one follows the character through every later
            chapter.
          </p>
          <div className="space-y-3">
            {pending.map((term) => (
              <PendingTerm
                key={term.source}
                term={term}
                busy={busy}
                onApprove={(entry) => act(() => api.approveTerm(pid, entry))}
                onReject={(source) => act(() => api.rejectTerm(pid, source))}
              />
            ))}
          </div>
        </section>
      )}

      <section>
        <h2 className="mb-3 text-lg font-medium">Locked</h2>
        {entries.length === 0 ? (
          <div className="card p-8 text-center text-muted"
               style={{ borderStyle: 'dashed', borderColor: 'var(--line-strong)' }}>
            Nothing locked yet. Terms appear here once you approve them.
          </div>
        ) : (
          <div className="card overflow-hidden">
            <table className="w-full text-sm">
              <thead>
                <tr style={{ borderBottom: '1px solid var(--line)' }}>
                  <th className="p-2 text-left font-normal text-hint">Japanese</th>
                  <th className="p-2 text-left font-normal text-hint">English</th>
                  <th className="p-2 text-left font-normal text-hint">Type</th>
                  <th className="p-2 text-left font-normal text-hint">Notes</th>
                  <th className="w-10 p-2" />
                </tr>
              </thead>
              <tbody>
                {entries.map((entry) => {
                  const spellings = [entry.source, ...(entry.variants || []),
                                     entry.reading].filter(Boolean)
                  const hints = [entry.pronoun,
                                 entry.first_person && `says ${entry.first_person}`,
                                 entry.register].filter(Boolean)
                  return (
                    <tr key={entry.source || entry.english}
                        style={{ borderBottom: '1px solid var(--line)' }}>
                      <td className="p-2 font-source">
                        {spellings.length ? [...new Set(spellings)].join(' / ')
                          : <span className="text-hint">—</span>}
                      </td>
                      <td className="p-2">{entry.english}</td>
                      <td className="p-2"><span className="pill text-hint">{entry.type}</span></td>
                      <td className="p-2 text-hint">
                        {[hints.join(', '), entry.note].filter(Boolean).join(' — ')}
                      </td>
                      <td className="p-2 text-right">
                        <button type="button" className="btn" disabled={busy}
                                title="Unlock this term"
                                onClick={() => act(() =>
                                  api.removeTerm(pid, entry.source || entry.english))}>
                          ×
                        </button>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  )
}
