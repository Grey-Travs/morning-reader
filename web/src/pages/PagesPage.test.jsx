import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import PagesPage from './PagesPage'

// What these pin, in order of what would hurt most if it broke:
//
//  * Reading pages SPENDS the Claude plan. It must confirm first, and a declined
//    confirm must not reach the server at all.
//  * A reorder sends the whole permutation. The server refuses a partial list, so a
//    delta here would not corrupt the book — it would look like a broken button,
//    which is harder to diagnose and just as annoying.
//  * A page the reader was unsure about must be visible and must not be built from.
//  * Files the server refused must be reported. Silently dropping three photos out of
//    a folder of forty is the failure nobody notices until the chapter has a hole.

vi.mock('../api', () => ({
  api: {
    pages: vi.fn(),
    pageImageUrl: (pid, id) => `/api/projects/${pid}/pages/${id}/image`,
    uploadPages: vi.fn(),
    readPages: vi.fn(),
    reorderPages: vi.fn(),
    deletePages: vi.fn(),
    setPageJoin: vi.fn(),
    setPageStatus: vi.fn(),
    proposeJoins: vi.fn(),
    buildFromPages: vi.fn(),
    activeJob: vi.fn(),
    resume: vi.fn(),
    cancel: vi.fn(),
    streamUrl: () => '/stream',
  },
}))

const { api } = await import('../api')

function page(seq, over = {}) {
  return {
    id: `p${seq}`, seq, name: `page-${seq}.jpg`, status: 'ok',
    width: 1200, height: 1800, bytes: 1000, batch: 'b1', error: null, hint: '',
    join_prev: '', join_glue: 'none', join_prev_source: '', join_reason: '',
    // `read` is what the server's sweep keys on, and what the row now carries. A page
    // can be fully read and bill nothing to show for it — a splash or an action beat —
    // so region count is not the same question.
    read: true, regions: 3, confidence: 'high', chars: 400, ...over,
  }
}

function manifest(pages, over = {}) {
  const byStatus = {}
  for (const p of pages) byStatus[p.status] = (byStatus[p.status] || 0) + 1
  return {
    pages,
    summary: {
      total: pages.length,
      by_status: byStatus,
      ready: pages.filter((p) => ['ok', 'edited'].includes(p.status)).length,
      unmeasured: pages.filter((p) => !(p.width && p.height)).length,
      chars: pages.reduce((n, p) => n + p.chars, 0),
      totals: { cost_usd: 0 },
      build: null,
      ...over,
    },
  }
}

function show() {
  return render(
    <MemoryRouter initialEntries={['/work/abc/pages']}>
      <Routes>
        <Route path="/work/:pid/pages" element={<PagesPage />} />
      </Routes>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  // jsdom has no EventSource, and useJobStream constructs one as soon as there is a
  // job. There is no job in these tests, so it never gets that far — but the class
  // has to exist for the module to be safe to import.
  global.EventSource = class {
    constructor() { this.close = () => {} }
    close() {}
  }
  api.activeJob.mockResolvedValue({ job_id: null })
  vi.spyOn(window, 'confirm').mockReturnValue(true)
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  vi.restoreAllMocks()
})

