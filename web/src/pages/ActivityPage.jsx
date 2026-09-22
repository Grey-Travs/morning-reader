import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api'
import { countLabel } from '../format'

// Every work with something in flight, across the whole app.
//
// Deliberately a poll rather than a stream: this view watches ALL projects, and a
// stream per project would open one connection per row. The per-work page is where
// the live console lives.

export default function ActivityPage() {
  const [jobs, setJobs] = useState(null)

  useEffect(() => {
    let alive = true
    const tick = async () => {
      try {
        const data = await api.queueOverview()
        // Normalise the shape so every downstream `job.pending` access is safe.
        if (alive) setJobs((data.jobs || []).map((j) => ({ ...j, pending: j.pending || [] })))
      } catch {
        // The server may be restarting; the next tick tries again.
      }
    }
    tick()
    const id = setInterval(tick, 3000)
    return () => { alive = false; clearInterval(id) }
  }, [])

  const inQueue = (jobs || []).reduce(
    (n, job) => n + (job.current != null ? 1 : 0) + job.pending.length, 0)

  return (
    <div className="page page-narrow">
      <div className="mb-6">
        <h1 className="text-2xl font-medium tracking-tight">Activity</h1>
        <p className="mt-1 text-sm text-hint">
          {jobs == null ? 'Loading…'
            : jobs.length === 0 ? 'Nothing running right now.'
            : `${countLabel(inQueue, 'item')} across ${countLabel(jobs.length, 'work')}.`}
        </p>
      </div>

      {jobs != null && jobs.length === 0 && (
        <div className="card p-10 text-center text-muted"
             style={{ borderStyle: 'dashed', borderColor: 'var(--line-strong)' }}>
          <p>
            Nothing is running. Open a work and prepare some chapters — progress shows
            up here and on that work&rsquo;s own page.
          </p>
          <Link to="/" className="btn btn-primary mt-4 no-underline">Go to the library</Link>
        </div>
      )}

      <div className="space-y-3">
        {(jobs || []).map((job) => {
          const count = (job.current != null ? 1 : 0) + job.pending.length
          const preview = job.pending.slice(0, 16).join(', ')
          return (
            <div key={job.pid} className="card p-4">
              <div className="flex items-center justify-between gap-2">
                <Link to={`/work/${job.pid}`} className="min-w-0 flex-1 truncate font-medium no-underline"
                      style={{ color: 'var(--ink)' }}>
                  {job.title}
                </Link>
                <span className="pill shrink-0 text-hint">{job.kind_of_work}</span>
                <span className="shrink-0 text-xs text-hint">{count} in queue</span>
              </div>

              <div className="mt-1 flex items-center gap-2 text-sm text-muted">
                <span className="inline-block h-2 w-2 shrink-0 rounded-full"
                      style={{ background: 'var(--accent)' }} />
                {job.waiting ? (
                  <span style={{ color: 'var(--warn)' }}>
                    Waiting for the usage window to refresh
                    {job.waiting.resume_at && (
                      <> · retries ~{new Date(job.waiting.resume_at * 1000)
                        .toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</>
                    )}
                  </span>
                ) : job.current != null ? (
                  <span>Chapter {job.current}</span>
                ) : (
                  <span>Queued</span>
                )}
              </div>

              {job.pending.length > 0 && (
                <div className="mt-1 text-xs text-hint">
                  waiting: {preview}
                  {job.pending.length > 16 && ` +${job.pending.length - 16} more`}
                </div>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}
