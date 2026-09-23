import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import MangaReaderPage from './MangaReaderPage'

// What these pin, in order of what would hurt most if it broke:
//
//  * A page read left-to-right is backwards sentence by sentence and every sentence is
//    still fluent English. The banner and its one-click fix are the only thing that
//    catches it, and the fix costs nothing.
//  * A box is positioned as a PERCENTAGE of the image. Pixels, or `object-fit: cover`,
//    would drift every bubble with no error anywhere.
//  * A line whose Japanese changed must not look finished.
//  * A region with no position is REPORTED, never silently skipped — a bubble with no
//    English and no explanation reads as "the app missed this one".
//  * Reordering sends the whole permutation, including the regions that are not
//    translated, because the server requires one.

vi.mock('../api', () => ({
  api: {
    mangaChapter: vi.fn(),
    translateManga: vi.fn(),
    setPageOrder: vi.fn(),
    setLine: vi.fn(),
    glossary: vi.fn(),
  },
}))

const { api } = await import('../api')

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
    regions: [line('r0'), line('r1', { box: [0.5, 0.5, 0.3, 0.2] })],
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

function show() {
  return render(
    <MemoryRouter initialEntries={['/work/abc/manga/1']}>
      <Routes>
        <Route path="/work/:pid/manga/:index" element={<MangaReaderPage />} />
      </Routes>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  api.glossary.mockResolvedValue({ entries: [], pending: [] })
  vi.spyOn(window, 'confirm').mockReturnValue(true)
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  vi.restoreAllMocks()
})

describe('a page that reads the wrong way round', () => {
  it('says so, and offers the layout for free', async () => {
    api.mangaChapter.mockResolvedValue(chapter({
      pages: [page({
        order_check: { looks_reversed: true, disagreement: 4, order_source: 'model' },
        order_proposal: ['r1', 'r0'],
      })],
    }))
    api.setPageOrder.mockResolvedValue({ ok: true })
    show()

    expect(await screen.findByText(/looks left-to-right/)).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: /Use the layout/ }))

    expect(api.setPageOrder).toHaveBeenCalledWith('abc', 'pg1', ['r1', 'r0'])
  })

  it('stops saying so once the human has decided', async () => {
    // Nagging a page somebody has settled is what `set_join`'s user rule exists to
    // prevent, and a warning on a decided page teaches people to ignore it elsewhere.
    api.mangaChapter.mockResolvedValue(chapter({
      pages: [page({
        order_check: { looks_reversed: true, disagreement: 4, order_source: 'user' },
      })],
    }))
    show()

    await screen.findByText('第1話')
    expect(screen.queryByText(/looks left-to-right/)).not.toBeInTheDocument()
  })
})

describe('placing the English on the art', () => {
  it('positions a box as a percentage of the image', async () => {
    api.mangaChapter.mockResolvedValue(chapter())
    show()

    // By its accessible name, not its text: the English deliberately appears twice,
    // once over the art and once in the line strip beneath it.
    const overlay = await screen.findByRole('button', { name: /^Line 1/ })
    expect(overlay).toHaveStyle({ left: '10%', top: '10%', width: '30%' })
    expect(overlay).toHaveTextContent('English r0')
  })

  it('never crops or letterboxes the picture inside its element', async () => {
    // `object-fit: cover` — what the pages-screen thumbnails use — would scale the
    // picture inside the element, so percentages of the element stop being
    // percentages of the picture and every box drifts with no error anywhere.
    api.mangaChapter.mockResolvedValue(chapter())
    show()

    await screen.findByRole('button', { name: /^Line 1/ })
    expect(screen.getByAltText('page-1.jpg')).toHaveStyle({ objectFit: 'fill' })
  })

  it('reports a region it could not place rather than skipping it', async () => {
    api.mangaChapter.mockResolvedValue(chapter({
      pages: [page({
        regions: [line('r0', { box: [0, 0, 0, 0], drawable: false }), line('r1')],
      })],
    }))
    show()

    expect(await screen.findByText(/could not be placed/)).toBeInTheDocument()
  })

  it('refuses to draw an overlay on a page whose size is unknown', async () => {
    api.mangaChapter.mockResolvedValue(chapter({
      pages: [page({ width: 0, height: 0 })],
    }))
    show()

    expect(await screen.findByText(/size could not be read/)).toBeInTheDocument()
  })
})

