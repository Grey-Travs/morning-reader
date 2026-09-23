import { lazy, Suspense } from 'react'
import { Link, NavLink, Route, Routes, useLocation } from 'react-router-dom'

// Lazy so the first paint is the library, not the whole app. Small now; the reader
// and the overlay view (steps 2 and 4) are the ones that will make this matter.
const LibraryPage = lazy(() => import('./pages/LibraryPage'))
const ProjectPage = lazy(() => import('./pages/ProjectPage'))
const ActivityPage = lazy(() => import('./pages/ActivityPage'))
const ReaderPage = lazy(() => import('./pages/ReaderPage'))
const GlossaryPage = lazy(() => import('./pages/GlossaryPage'))
const PagesPage = lazy(() => import('./pages/PagesPage'))

const NAV = [
  { to: '/', label: 'Library', end: true },
  { to: '/activity', label: 'Activity' },
]

function Shell({ children }) {
  const { pathname } = useLocation()
  return (
    <div className="min-h-full">
      <header style={{ borderBottom: '1px solid var(--line)', background: 'var(--surface)' }}>
        <div className="page" style={{ paddingTop: '0.9rem', paddingBottom: '0.9rem' }}>
          <div className="flex flex-wrap items-center gap-x-6 gap-y-2">
            <Link to="/" className="text-base font-medium tracking-tight no-underline"
                  style={{ color: 'var(--ink)' }}>
              Morning Reader
            </Link>
            <nav className="flex items-center gap-4 text-sm">
              {NAV.map((item) => (
                <NavLink
                  key={item.to}
                  to={item.to}
                  end={item.end}
                  className="no-underline"
                  style={({ isActive }) => ({
                    color: isActive ? 'var(--accent)' : 'var(--muted)',
                    fontWeight: isActive ? 500 : 400,
                  })}
                >
                  {item.label}
                </NavLink>
              ))}
            </nav>
            <span className="ml-auto text-xs text-hint">
              Japanese novels &amp; manga · translate and read
            </span>
          </div>
        </div>
      </header>

      <main key={pathname}>{children}</main>
    </div>
  )
}

export default function App() {
  return (
    <Shell>
      <Suspense fallback={<div className="page text-sm text-hint">Loading…</div>}>
        <Routes>
          <Route path="/" element={<LibraryPage />} />
          <Route path="/activity" element={<ActivityPage />} />
          <Route path="/work/:pid" element={<ProjectPage />} />
          <Route path="/work/:pid/read/:index" element={<ReaderPage />} />
          <Route path="/work/:pid/glossary" element={<GlossaryPage />} />
          <Route path="/work/:pid/pages" element={<PagesPage />} />
          <Route path="*" element={<NotFound />} />
        </Routes>
      </Suspense>
    </Shell>
  )
}

function NotFound() {
  return (
    <div className="page page-narrow">
      <h1 className="text-xl font-medium">That page does not exist</h1>
      <p className="mt-2 text-sm text-muted">
        <Link to="/" style={{ color: 'var(--accent)' }}>Back to the library</Link>
      </p>
    </div>
  )
}
