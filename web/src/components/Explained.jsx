import { useState } from 'react'

// Render a failure the way the server explained it.
//
// The server never sends a bare string — every error arrives as the same object with
// a title, a plain-English description, and ordered fixes (server/errors.Explained).
// This component is the reason that shape is worth having: one place renders it, so a
// failure looks the same wherever it happens, and the traceback stays behind a
// disclosure instead of in the user's face.

export default function Explained({ error, onRetry, onDismiss }) {
  const [showDetail, setShowDetail] = useState(false)
  if (!error) return null
  const e = error.explained || error

  return (
    <div className="card p-4" style={{ borderColor: 'var(--bad)' }} role="alert">
      <div className="flex items-start gap-3">
        <div className="min-w-0 flex-1">
          <p className="font-medium" style={{ color: 'var(--bad)' }}>
            {e.title || 'Something went wrong'}
          </p>
          {e.what && <p className="mt-1 text-sm text-muted">{e.what}</p>}
          {Array.isArray(e.fixes) && e.fixes.length > 0 && (
            <ul className="mt-2 list-disc space-y-1 pl-5 text-sm text-muted">
              {e.fixes.map((fix, i) => <li key={i}>{fix}</li>)}
            </ul>
          )}

          <div className="mt-3 flex flex-wrap items-center gap-2">
            {e.retryable && onRetry && (
              <button type="button" className="btn" onClick={onRetry}>Try again</button>
            )}
            {onDismiss && (
              <button type="button" className="btn" onClick={onDismiss}>Dismiss</button>
            )}
            {(e.detail || e.trace) && (
              <button
                type="button"
                className="btn"
                aria-expanded={showDetail}
                onClick={() => setShowDetail((v) => !v)}
              >
                {showDetail ? 'Hide' : 'Technical details'}
              </button>
            )}
          </div>

          {showDetail && (
            <pre className="console mt-3" style={{ maxHeight: '14rem' }}>
              {e.trace || e.detail}
            </pre>
          )}
        </div>
      </div>
    </div>
  )
}