describe('lines that are no longer true', () => {
  it('marks a line whose Japanese changed', async () => {
    api.mangaChapter.mockResolvedValue(chapter({
      pages: [page({ regions: [line('r0', { stale: true }), line('r1')] })],
      counts: { lines: 2, translated: 1, stale: 1, untranslated: 0, pages: 1,
                unchecked_pages: 0, silent_pages: 0, undrawable: 0 },
    }))
    show()

    expect(await screen.findAllByText(/the Japanese changed/)).not.toHaveLength(0)
    expect(screen.getByText(/1 out of date/)).toBeInTheDocument()
  })

  it('keeps the English on a stale line rather than hiding it', async () => {
    // Nothing in this app deletes prose. A stale line is labelled, not blanked.
    api.mangaChapter.mockResolvedValue(chapter({
      pages: [page({ regions: [line('r0', { stale: true })] })],
    }))
    show()

    expect(await screen.findAllByText('English r0')).not.toHaveLength(0)
  })

  it('says when the order changed after translating, without calling anything stale',
    async () => {
      api.mangaChapter.mockResolvedValue(chapter({ order_changed: true }))
      show()

      expect(await screen.findByText(/reading order changed after/)).toBeInTheDocument()
      // No LINE is marked — the banner's own wording contains "out of date", so the
      // assertion has to be about the per-line badge rather than about the page text.
      expect(screen.queryByText(/the Japanese changed/)).not.toBeInTheDocument()
    })
})

describe('correcting the order by hand', () => {
  it('sends the whole permutation, including regions that are not translated',
    async () => {
      // The server requires a permutation of EVERY region on the page, not just the
      // ones with English on them.
      api.mangaChapter.mockResolvedValue(chapter({
        pages: [page({
          regions: [line('r0'), line('r1'),
                    line('page-number', { translatable: false, kind: 'page-number' })],
        })],
      }))
      api.setPageOrder.mockResolvedValue({ ok: true })
      show()

      await userEvent.click(
        await screen.findByRole('button', { name: /Move line 2 earlier/ }))

      expect(api.setPageOrder).toHaveBeenCalledWith(
        'abc', 'pg1', ['r1', 'r0', 'page-number'])
    })

  it('cannot move the ends off the ends', async () => {
    api.mangaChapter.mockResolvedValue(chapter())
    show()

    expect(await screen.findByRole('button', { name: /Move line 1 earlier/ }))
      .toBeDisabled()
    expect(screen.getByRole('button', { name: /Move line 2 later/ })).toBeDisabled()
  })
})

describe('editing a line', () => {
  it('saves the English and the speaker as the human\'s', async () => {
    api.mangaChapter.mockResolvedValue(chapter())
    api.setLine.mockResolvedValue({ line: {} })
    show()

    const rows = await screen.findAllByRole('listitem')
    await userEvent.click(within(rows[0]).getByRole('button', { name: 'Edit' }))
    const english = screen.getByLabelText('English for line 1')
    await userEvent.clear(english)
    await userEvent.type(english, 'My wording.')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))

    expect(api.setLine).toHaveBeenCalledWith('abc', 'pg1', 'r0',
      { english: 'My wording.', speaker: '' })
  })

  it('shows a model speaker as a suggestion', async () => {
    api.mangaChapter.mockResolvedValue(chapter({
      pages: [page({
        regions: [line('r0', { speaker: 'Aoi', speaker_source: 'model' })],
      })],
    }))
    show()

    expect(await screen.findByText('(suggested)')).toBeInTheDocument()
  })
})

describe('spending money', () => {
  it('asks before translating a chapter', async () => {
    api.mangaChapter.mockResolvedValue(chapter())
    api.translateManga.mockResolvedValue({ queued: [1], job_id: 'j1' })
    show()

    await userEvent.click(
      await screen.findByRole('button', { name: /Translate this chapter/ }))

    expect(window.confirm).toHaveBeenCalledOnce()
    expect(api.translateManga).toHaveBeenCalledWith('abc',
      { indices: [1], force: true })
  })

  it('does not reach the server when the confirm is declined', async () => {
    api.mangaChapter.mockResolvedValue(chapter())
    window.confirm.mockReturnValue(false)
    show()

    await userEvent.click(
      await screen.findByRole('button', { name: /Translate this chapter/ }))

    expect(api.translateManga).not.toHaveBeenCalled()
  })
})

describe('what the chapter says about itself', () => {
  it('counts the pages you have not checked, rather than hiding them', async () => {
    // A manga keeps unchecked pages in the book — the art is correct and dropping one
    // is a scene nobody sees — so the count is surfaced instead.
    api.mangaChapter.mockResolvedValue(chapter({
      counts: { lines: 2, translated: 2, stale: 0, untranslated: 0, pages: 3,
                unchecked_pages: 1, silent_pages: 1, undrawable: 0 },
    }))
    show()

    expect(await screen.findByText(/1 page you have not checked/)).toBeInTheDocument()
  })

  it('says plainly when nothing is said on a page', async () => {
    api.mangaChapter.mockResolvedValue(chapter({
      pages: [page({ regions: [], panels: [] })],
    }))
    show()

    expect(await screen.findByText(/Nothing is said on this page/)).toBeInTheDocument()
  })

  it('explains a failure to load rather than showing an empty reader', async () => {
    api.mangaChapter.mockRejectedValue({
      explained: { code: 'offline', title: 'Morning Reader is not responding' },
    })
    show()

    expect(await screen.findByText('Morning Reader is not responding'))
      .toBeInTheDocument()
  })
})
