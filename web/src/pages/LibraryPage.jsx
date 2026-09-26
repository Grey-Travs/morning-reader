import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { api } from '../api'
import Explained from '../components/Explained'
import { countLabel, whenish } from '../format'

// The shelf, and the three ways a work gets onto it: pasted text or a .txt, a
// Google Doc, or a stack of photographs. The third creates an EMPTY work and
// hands you straight to its pages — there is nothing to split until they are read.

const MODES = [
  { value: 'heading', label: 'Split on chapter headings',
    hint: '第3話, 3話, 第三章, プロローグ, Chapter 3 …' },
  { value: 'separator', label: 'Split on a separator line', hint: '--- or === on its own line' },
  { value: 'single', label: 'Keep as one chapter', hint: 'No splitting at all' },
]

export default function LibraryPage() {
  const navigate = useNavigate()
  const [projects, setProjects] = useState(null)
  const [error, setError] = useState(null)
  const [adding, setAdding] = useState(false)

  const load = useCallback(async () => {
    try {
      setProjects((await api.listProjects()).projects || [])
      setError(null)
    } catch (err) {
      setError(err)
    }
  }, [])

  useEffect(() => { load() }, [load])

  return (
    <div className="page">
      <div className="mb-6 flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-medium tracking-tight">Library</h1>
          <p className="mt-1 text-sm text-hint">
            {projects == null ? 'Loading…'
              : projects.length === 0 ? 'Nothing here yet.'
              : countLabel(projects.length, 'work')}
          </p>
        </div>
        <button type="button" className="btn btn-primary"
                onClick={() => setAdding((v) => !v)}>
          {adding ? 'Cancel' : 'Add a work'}
        </button>
      </div>

      {error && (
        <div className="mb-5">
          <Explained error={error} onRetry={load} onDismiss={() => setError(null)} />
        </div>
      )}

      {adding && (
        <div className="mb-6">
          <AddWork
            onAdded={(project) => navigate(project.ingest === 'images'
              ? `/work/${project.id}/pages`
              : `/work/${project.id}`)}
            onError={setError}
          />
        </div>
      )}

      {projects != null && projects.length === 0 && !adding && (
        <div className="card p-10 text-center text-muted"
             style={{ borderStyle: 'dashed', borderColor: 'var(--line-strong)' }}>
          <p>Paste a Japanese novel, or drop a .txt file, and it becomes a work here.</p>
          <button type="button" className="btn btn-primary mt-4"
                  onClick={() => setAdding(true)}>
            Add the first one
          </button>
        </div>
      )}

      <div className="grid gap-3 sm:grid-cols-2">
        {(projects || []).map((project) => (
          <Link key={project.id} to={`/work/${project.id}`}
                className="card p-4 no-underline" style={{ color: 'var(--ink)' }}>
            <div className="flex items-start justify-between gap-2">
              <span className="min-w-0 flex-1 truncate font-medium">{project.title}</span>
              <span className="pill shrink-0 text-hint">{project.kind}</span>
            </div>
            <div className="mt-1 text-xs text-hint">
              {countLabel(project.chapter_count || 0, 'chapter')}
              {project.status && project.status !== 'ongoing' && ` · ${project.status}`}
              {project.updated_at && ` · ${whenish(project.updated_at)}`}
            </div>
          </Link>
        ))}
      </div>
    </div>
  )
}

