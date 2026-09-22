// Small shared formatters. Kept together so two views cannot disagree about what a
// status is called or how a count is written.

export const STATUS_LABEL = {
  '': 'Not looked at',
  pending: 'Queued',
  prepared: 'Ready',
  translated: 'Translated',
  validated: 'Checked',
  'needs-review': 'Needs review',
  failed: 'Failed',
  empty: 'Blank',
  'english-source': 'Already English',
}

export const STATUS_TONE = {
  prepared: 'var(--ok)',
  validated: 'var(--ok)',
  translated: 'var(--accent)',
  'needs-review': 'var(--warn)',
  failed: 'var(--bad)',
  empty: 'var(--hint)',
  'english-source': 'var(--hint)',
}

// What each class of chapter IS. Deliberately not the language's name: the server
// persists "source", and a label that said "Japanese" would drift the moment anyone
// pasted a chapter that was not.
export const CLASS_LABEL = {
  source: 'Source',
  english: 'English',
  empty: 'Blank',
}

export function countLabel(n, singular, plural = `${singular}s`) {
  return `${n.toLocaleString()} ${n === 1 ? singular : plural}`
}

export function percent(done, total) {
  if (!total) return 0
  return Math.max(0, Math.min(100, Math.round((done / total) * 100)))
}

export function whenish(iso) {
  if (!iso) return ''
  const then = new Date(iso)
  if (Number.isNaN(then.getTime())) return ''
  const seconds = Math.round((Date.now() - then.getTime()) / 1000)
  if (seconds < 60) return 'just now'
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`
  return then.toLocaleDateString()
}
