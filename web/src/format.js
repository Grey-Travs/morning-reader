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

// The same for a scanned page. Separate from the chapter statuses above because they
// are a different key space entirely — `ok` means "read and the model was confident",
// which no chapter is ever in. Kept in this file so the grid and the overlay reader
// (step 4) cannot drift apart. Mirrors server/pages.py.
export const PAGE_STATUS_LABEL = {
  new: 'Not read',
  queued: 'Queued',
  reading: 'Reading…',
  ok: 'Read',
  'needs-check': 'Check this',
  edited: 'Checked',
  skipped: 'Not part of the text',
  failed: 'Failed',
}

export const PAGE_STATUS_TONE = {
  ok: 'var(--ok)',
  edited: 'var(--ok)',
  reading: 'var(--accent)',
  'needs-check': 'var(--warn)',
  failed: 'var(--bad)',
  skipped: 'var(--hint)',
}

// A manga chapter's translation lifecycle. Separate from both the prose chapter
// statuses and the page ones, because it is a third key space: a manga chapter is a run
// of pages and "translating" is not "reading".
export const MANGA_CHAPTER_STATUS_LABEL = {
  '': 'Not translated',
  queued: 'Queued',
  translating: 'Translating...',
  ok: 'Translated',
  'needs-check': 'Partly translated',
  failed: 'Failed',
}

export const MANGA_CHAPTER_STATUS_TONE = {
  ok: 'var(--ok)',
  translating: 'var(--accent)',
  'needs-check': 'var(--warn)',
  failed: 'var(--bad)',
}

// How a page can follow the one before it, in the user's words. The four seam kinds
// from morning/pageread.py — one level up from the regions, which use the same names.
export const JOIN_LABEL = {
  sentence: 'same sentence',
  paragraph: 'new paragraph',
  chapter: 'new chapter',
  gap: 'a page is missing',
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