describe('spending money', () => {
  it('asks before reading, and says how many pages that is', async () => {
    api.pages.mockResolvedValue(manifest([
      page(1, { status: 'new', read: false, regions: 0, chars: 0 }),
      page(2, { status: 'new', read: false, regions: 0, chars: 0 }),
    ]))
    api.readPages.mockResolvedValue({ queued: [1, 2], job_id: 'j1' })
    show()

    await userEvent.click(await screen.findByRole('button', { name: /Read 2 pages/ }))

    expect(window.confirm).toHaveBeenCalledOnce()
    expect(window.confirm.mock.calls[0][0]).toContain('Read 2 pages')
    expect(api.readPages).toHaveBeenCalledWith('abc', {})
  })

  it('does not reach the server when the confirm is declined', async () => {
    api.pages.mockResolvedValue(manifest([page(1, { status: 'new', read: false, regions: 0 })]))
    window.confirm.mockReturnValue(false)
    show()

    await userEvent.click(await screen.findByRole('button', { name: /Read 1 page/ }))

    expect(api.readPages).not.toHaveBeenCalled()
  })

  it('re-reads only the selected pages, and says so on the button', async () => {
    api.pages.mockResolvedValue(manifest([page(1), page(2)]))
    api.readPages.mockResolvedValue({ queued: [1], job_id: 'j1' })
    show()

    await userEvent.click(await screen.findByAltText('page-1.jpg'))
    await userEvent.click(screen.getByRole('button', { name: /Read 1 page again/ }))

    // `force`, because every one of these has been read already and a sweep would
    // skip them — selecting a page IS the instruction to pay for it again.
    expect(api.readPages).toHaveBeenCalledWith('abc', { ids: ['p1'], force: true })
  })

  it('says there is nothing to read rather than opening a dialog', async () => {
    api.pages.mockResolvedValue(manifest([page(1), page(2)]))
    show()

    await userEvent.click(await screen.findByRole('button', { name: /^Read pages$/ }))

    expect(window.confirm).not.toHaveBeenCalled()
    expect(api.readPages).not.toHaveBeenCalled()
    expect(screen.getByText(/Every page has been read already/)).toBeInTheDocument()
  })
})

describe('order', () => {
  it('sends the whole new order, not the page that moved', async () => {
    api.pages.mockResolvedValue(manifest([page(1), page(2), page(3)]))
    api.reorderPages.mockResolvedValue({ ok: true })
    show()

    await userEvent.click(await screen.findByRole('button', { name: /Move page 3 earlier/ }))

    expect(api.reorderPages).toHaveBeenCalledWith('abc', ['p1', 'p3', 'p2'])
  })

  it('moves a page later', async () => {
    api.pages.mockResolvedValue(manifest([page(1), page(2), page(3)]))
    api.reorderPages.mockResolvedValue({ ok: true })
    show()

    await userEvent.click(await screen.findByRole('button', { name: /Move page 1 later/ }))

    expect(api.reorderPages).toHaveBeenCalledWith('abc', ['p2', 'p1', 'p3'])
  })

  it('cannot move the ends off the ends', async () => {
    api.pages.mockResolvedValue(manifest([page(1), page(2)]))
    show()

    expect(await screen.findByRole('button', { name: /Move page 1 earlier/ })).toBeDisabled()
    expect(screen.getByRole('button', { name: /Move page 2 later/ })).toBeDisabled()
  })
})

describe('pages the reader was unsure about', () => {
  it('surfaces them and offers to accept one', async () => {
    api.pages.mockResolvedValue(manifest([page(1, { status: 'needs-check' })]))
    api.setPageStatus.mockResolvedValue({ ok: true })
    show()

    expect(await screen.findByText(/1 page the reader was unsure about/)).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Looks right' }))

    expect(api.setPageStatus).toHaveBeenCalledWith('abc', 'p1', 'edited')
  })

  it('will not build when nothing is ready', async () => {
    api.pages.mockResolvedValue(manifest([page(1, { status: 'needs-check' })]))
    show()

    expect(await screen.findByRole('button', { name: /Build the chapters/ })).toBeDisabled()
    expect(api.buildFromPages).not.toHaveBeenCalled()
  })

  it('builds once something is ready, and reports what came out', async () => {
    api.pages.mockResolvedValue(manifest([page(1), page(2)]))
    api.buildFromPages.mockResolvedValue({ chapters: 2, pages_used: 2, warnings: [] })
    show()

    await userEvent.click(await screen.findByRole('button', { name: /Build the chapters/ }))

    await waitFor(() =>
      expect(screen.getByText(/Built 2 chapters from 2 pages/)).toBeInTheDocument())
  })

  it('shows a build warning rather than swallowing it', async () => {
    api.pages.mockResolvedValue(manifest([page(1)]))
    api.buildFromPages.mockResolvedValue({
      chapters: 1, pages_used: 1, warnings: ['A page appears to be missing before 1.'],
    })
    show()

    await userEvent.click(await screen.findByRole('button', { name: /Build the chapters/ }))

    await waitFor(() =>
      expect(screen.getByText(/A page appears to be missing/)).toBeInTheDocument())
  })
})

