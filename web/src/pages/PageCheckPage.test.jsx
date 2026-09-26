import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import PageCheckPage from './PageCheckPage'

// What these pin, in order of what would hurt most if it broke:
//
//  * A correction is SAVED — after the pause, and also when the page is left inside
//    the pause. Night Reader lost corrections typed and then navigated away from,
//    silently, with the typo still there on return.
//  * It is saved against the page it was typed on, never the one navigated to.
//  * Reading again spends the plan and replaces corrections. It asks first, says so,
//    and a declined confirm reaches nothing.
//  * Nothing can be typed while the page is being read: the read would replace it.
//  * WHY the reader was unsure is on screen — the thing there was to check against.

vi.mock('../api', () => ({
  api: {
    page: vi.fn(),
    pageImageUrl: (pid, id) => `/api/projects/${pid}/pages/${id}/image`,
    correctRegion: vi.fn(),
    setPageStatus: vi.fn(),
    readPages: vi.fn(),
  },
}))

const { api } = await import('../api')

const FIRST = '電車はまだ来ない。'
const MISREAD = '彼女はホームに立つていた。'
const FIXED = '彼女はホームに立っていた。'

function region(id, text, over = {}) {
  return { id, kind: 'body', order: 0, box: [0.1, 0.1, 0.8, 0.2], text,
           original: text, corrected: false, ...over }
}

function payload(over = {}, page = {}) {
  return {
    page: { id: 'p1', seq: 1, name: 'page-1.jpg', status: 'needs-check', hint: '',
            error: null, ocr: { confidence: 'medium' }, read: { regions: [] }, ...page },
    kind: 'novel',
    text: '',
    regions: [region('r1', FIRST), region('r0', MISREAD),
              region('r2', '—26—', { kind: 'page-number' })],
    notes: ['the bottom line is faint'],
    heading: null,
    note: '',
    position: { index: 0, total: 2, prev: null, next: 'p2' },
    built_at: null,
    corrected_at: null,
    ...over,
  }
}

function show(path = '/work/abc/pages/p1') {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/work/:pid/pages/:pageId" element={<PageCheckPage />} />
        <Route path="/work/:pid/pages" element={<p>the pages screen</p>} />
      </Routes>
    </MemoryRouter>,
  )
}

function saved(regionId, text) {
  return { region: { id: regionId, text, original: MISREAD, corrected: text !== MISREAD },
           status: 'edited', note: '', text: '', corrections: 1 }
}

beforeEach(() => {
  api.page.mockResolvedValue(payload())
  api.correctRegion.mockImplementation((pid, pageId, regionId, text) =>
    Promise.resolve(saved(regionId, text)))
  api.setPageStatus.mockResolvedValue({ status: 'edited' })
  api.readPages.mockResolvedValue({ job_id: 'j1', queued: [1] })
  vi.spyOn(window, 'confirm').mockReturnValue(true)
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  vi.restoreAllMocks()
})

