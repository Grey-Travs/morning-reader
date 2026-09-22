import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { api } from '../api'
import Explained from '../components/Explained'
import { countLabel, whenish } from '../format'

// The shelf, plus the one ingestion path step 1 ships: paste text or drop a .txt.

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
            onAdded={(project) => navigate(`/work/${project.id}`)}
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
  const [busy, setBusy] = useState(false)
  const fileRef = useRef(null)

  const submit = async (event) => {
    event.preventDefault()
    if (busy) return
    setBusy(true)
    try {
      const file = fileRef.current?.files?.[0]
      // A chosen file wins over the textarea: picking one and then not noticing the
      // box still had text in it should not silently import the wrong thing.
      const created = file
        ? await api.uploadTxt(file, { title, kind, mode })
        : await api.createFromText({ title, kind, mode, text })
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
          <select className="field" value={kind} onChange={(e) => setKind(e.target.value)}>
            <option value="novel">Novel</option>
            <option value="manga">Manga</option>
          </select>
        </label>
      </div>

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

      <div className="mt-4 flex items-center gap-2">
        <button type="submit" className="btn btn-primary" disabled={busy}>
          {busy ? 'Adding…' : 'Add it'}
        </button>
      </div>
    </form>
  )
}