describe('seams', () => {
  it('records a seam the user chose', async () => {
    api.pages.mockResolvedValue(manifest([page(1), page(2, { join_prev: 'paragraph' })]))
    api.setPageJoin.mockResolvedValue({ ok: true })
    show()

    const select = await screen.findByRole('combobox')
    await userEvent.selectOptions(select, 'sentence')

    expect(api.setPageJoin).toHaveBeenCalledWith('abc', 'p2', 'sentence')
  })

  it('marks a seam the user decided, so a re-read is not assumed to have undone it', async () => {
    api.pages.mockResolvedValue(manifest([
      page(1),
      page(2, { join_prev: 'chapter', join_prev_source: 'user' }),
    ]))
    show()

    expect(await screen.findByText('your choice')).toBeInTheDocument()
  })

  it('offers no seam for the first page, which follows nothing', async () => {
    api.pages.mockResolvedValue(manifest([page(1)]))
    show()

    await screen.findByText(/page-1\.jpg/)
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument()
  })
})

describe('uploading', () => {
  it('reports the files the server refused instead of dropping them quietly', async () => {
    api.pages.mockResolvedValue(manifest([]))
    api.uploadPages.mockResolvedValue({
      added: [{ id: 'p1', seq: 1, measured: true }],
      duplicates: [{ name: 'again.jpg', seq: 1 }],
      rejected: [{ name: 'photo.heic', reason: 'That is an HEIC photo.' }],
    })
    show()

    // Wait for the manifest: until it lands the screen is "Loading…", with no form on
    // it at all.
    await screen.findByText(/No pages yet/)
    const input = document.querySelector('input[type="file"]')
    await userEvent.upload(input, [
      new File(['x'], 'one.jpg', { type: 'image/jpeg' }),
      new File(['x'], 'again.jpg', { type: 'image/jpeg' }),
      new File(['x'], 'photo.heic', { type: 'image/heic' }),
    ])

    await waitFor(() => expect(api.uploadPages).toHaveBeenCalled())
    expect(await screen.findByText(/That is an HEIC photo/)).toBeInTheDocument()
    expect(screen.getByText(/1 already here/)).toBeInTheDocument()
  })

  it('says what to do when there are no pages at all', async () => {
    api.pages.mockResolvedValue(manifest([]))
    show()

    expect(await screen.findByText(/No pages yet/)).toBeInTheDocument()
  })
})

describe('what the grid says about a page', () => {
  it('flags a page whose size could not be read, because it cannot anchor an overlay',
    async () => {
      api.pages.mockResolvedValue(manifest([page(1, { width: 0, height: 0 })]))
      show()

      expect(await screen.findByText('size unknown')).toBeInTheDocument()
    })

  it('shows the failure on the page it belongs to', async () => {
    api.pages.mockResolvedValue(manifest([
      page(1, { status: 'failed', error: 'The model returned nothing.' }),
    ]))
    show()

    expect(await screen.findByText('The model returned nothing.')).toBeInTheDocument()
    expect(screen.getByText('Failed')).toBeInTheDocument()
  })

  it('explains a failure to load the manifest instead of showing an empty grid', async () => {
    api.pages.mockRejectedValue({
      explained: { code: 'offline', title: 'Morning Reader is not responding' },
    })
    show()

    expect(await screen.findByText('Morning Reader is not responding')).toBeInTheDocument()
  })
})
