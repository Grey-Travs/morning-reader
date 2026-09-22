import { describe, expect, it } from 'vitest'
import { CLASS_LABEL, STATUS_LABEL, countLabel, percent, whenish } from './format'

// These labels are the interface's half of a contract the server persists. If the two
// drift, a chapter shows a status nobody can explain — so the mapping is pinned.

describe('status labels', () => {
  it('names every status the server can store', () => {
    // Mirrors morning/state.py's STATUSES, plus the empty "never looked at" case.
    const fromServer = ['', 'pending', 'prepared', 'translated', 'validated',
      'needs-review', 'failed', 'empty', 'english-source']
    for (const status of fromServer) {
      expect(STATUS_LABEL[status], `no label for ${status || '(empty)'}`).toBeTruthy()
    }
  })

  it('does not name the source language anywhere', () => {
    // The whole reason this app is separate. A label reading "Japanese" would drift
    // the moment someone pasted a chapter that was not.
    const labels = [...Object.values(STATUS_LABEL), ...Object.values(CLASS_LABEL)]
    for (const label of labels) {
      expect(label.toLowerCase()).not.toContain('japanese')
      expect(label.toLowerCase()).not.toContain('korean')
    }
  })

  it('labels the three classifications the server sends', () => {
    expect(Object.keys(CLASS_LABEL).sort()).toEqual(['empty', 'english', 'source'])
  })
})

describe('countLabel', () => {
  it('agrees with itself about singular and plural', () => {
    expect(countLabel(1, 'chapter')).toBe('1 chapter')
    expect(countLabel(2, 'chapter')).toBe('2 chapters')
    expect(countLabel(0, 'chapter')).toBe('0 chapters')
  })

  it('takes an explicit plural when the regular one would be wrong', () => {
    expect(countLabel(2, 'entry', 'entries')).toBe('2 entries')
  })

  it('groups large numbers so a long novel reads as a number', () => {
    expect(countLabel(3967, 'chapter')).toMatch(/3[,.  ]?967/)
  })
})

describe('percent', () => {
  it('reports plain progress', () => {
    expect(percent(0, 10)).toBe(0)
    expect(percent(5, 10)).toBe(50)
    expect(percent(10, 10)).toBe(100)
  })

  it('never divides by zero', () => {
    // An item with no units yet is the normal state for the first frame of a run.
    expect(percent(0, 0)).toBe(0)
    expect(percent(3, 0)).toBe(0)
  })

  it('clamps nonsense rather than drawing a bar past the end', () => {
    expect(percent(20, 10)).toBe(100)
    expect(percent(-5, 10)).toBe(0)
  })
})

describe('whenish', () => {
  it('says nothing when there is nothing to say', () => {
    expect(whenish(null)).toBe('')
    expect(whenish('')).toBe('')
    expect(whenish('not a date')).toBe('')
  })

  it('describes recent times relatively', () => {
    expect(whenish(new Date().toISOString())).toBe('just now')
    expect(whenish(new Date(Date.now() - 5 * 60 * 1000).toISOString())).toBe('5m ago')
    expect(whenish(new Date(Date.now() - 3 * 3600 * 1000).toISOString())).toBe('3h ago')
  })
})
