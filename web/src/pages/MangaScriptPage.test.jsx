import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import MangaScriptPage from './MangaScriptPage'

// What these pin, in order of what would hurt most if it broke:
//
//  * A model's speaker stays visibly a suggestion until a human confirms it, and only
//    a deliberate act confirms it. A wrong speaker reads perfectly well and quietly
//    makes two characters into one; if tabbing past a suggestion, or showing one,
//    counted as agreeing with it, the gate would exist on screen and nowhere else.
//  * A speaker the glossary has not approved is called out. A name only WAITING for
//    approval is not approved, and confirming who says a line does not approve the
//    name either.
//  * The script is the chapter in reading order. Lines are grouped under a panel
//    heading only while they are consecutive: grouping by panel number instead would
//    quietly move a line to somewhere else in the conversation.
//  * Each line shows its Japanese beside its English, and a line with no English, or
//    English made from different Japanese, does not look finished.
//  * Only story text is listed. A page number or a furigana reading is not a line
//    anybody says, and a page with none still appears, so a silent page is never
//    mistaken for a missing one.

vi.mock('../api', () => ({
  api: {
    mangaChapter: vi.fn(),
    setLine: vi.fn(),
    glossary: vi.fn(),
  },
}))

const { api } = await import('../api')

// The payload shapes are server/app.py's `_manga_page_payload` and
// `read_manga_chapter`, the same helpers as MangaReaderPage.test.jsx. The Japanese is
// invented.
function line(id, over = {}) {
  return {
    id, box: [0.1, 0.1, 0.3, 0.2], kind: 'bubble', order: 0,
    text: `日本語 ${id}`, panel: 1, english: `English ${id}`,
    english_source: 'model', speaker: '', speaker_source: '',
    translatable: true, drawable: true, stale: false, ...over,
  }
}

function page(over = {}) {
  return {
    id: 'pg1', seq: 1, name: 'page-1.jpg', status: 'ok',
    width: 1600, height: 2400,
    regions: [line('r0'), line('r1', { box: [0.5, 0.5, 0.3, 0.2], order: 1, panel: 2 })],
    panels: [['r0'], ['r1']],
    order_source: 'model', order_note: '',
    order_check: { looks_reversed: false, disagreement: 0, order_source: 'model' },
    order_proposal: ['r0', 'r1'],
    counts: { lines: 2, translated: 2, stale: 0, untranslated: 0, undrawable: 0 },
    ...over,
  }
}

function chapter(over = {}) {
  return {
    index: 1, title: '第1話', status: 'ok',
    pages: [page()],
    counts: { lines: 2, translated: 2, stale: 0, untranslated: 0, undrawable: 0,
              pages: 1, unchecked_pages: 0, silent_pages: 0 },
    warnings: [], order_changed: false, missing: [], cost_usd: 0.02,
    prev: null, next: null, ...over,
  }
}

