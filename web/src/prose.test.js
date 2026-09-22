import { describe, expect, it } from 'vitest'
import { inlineRuns, isDivider, splitParagraphs } from './prose'

// The reader renders a chapter with a deliberately tiny subset of Markdown: emphasis
// and scene dividers, nothing else. These pin the "nothing else" — a general Markdown
// renderer turns a line of dialogue starting with a dash into a bullet list, and a
// chapter is prose, not a document.

describe('splitParagraphs', () => {
  it('splits on blank lines', () => {
    expect(splitParagraphs('One.\n\nTwo.\n\nThree.')).toEqual(['One.', 'Two.', 'Three.'])
  })

  it('keeps a single newline inside a paragraph', () => {
    expect(splitParagraphs('One line\nstill the same paragraph.'))
      .toEqual(['One line\nstill the same paragraph.'])
  })

  it('tolerates windows line endings and runs of blank lines', () => {
    expect(splitParagraphs('One.\r\n\r\n\r\nTwo.')).toEqual(['One.', 'Two.'])
  })

  it('returns nothing for nothing', () => {
    expect(splitParagraphs('')).toEqual([])
    expect(splitParagraphs(null)).toEqual([])
    expect(splitParagraphs('   \n\n  ')).toEqual([])
  })
})

describe('inlineRuns', () => {
  it('marks *emphasis* without touching the rest', () => {
    expect(inlineRuns('She thought *this cannot be right* and turned away.')).toEqual([
      { text: 'She thought ', em: false },
      { text: 'this cannot be right', em: true },
      { text: ' and turned away.', em: false },
    ])
  })

  it('handles several runs in one paragraph', () => {
    const runs = inlineRuns('*One* plain *two*')
    expect(runs.filter((r) => r.em).map((r) => r.text)).toEqual(['One', 'two'])
  })

  it('leaves a lone asterisk alone', () => {
    // A scene divider, or a footnote marker. Treating it as an unterminated emphasis
    // would swallow the rest of the chapter.
    expect(inlineRuns('A line with * in it')).toEqual([
      { text: 'A line with * in it', em: false },
    ])
  })

  it('does not treat spaced asterisks as emphasis', () => {
    expect(inlineRuns('two * three * four').some((r) => r.em)).toBe(false)
  })

  it('returns the paragraph unchanged when there is no emphasis', () => {
    expect(inlineRuns('Plain prose.')).toEqual([{ text: 'Plain prose.', em: false }])
  })

  it('never drops text', () => {
    // The property that matters: whatever the markup, every character survives.
    for (const input of ['*a* b *c*', 'a * b', '*only*', 'plain', '*', '**']) {
      const rebuilt = inlineRuns(input).map((r) => (r.em ? `*${r.text}*` : r.text)).join('')
      expect(rebuilt).toBe(input)
    }
  })
})

describe('isDivider', () => {
  it('recognises the scene breaks a Japanese source uses', () => {
    for (const line of ['◇', '◇ ◇ ◇', '＊', '※', '・ ・ ・', '---', '* * *']) {
      expect(isDivider(line), line).toBe(true)
    }
  })

  it('does not mistake prose for a divider', () => {
    for (const line of ['She waited.', '— he said', 'A dash - in prose', '1']) {
      expect(isDivider(line), line).toBe(false)
    }
  })

  it('does not mistake a long rule of dashes for prose', () => {
    expect(isDivider('—')).toBe(true)
  })
})
