// Turning a translated chapter into something readable.
//
// The prompt asks for clean Markdown with exactly two inline conventions: `*...*` for
// internal thought and emphasis, and curly quotes for speech. So rather than pulling
// in a full Markdown renderer for a chapter that is 99% plain paragraphs, this handles
// those two and treats everything else as text.
//
// Deliberately NOT a general Markdown parser. A chapter is prose, and a renderer that
// interpreted `#`, `-` or `1.` at the start of a line would turn a line of dialogue
// beginning with a dash into a bullet list.

// A run of emphasis: *like this*. Requires non-space immediately inside the asterisks
// so a lone `*` used as a scene divider, or a multiplication sign, is left alone.
const EMPHASIS = /\*(\S(?:[^*]*\S)?)\*/g

// A line that is only a divider — the author's scene break. The prompt asks for these
// to be preserved exactly, so they are recognised and centred rather than rendered as
// a stray paragraph of punctuation.
const DIVIDER = /^\s*(?:[◇◆＊*※・\-–—_=]\s*){1,9}$/

export function splitParagraphs(text) {
  if (!text) return []
  return text
    .replace(/\r\n/g, '\n')
    .split(/\n\s*\n+/)
    .map((p) => p.trim())
    .filter(Boolean)
}

export function isDivider(paragraph) {
  return DIVIDER.test(paragraph || '')
}

// Split one paragraph into alternating plain / emphasised runs, so the caller can
// render them without setting innerHTML. Returns [{text, em}].
export function inlineRuns(paragraph) {
  const runs = []
  let last = 0
  const source = paragraph || ''
  EMPHASIS.lastIndex = 0
  let match = EMPHASIS.exec(source)
  while (match !== null) {
    if (match.index > last) runs.push({ text: source.slice(last, match.index), em: false })
    runs.push({ text: match[1], em: true })
    last = match.index + match[0].length
    match = EMPHASIS.exec(source)
  }
  if (last < source.length) runs.push({ text: source.slice(last), em: false })
  return runs.length ? runs : [{ text: source, em: false }]
}

// How far through a chapter the reader has scrolled, as a fraction. Used only for the
// progress bar; it is deliberately not persisted, because a reading position that
// survives a reload but not a device is more confusing than none at all.
export function scrollFraction(el) {
  if (!el) return 0
  const max = el.scrollHeight - el.clientHeight
  if (max <= 0) return 1
  return Math.max(0, Math.min(1, el.scrollTop / max))
}