function deferred() {
  let resolve
  let reject
  const promise = new Promise((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

function show() {
  return render(
    <MemoryRouter initialEntries={['/work/abc/manga/1/script']}>
      <Routes>
        <Route path="/work/:pid/manga/:index/script" element={<MangaScriptPage />} />
      </Routes>
    </MemoryRouter>,
  )
}

// One line's card, found by its speaker field — the one control every line has, and
// labelled with the region id, so a test can say which line it means.
function row(id) {
  return screen.getByRole('textbox', { name: `Speaker for ${id}` }).closest('li')
}

function idOf(item) {
  return within(item).getByRole('textbox').getAttribute('aria-label')
    .replace('Speaker for ', '')
}

// The script as a person scans it: each heading, then the lines under it, in document
// order. Built from document position rather than from the markup's nesting, so it
// says what is on screen whatever the component's structure.
function outline() {
  const headings = screen.getAllByText(/^Page \d+/)
  const items = screen.queryAllByRole('listitem')
  const nodes = [...headings, ...items].sort((a, b) =>
    (a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1))
  const groups = []
  for (const node of nodes) {
    if (headings.includes(node)) groups.push({ heading: node.textContent, lines: [] })
    else groups[groups.length - 1].lines.push(idOf(node))
  }
  return groups.map((g) => (g.lines.length ? `${g.heading}: ${g.lines.join(', ')}` : g.heading))
}

beforeEach(() => {
  api.glossary.mockResolvedValue({ entries: [], pending: [] })
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  vi.restoreAllMocks()
})

describe('the script in reading order', () => {
  it('heads each run of lines with its page and panel, one heading per run', async () => {
    // Real payloads reuse region ids on every page, so the two pages here do too.
    // Page seqs are the manifest's, not 1 and 2: a chapter that starts on page 7 must
    // not be headed "Page 1".
    api.mangaChapter.mockResolvedValue(chapter({
      pages: [
        page({
          id: 'pg7', seq: 7,
          regions: [line('r0', { panel: 1 }), line('r1', { order: 1, panel: 1 }),
                    line('r2', { order: 2, panel: 2 })],
        }),
        page({ id: 'pg8', seq: 8, regions: [line('r0', { panel: 1 })] }),
      ],
    }))
    show()

    await screen.findByText('第1話 — script')
    expect(outline()).toEqual([
      'Page 7 · panel 1: r0, r1',
      'Page 7 · panel 2: r2',
      'Page 8 · panel 1: r0',
    ])
  })

  it('keeps the reading order when a panel comes back after another one', async () => {
    // After a human reorders a page, a line can belong to a panel already passed.
    // Grouping by panel number would pull it back up under the first heading and
    // silently change who answers whom; the script must show the order as read.
    api.mangaChapter.mockResolvedValue(chapter({
      pages: [page({
        regions: [line('r3', { order: 0, panel: 1 }), line('r0', { order: 1, panel: 1 }),
                  line('r1', { order: 2, panel: 2 }), line('r2', { order: 3, panel: 1 })],
      })],
    }))
    show()

    await screen.findByText('第1話 — script')
    expect(outline()).toEqual([
      'Page 1 · panel 1: r3, r0',
      'Page 1 · panel 2: r1',
      'Page 1 · panel 1: r2',
    ])
  })

  it('lists only the story text, not page numbers or furigana', async () => {
    // A page number is not a line anybody says, and a furigana reading is a gloss on
    // a kanji rather than a line of its own. Listing them would ask for a speaker for
    // each and make every page look unfinished.
    api.mangaChapter.mockResolvedValue(chapter({
      pages: [page({
        regions: [
          line('r0', { text: '雨がやんだ。' }),
          line('f0', { kind: 'furigana', text: 'あおい', translatable: false,
                       english: '', english_source: '' }),
          line('pn', { kind: 'page-number', text: '１２', translatable: false,
                       english: '', english_source: '' }),
        ],
      })],
    }))
    show()

    await screen.findByText('雨がやんだ。')
    expect(screen.getAllByRole('listitem')).toHaveLength(1)
    expect(screen.queryByText('あおい')).not.toBeInTheDocument()
    expect(screen.queryByText('１２')).not.toBeInTheDocument()
  })

  it('says plainly when nothing is said on a page, rather than leaving it out', async () => {
    // A page that simply vanished from the script would read as a page the app lost.
    // A page whose only text is its page number is still a page with nothing said.
    api.mangaChapter.mockResolvedValue(chapter({
      pages: [
        page({ id: 'pg7', seq: 7 }),
        page({ id: 'pg8', seq: 8,
               regions: [line('pn', { kind: 'page-number', text: '８',
                                      translatable: false, english: '' })] }),
        page({ id: 'pg9', seq: 9, regions: [], panels: [] }),
      ],
    }))
    show()

    await screen.findByText('第1話 — script')
    expect(outline()).toEqual([
      'Page 7 · panel 1: r0',
      'Page 7 · panel 2: r1',
      'Page 8',
      'Page 9',
    ])
    expect(screen.getAllByText('Nothing is said on this page.')).toHaveLength(2)
  })

  // A translatable region the reader returned with no text is in no numbered panel:
  // the server sends `panel: 0` (manga.panel_numbers) and the translator never sees
  // the line. The script used to invent `group index + 1` for it — here "panel 2", the
  // number of a DIFFERENT, real panel — so panel 2 appeared twice and a line was
  // claimed for a panel it is not in.
  it('does not invent a panel number for a line that is in no panel', async () => {
    api.mangaChapter.mockResolvedValue(chapter({
      pages: [page({
        regions: [line('r0', { panel: 1, text: '待って！' }),
                  line('r1', { order: 1, panel: 0, text: '', english: '' }),
                  line('r2', { order: 2, panel: 2, text: 'どこへ行くの？' })],
      })],
    }))
    show()

    await screen.findByText('第1話 — script')
    const group = outline().find((g) => /: (.*, )?r1(,|$)/.test(g))
    expect(group).toBeDefined()
    expect(group).not.toMatch(/panel \d/)
  })
})

describe('each line', () => {
  it('shows the Japanese beside the English, on the same line', async () => {
    api.mangaChapter.mockResolvedValue(chapter({
      pages: [page({
        regions: [line('r0', { text: '雨がやんだ。', english: 'The rain stopped.' }),
                  line('r1', { order: 1, panel: 2, text: '明日も来るよ。',
                               english: "I'll come tomorrow too." })],
      })],
    }))
    show()

    await screen.findByText('第1話 — script')
    expect(within(row('r0')).getByText('雨がやんだ。')).toBeInTheDocument()
    expect(within(row('r0')).getByText('The rain stopped.')).toBeInTheDocument()
    expect(within(row('r1')).getByText('明日も来るよ。')).toBeInTheDocument()
    expect(within(row('r1')).getByText("I'll come tomorrow too.")).toBeInTheDocument()
  })

  it('says a line is not translated yet rather than leaving it blank', async () => {
    // A blank English column reads as a line with nothing to translate.
    api.mangaChapter.mockResolvedValue(chapter({
      pages: [page({
        regions: [line('r0', { english: '', english_source: '' }),
                  line('r1', { order: 1, panel: 2 })],
      })],
    }))
    show()

    await screen.findByText('第1話 — script')
    expect(within(row('r0')).getByText('not translated yet')).toBeInTheDocument()
    expect(within(row('r1')).queryByText('not translated yet')).not.toBeInTheDocument()
  })

  it('marks a line whose Japanese changed since it was translated', async () => {
    // Stale English is real and paid for, so it is shown — but it was made from
    // different words, and must not look finished.
    api.mangaChapter.mockResolvedValue(chapter({
      pages: [page({
        regions: [line('r0', { stale: true }), line('r1', { order: 1, panel: 2 })],
      })],
      counts: { lines: 2, translated: 1, stale: 1, untranslated: 0, undrawable: 0,
                pages: 1, unchecked_pages: 0, silent_pages: 0 },
    }))
    show()

    await screen.findByText('第1話 — script')
    expect(within(row('r0')).getByText('the Japanese changed')).toBeInTheDocument()
    expect(within(row('r1')).queryByText('the Japanese changed')).not.toBeInTheDocument()
    expect(screen.getByText(/1 out of date/)).toBeInTheDocument()
  })
})

describe('who says it', () => {
  it('shows a model speaker as a suggestion, with a way to confirm it', async () => {
    api.mangaChapter.mockResolvedValue(chapter({
      pages: [page({
        regions: [line('r0', { speaker: 'Aoi', speaker_source: 'model' })],
      })],
    }))
    show()

    await screen.findByText('第1話 — script')
    const card = row('r0')
    expect(within(card).getByRole('textbox')).toHaveValue('Aoi')
    expect(within(card).getByText('suggested')).toBeInTheDocument()
    expect(within(card).getByRole('button', { name: 'Confirm' })).toBeEnabled()
    expect(within(card).queryByText('confirmed')).not.toBeInTheDocument()
  })

  it('confirms a suggestion as the human\'s, then shows it confirmed', async () => {
    const suggested = chapter({
      pages: [page({ regions: [line('r0', { speaker: 'Aoi', speaker_source: 'model' })] })],
    })
    const confirmed = chapter({
      pages: [page({ regions: [line('r0', { speaker: 'Aoi', speaker_source: 'user' })] })],
    })
    api.mangaChapter.mockResolvedValueOnce(suggested).mockResolvedValueOnce(confirmed)
    api.setLine.mockResolvedValue({ line: { speaker: 'Aoi', speaker_source: 'user' } })
    show()

    await screen.findByText('第1話 — script')
    await userEvent.click(within(row('r0')).getByRole('button', { name: 'Confirm' }))

    // The page id and region id the server looks the line up by, and the speaker as
    // it stands — the server marks whatever arrives here as the human's.
    expect(api.setLine).toHaveBeenCalledOnce()
    expect(api.setLine).toHaveBeenCalledWith('abc', 'pg1', 'r0', { speaker: 'Aoi' })
    expect(await within(row('r0')).findByText('confirmed')).toBeInTheDocument()
    expect(within(row('r0')).queryByText('suggested')).not.toBeInTheDocument()
    expect(within(row('r0')).queryByRole('button', { name: 'Confirm' }))
      .not.toBeInTheDocument()
  })

  it('shows a confirmed speaker as confirmed, with nothing left to confirm', async () => {
    api.mangaChapter.mockResolvedValue(chapter({
      pages: [page({
        regions: [line('r0', { speaker: 'Aoi', speaker_source: 'user' })],
      })],
    }))
    show()

    await screen.findByText('第1話 — script')
    const card = row('r0')
    expect(within(card).getByText('confirmed')).toBeInTheDocument()
    expect(within(card).queryByText('suggested')).not.toBeInTheDocument()
    expect(within(card).queryByRole('button', { name: 'Confirm' })).not.toBeInTheDocument()
  })

  it('does not confirm a suggestion just because the field was tabbed through', async () => {
    // Leaving the field unchanged is not agreeing with it. Saving on every blur would
    // send the model's name back marked as the human's — the gate, bypassed by the
    // Tab key.
    api.mangaChapter.mockResolvedValue(chapter({
      pages: [page({
        regions: [line('r0', { speaker: 'Aoi', speaker_source: 'model' })],
      })],
    }))
    show()

    await screen.findByText('第1話 — script')
    await userEvent.click(screen.getByRole('textbox', { name: 'Speaker for r0' }))
    await userEvent.tab()

    expect(api.setLine).not.toHaveBeenCalled()
  })

  it('saves a speaker typed by hand once, when the field is left', async () => {
    // Once, not per keystroke: every save reloads the chapter, and "R", "Re" would
    // each be recorded as the human's answer on the way to "Ren".
    api.mangaChapter.mockResolvedValue(chapter({
      pages: [page({ regions: [line('r0')] })],
    }))
    api.setLine.mockResolvedValue({ line: {} })
    show()

    await screen.findByText('第1話 — script')
    await userEvent.type(screen.getByRole('textbox', { name: 'Speaker for r0' }), 'Ren')
    expect(api.setLine).not.toHaveBeenCalled()
    await userEvent.tab()

    expect(api.setLine).toHaveBeenCalledOnce()
    expect(api.setLine).toHaveBeenCalledWith('abc', 'pg1', 'r0', { speaker: 'Ren' })
  })

  it('will not send a second confirm while the first is still saving', async () => {
    const saving = deferred()
    api.mangaChapter.mockResolvedValue(chapter({
      pages: [page({
        regions: [line('r0', { speaker: 'Aoi', speaker_source: 'model' }),
                  line('r1', { order: 1, panel: 2, speaker: 'Ren', speaker_source: 'model' })],
      })],
    }))
    api.setLine.mockReturnValue(saving.promise)
    show()

    await screen.findByText('第1話 — script')
    await userEvent.click(within(row('r0')).getByRole('button', { name: 'Confirm' }))

    expect(within(row('r1')).getByRole('button', { name: 'Confirm' })).toBeDisabled()
    saving.resolve({ line: {} })
    await waitFor(() =>
      expect(within(row('r1')).getByRole('button', { name: 'Confirm' })).toBeEnabled())
    expect(api.setLine).toHaveBeenCalledOnce()
  })
})

describe('names nobody has approved', () => {
  it('calls out a speaker who is not in the approved glossary', async () => {
    // Approved means in `entries`, matched on the English. A name still waiting in
    // `pending` is not approved yet, and confirming who says a line is a different
    // decision from approving the name — so a confirmed stranger is still called out.
    api.glossary.mockResolvedValue({
      entries: [{ source: '葵', english: 'Aoi', type: 'name', variants: [] }],
      pending: [{ source: '蓮', english: 'Ren', type: 'name' }],
    })
    api.mangaChapter.mockResolvedValue(chapter({
      pages: [page({
        regions: [
          line('r0', { speaker: 'Aoi', speaker_source: 'model' }),
          line('r1', { order: 1, panel: 2, speaker: 'Ren', speaker_source: 'model' }),
          line('r2', { order: 2, panel: 2, speaker: 'Ren', speaker_source: 'user' }),
          line('r3', { order: 3, panel: 2 }),
        ],
      })],
    }))
    show()

    await screen.findByText('第1話 — script')
    // Until the glossary arrives every name is unknown, so wait for the approved one
    // to clear rather than reading the first render.
    await waitFor(() => expect(within(row('r0'))
      .queryByText('not a name you have approved')).not.toBeInTheDocument())
    expect(within(row('r1')).getByText('not a name you have approved')).toBeInTheDocument()
    expect(within(row('r2')).getByText('not a name you have approved')).toBeInTheDocument()
    // No speaker is not an unknown name. Calling out every unattributed line would
    // bury the ones that matter.
    expect(within(row('r3')).queryByText('not a name you have approved'))
      .not.toBeInTheDocument()
    expect(api.glossary).toHaveBeenCalledWith('abc')
  })

  it('still shows the script when the glossary cannot be read, and trusts no name', async () => {
    // Failing closed: with no glossary to check against, every name is called out.
    // Failing open would present a name nobody approved as though somebody had.
    api.glossary.mockRejectedValue({
      explained: { code: 'offline', title: 'Morning Reader is not responding' },
    })
    api.mangaChapter.mockResolvedValue(chapter({
      pages: [page({
        regions: [line('r0', { speaker: 'Aoi', speaker_source: 'user' })],
      })],
    }))
    show()

    expect(await within(await screen.findByRole('listitem'))
      .findByText('not a name you have approved')).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })
})

describe('what the chapter says about itself', () => {
  it('names the chapter, counts its lines and links back to the reader', async () => {
    api.mangaChapter.mockResolvedValue(chapter({
      counts: { lines: 3, translated: 2, stale: 0, untranslated: 1, undrawable: 0,
                pages: 1, unchecked_pages: 0, silent_pages: 0 },
    }))
    show()

    expect(await screen.findByText('第1話 — script')).toBeInTheDocument()
    expect(screen.getByText(/3 lines · 2 translated/)).toBeInTheDocument()
    expect(screen.queryByText(/out of date/)).not.toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Back to the reader' }))
      .toHaveAttribute('href', '/work/abc/manga/1')
  })

  it('says it is loading until the chapter arrives', async () => {
    const loading = deferred()
    api.mangaChapter.mockReturnValue(loading.promise)
    show()

    expect(screen.getByText('Loading…')).toBeInTheDocument()
    loading.resolve(chapter())
    expect(await screen.findByText('第1話 — script')).toBeInTheDocument()
    expect(screen.queryByText('Loading…')).not.toBeInTheDocument()
  })

  it('explains a failure to load rather than loading forever', async () => {
    api.mangaChapter.mockRejectedValue({
      explained: { code: 'not-found',
                   title: 'There is no chapter with that number in this manga.' },
    })
    show()

    expect(await screen.findByRole('alert'))
      .toHaveTextContent('There is no chapter with that number in this manga.')
    expect(screen.queryByText('Loading…')).not.toBeInTheDocument()
  })

  it('loads again from the explanation when the failure can be retried', async () => {
    api.mangaChapter
      .mockRejectedValueOnce({
        explained: { code: 'offline', title: 'Morning Reader is not responding',
                     retryable: true },
      })
      .mockResolvedValueOnce(chapter())
    show()

    await userEvent.click(await screen.findByRole('button', { name: 'Try again' }))

    expect(await screen.findByText('第1話 — script')).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('keeps the script on screen when a save fails, and explains it above', async () => {
    // Replacing the whole script with the error would throw away the reader's place
    // for a failure that concerns one line.
    api.mangaChapter.mockResolvedValue(chapter({
      pages: [page({ regions: [line('r0', { speaker: 'Aoi', speaker_source: 'model' })] })],
    }))
    api.setLine.mockRejectedValue({
      explained: { code: 'not-found',
                   title: 'There is no region with that id on this page.' },
    })
    show()

    await screen.findByText('第1話 — script')
    await userEvent.click(within(row('r0')).getByRole('button', { name: 'Confirm' }))

    expect(await screen.findByRole('alert'))
      .toHaveTextContent('There is no region with that id on this page.')
    expect(row('r0')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Dismiss' }))
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })
})