describe('what the page shows', () => {
  it('lists what was read, in reading order, beside the photograph', async () => {
    show()

    const boxes = await screen.findAllByRole('textbox', { name: /Region/ })
    expect(boxes.map((b) => b.value)).toEqual([FIRST, MISREAD, '—26—'])
    expect(screen.getByAltText('Page 1')).toHaveAttribute(
      'src', '/api/projects/abc/pages/p1/image')
    expect(screen.getByText('not part of the text')).toBeInTheDocument()
  })

  it('says why the reader was unsure', async () => {
    show()

    expect(await screen.findByText(/the bottom line is faint/)).toBeInTheDocument()
  })

  it('shows why the last read failed', async () => {
    api.page.mockResolvedValue(payload({}, { status: 'failed', error: 'RuntimeError: boom' }))
    show()

    expect(await screen.findByText('RuntimeError: boom')).toBeInTheDocument()
  })

  it('says plainly when a page has not been read', async () => {
    api.page.mockResolvedValue(payload({ regions: [], notes: [] },
      { status: 'new', read: null }))
    show()

    expect(await screen.findByText(/has not been read yet/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Looks right' })).not.toBeInTheDocument()
  })
})

describe('correcting', () => {
  it('saves after the pause, once, with what was finally typed', async () => {
    const user = userEvent.setup()
    show()
    const box = (await screen.findAllByRole('textbox', { name: /Region/ }))[1]

    await user.clear(box)
    await user.type(box, FIXED)
    expect(api.correctRegion).not.toHaveBeenCalled()

    await waitFor(() => expect(api.correctRegion).toHaveBeenCalledTimes(1),
      { timeout: 2000 })
    expect(api.correctRegion).toHaveBeenCalledWith('abc', 'p1', 'r0', FIXED)
    expect(await screen.findByText('your correction')).toBeInTheDocument()
  })

  it('saves a correction typed just before moving to the next page, against its own page',
    async () => {
      api.page.mockImplementation((pid, id) => Promise.resolve(id === 'p1'
        ? payload()
        : payload({ position: { index: 1, total: 2, prev: 'p1', next: null } },
          { id: 'p2', seq: 2 })))
      const user = userEvent.setup()
      show()
      const box = (await screen.findAllByRole('textbox', { name: /Region/ }))[1]

      await user.type(box, 'X')
      await user.click(screen.getByRole('link', { name: 'Next page' }))

      await waitFor(() => expect(api.correctRegion).toHaveBeenCalledWith(
        'abc', 'p1', 'r0', `${MISREAD}X`))
      expect(api.correctRegion).not.toHaveBeenCalledWith(
        'abc', 'p2', expect.anything(), expect.anything())
      expect(await screen.findByText(/Page 2/)).toBeInTheDocument()
    })

  it('puts the reader’s version back on request', async () => {
    api.page.mockResolvedValue(payload({
      regions: [region('r0', FIXED, { original: MISREAD, corrected: true })] }))
    const user = userEvent.setup()
    show()

    await user.click(await screen.findByRole('button', { name: /reader.s version/ }))

    expect(api.correctRegion).toHaveBeenCalledWith('abc', 'p1', 'r0', MISREAD)
  })

  it('cannot be typed into while the page is being read, and looks again until it is done',
    async () => {
      api.page
        .mockResolvedValueOnce(payload({ in_flight: true }, { status: 'reading' }))
        .mockResolvedValue(payload({ in_flight: false }, { status: 'ok' }))
      show()

      const boxes = await screen.findAllByRole('textbox', { name: /Region/ })
      expect(boxes[0]).toBeDisabled()
      expect(screen.getByText(/being read/)).toBeInTheDocument()

      await waitFor(() => expect(screen.getAllByRole('textbox', { name: /Region/ })[0])
        .toBeEnabled(), { timeout: 3000 })
    })

  it('locks a page waiting out a usage limit, whatever its status says', async () => {
    // The read is put back to wait for the limit and the page rests at its old status
    // for hours. Judged by status, the boxes opened and the read later replaced what
    // was typed.
    api.page.mockResolvedValue(payload({ in_flight: true }, { status: 'needs-check' }))
    show()

    expect((await screen.findAllByRole('textbox', { name: /Region/ }))[0]).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Read again' })).toBeDisabled()
  })

  it('keeps looking after one failed look, rather than staying locked for good',
    async () => {
      api.page
        .mockResolvedValueOnce(payload({ in_flight: true }, { status: 'reading' }))
        .mockRejectedValueOnce(new Error('the connection dropped'))
        .mockResolvedValue(payload({ in_flight: false }, { status: 'ok' }))
      show()

      await waitFor(() => expect(screen.getAllByRole('textbox', { name: /Region/ })[0])
        .toBeEnabled(), { timeout: 5000 })
    })

  it('does not lock a page a dead run left marked "reading"', async () => {
    api.page.mockResolvedValue(payload({ in_flight: false }, { status: 'reading' }))
    show()

    expect((await screen.findAllByRole('textbox', { name: /Region/ }))[0]).toBeEnabled()
  })

  it('cannot be typed into on a page marked not text', async () => {
    api.page.mockResolvedValue(payload({}, { status: 'skipped' }))
    show()

    expect((await screen.findAllByRole('textbox', { name: /Region/ }))[0]).toBeDisabled()
    expect(screen.getByText(/Put it back to correct it/)).toBeInTheDocument()
  })

  it('never shows the page it just left under the next page’s address', async () => {
    // "Looks right" on page 1, then the arrow before it answered: the reload of page 1
    // landed after page 2 was showing, and typing there saved page 1's words onto
    // page 2 and approved it.
    let finishStatus
    api.setPageStatus.mockReturnValue(new Promise((resolve) => { finishStatus = resolve }))
    api.page.mockImplementation((pid, id) => Promise.resolve(id === 'p1'
      ? payload()
      : payload({ regions: [region('r0', '二ページ目')],
                  position: { index: 1, total: 2, prev: 'p1', next: null } },
                { id: 'p2', seq: 2 })))
    const user = userEvent.setup()
    show()

    await user.click(await screen.findByRole('button', { name: 'Looks right' }))
    await user.click(screen.getByRole('link', { name: 'Next page' }))
    expect(await screen.findByDisplayValue('二ページ目')).toBeInTheDocument()
    finishStatus({ status: 'edited' })

    await new Promise((r) => setTimeout(r, 50))
    expect(screen.getByDisplayValue('二ページ目')).toBeInTheDocument()
    expect(screen.queryByDisplayValue(FIRST)).not.toBeInTheDocument()
  })

  it('counts a correction still waiting to save before reading again', async () => {
    const user = userEvent.setup()
    show()
    const box = (await screen.findAllByRole('textbox', { name: /Region/ }))[1]

    await user.type(box, 'X')
    await user.click(screen.getByRole('button', { name: 'Read again' }))

    expect(window.confirm).toHaveBeenCalledWith(
      expect.stringMatching(/1 correction on this page will be replaced/))
  })

  it('says a built novel needs building again to use a correction', async () => {
    api.page.mockResolvedValue(payload({
      regions: [region('r0', FIXED, { original: MISREAD, corrected: true })],
      built_at: '2026-09-01T00:00:00+00:00', corrected_at: '2026-09-02T00:00:00+00:00',
    }))
    show()

    expect(await screen.findByText(/Build them again/)).toBeInTheDocument()
  })
})

describe('deciding about the page', () => {
  it('marks it checked', async () => {
    const user = userEvent.setup()
    show()

    await user.click(await screen.findByRole('button', { name: 'Looks right' }))

    expect(api.setPageStatus).toHaveBeenCalledWith('abc', 'p1', 'edited')
  })

  it('reads it again with the note, after asking', async () => {
    const user = userEvent.setup()
    show()

    await user.type(await screen.findByPlaceholderText(/cut off/), '下の二行が切れている')
    await user.click(screen.getByRole('button', { name: 'Read again' }))

    expect(window.confirm).toHaveBeenCalledWith(expect.stringMatching(/Claude plan/))
    expect(api.readPages).toHaveBeenCalledWith('abc', {
      ids: ['p1'], force: true, hint: '下の二行が切れている' })
  })

  it('warns that reading again replaces corrections', async () => {
    api.page.mockResolvedValue(payload({
      regions: [region('r0', FIXED, { original: MISREAD, corrected: true })] }))
    const user = userEvent.setup()
    show()

    await user.click(await screen.findByRole('button', { name: 'Read again' }))

    expect(window.confirm).toHaveBeenCalledWith(
      expect.stringMatching(/1 correction on this page will be replaced/))
  })

  it('reaches nothing when the confirm is declined', async () => {
    window.confirm.mockReturnValue(false)
    const user = userEvent.setup()
    show()

    await user.click(await screen.findByRole('button', { name: 'Read again' }))

    expect(api.readPages).not.toHaveBeenCalled()
  })

  it('saves a waiting correction before reading again', async () => {
    const user = userEvent.setup()
    show()
    const box = (await screen.findAllByRole('textbox', { name: /Region/ }))[1]

    await user.type(box, 'X')
    await user.click(screen.getByRole('button', { name: 'Read again' }))

    await waitFor(() => expect(api.readPages).toHaveBeenCalled())
    expect(api.correctRegion.mock.invocationCallOrder[0])
      .toBeLessThan(api.readPages.mock.invocationCallOrder[0])
  })
})