function AddWork({ onAdded, onError }) {
  const [title, setTitle] = useState('')
  const [kind, setKind] = useState('novel')
  const [mode, setMode] = useState('heading')
  const [text, setText] = useState('')
  const [source, setSource] = useState('text')   // text | docs | scans
  const [document, setDocument] = useState('')
  const [google, setGoogle] = useState(null)
  const [busy, setBusy] = useState(false)
  const fileRef = useRef(null)

  // Only asked for when the Google tab is opened. Most works never touch Google, and
  // checking on every page load would be a request nobody needed.
  useEffect(() => {
    if (source !== 'docs' || google) return
    api.googleStatus().then(setGoogle).catch(() => setGoogle(null))
  }, [source, google])

  const connect = async () => {
    setBusy(true)
    try {
      await api.googleConnect()
      setGoogle(await api.googleStatus())
    } catch (err) {
      onError(err)
    } finally {
      setBusy(false)
    }
  }

  const submit = async (event) => {
    event.preventDefault()
    if (busy) return
    setBusy(true)
    try {
      let created
      if (source === 'docs') {
        created = await api.createFromDoc({ document, title, kind })
      } else if (source === 'scans') {
        created = await api.createFromScans({ title, kind })
      } else {
        const file = fileRef.current?.files?.[0]
        // A chosen file wins over the textarea: picking one and then not noticing the
        // box still had text in it should not silently import the wrong thing.
        created = file
          ? await api.uploadTxt(file, { title, kind, mode })
          : await api.createFromText({ title, kind, mode, text })
      }
      onAdded(created.project)
    } catch (err) {
      onError(err)
    } finally {
      setBusy(false)
    }
  }

  const chosen = mode
  return (
    <form className="card p-4" onSubmit={submit}>
      <div className="grid gap-3 sm:grid-cols-2">
        <label className="block">
          <span className="mb-1 block text-xs text-muted">Title</span>
          <input className="field" value={title} onChange={(e) => setTitle(e.target.value)}
                 placeholder="Leave blank to use the file name" />
        </label>
        <label className="block">
          <span className="mb-1 block text-xs text-muted">What is it?</span>
          <select className="field" value={kind} onChange={(e) => {
            setKind(e.target.value)
            // A manga is its pictures. From text or a Doc it would be a work with no
            // pages that nothing can translate or read — the server refuses it too.
            if (e.target.value === 'manga') setSource('scans')
          }}>
            <option value="novel">Novel</option>
            <option value="manga">Manga</option>
          </select>
        </label>
      </div>

      <fieldset className="mt-4">
        <legend className="mb-1 text-xs text-muted">Where is it coming from?</legend>
        <div className="flex flex-wrap gap-4 text-sm">
          {[['text', 'Paste it, or a .txt file'], ['docs', 'A Google Doc'],
            ['scans', 'Photographs or scans']].map(
            ([value, label]) => (
              <label key={value} className="flex items-center gap-2">
                <input type="radio" name="source" value={value}
                       checked={source === value}
                       disabled={kind === 'manga' && value !== 'scans'}
                       onChange={() => setSource(value)} />
                <span className={kind === 'manga' && value !== 'scans' ? 'text-hint' : ''}>
                  {label}
                </span>
              </label>
            ))}
        </div>
        {kind === 'manga' && (
          <p className="mt-1 text-xs text-hint">
            A manga is added from its page images — the words are read off the art, and
            the English is placed back over it.
          </p>
        )}
      </fieldset>

      {source === 'docs' && (
        <div className="mt-4">
          {google && !google.connected ? (
            <div className="card p-4">
              <p className="text-sm text-muted">
                {google.credentials_present
                  ? 'Morning Reader is not signed in to Google yet.'
                  : 'No OAuth client is set up yet. Create one (Desktop app) in the '
                    + 'Google Cloud console under APIs & Services → Credentials, '
                    + 'download it, and save it as client_secret.json next to '
                    + 'launch.py.'}
              </p>
              <p className="mt-1 text-xs text-hint">
                It asks for read-only access to documents — this app never writes to one.
              </p>
              {google.credentials_present && (
                <button type="button" className="btn btn-primary mt-3" disabled={busy}
                        onClick={connect}>
                  {busy ? 'Waiting for Google…' : 'Connect Google'}
                </button>
              )}
            </div>
          ) : (
            <label className="block">
              <span className="mb-1 block text-xs text-muted">
                The document&rsquo;s link, or its id — one chapter per tab
              </span>
              <input className="field" value={document}
                     onChange={(e) => setDocument(e.target.value)}
                     placeholder="https://docs.google.com/document/d/…" />
            </label>
          )}
        </div>
      )}

      {source === 'scans' && (
        <p className="mt-4 text-sm text-muted">
          The work is created empty and you are taken to its pages, where you add the
          images. Nothing is read — or billed — until you ask for it.
        </p>
      )}

      {source === 'text' && (
      <>
      <fieldset className="mt-4">
        <legend className="mb-1 text-xs text-muted">How should it be split?</legend>
        <div className="grid gap-1">
          {MODES.map((option) => (
            <label key={option.value} className="flex items-baseline gap-2 text-sm">
              <input type="radio" name="mode" value={option.value}
                     checked={chosen === option.value}
                     onChange={() => setMode(option.value)} />
              <span>
                {option.label}
                <span className="ml-2 text-xs text-hint">{option.hint}</span>
              </span>
            </label>
          ))}
        </div>
      </fieldset>

      <label className="mt-4 block">
        <span className="mb-1 block text-xs text-muted">Paste the text</span>
        <textarea className="field font-source" rows={8} value={text}
                  onChange={(e) => setText(e.target.value)}
                  placeholder="第1話　…" />
      </label>

      <label className="mt-3 block">
        <span className="mb-1 block text-xs text-muted">…or choose a .txt file</span>
        <input ref={fileRef} type="file" accept=".txt,text/plain" className="text-sm" />
        <span className="mt-1 block text-xs text-hint">
          Shift_JIS and EUC-JP files are decoded too, not just UTF-8.
        </span>
      </label>
      </>
      )}

      <div className="mt-4 flex items-center gap-2">
        <button type="submit" className="btn btn-primary"
                disabled={busy || (source === 'docs' && google && !google.connected)}>
          {busy ? 'Adding…' : 'Add it'}
        </button>
      </div>
    </form>
  )
}
