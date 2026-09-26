import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import LibraryPage from './LibraryPage'

// What these pin: a manga can only be added from its pictures. Made from pasted text
// or a Doc it would be a work with no pages, which every manga screen needs and every
// prose screen refuses — nothing could translate or read it, and nothing would say
// why. The server refuses the pairing too; this is the form not offering it.

vi.mock('../api', () => ({
  api: {
    listProjects: vi.fn(),
    createFromText: vi.fn(),
    uploadTxt: vi.fn(),
    createFromDoc: vi.fn(),
    createFromScans: vi.fn(),
    googleStatus: vi.fn(),
    googleConnect: vi.fn(),
  },
}))

const { api } = await import('../api')

function show() {
  return render(
    <MemoryRouter initialEntries={['/']}>
      <Routes>
        <Route path="/" element={<LibraryPage />} />
        <Route path="/work/:pid/pages" element={<p>the pages screen</p>} />
        <Route path="/work/:pid" element={<p>the work screen</p>} />
      </Routes>
    </MemoryRouter>,
  )
}

async function openTheForm(user) {
  show()
  await user.click(await screen.findByRole('button', { name: 'Add a work' }))
}

beforeEach(() => {
  api.listProjects.mockResolvedValue({ projects: [] })
  api.googleStatus.mockResolvedValue({ connected: true, credentials_present: true })
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('adding a manga', () => {
  it('switches to photographs the moment manga is chosen', async () => {
    const user = userEvent.setup()
    await openTheForm(user)

    await user.selectOptions(screen.getByRole('combobox'), 'manga')

    expect(screen.getByRole('radio', { name: 'Photographs or scans' })).toBeChecked()
    expect(screen.getByText(/added from its page images/)).toBeInTheDocument()
  })

  it('does not offer text or a Google Doc for a manga', async () => {
    const user = userEvent.setup()
    await openTheForm(user)

    await user.selectOptions(screen.getByRole('combobox'), 'manga')

    expect(screen.getByRole('radio', { name: 'Paste it, or a .txt file' })).toBeDisabled()
    expect(screen.getByRole('radio', { name: 'A Google Doc' })).toBeDisabled()
  })

  it('creates it from scans and goes to its pages', async () => {
    const user = userEvent.setup()
    api.createFromScans.mockResolvedValue({
      project: { id: 'abc', kind: 'manga', ingest: 'images' } })
    await openTheForm(user)

    await user.selectOptions(screen.getByRole('combobox'), 'manga')
    await user.click(screen.getByRole('button', { name: 'Add it' }))

    await waitFor(() => expect(api.createFromScans).toHaveBeenCalledWith(
      { title: '', kind: 'manga' }))
    expect(api.createFromText).not.toHaveBeenCalled()
    expect(await screen.findByText('the pages screen')).toBeInTheDocument()
  })

  it('gives a novel every source back', async () => {
    const user = userEvent.setup()
    await openTheForm(user)

    await user.selectOptions(screen.getByRole('combobox'), 'manga')
    await user.selectOptions(screen.getByRole('combobox'), 'novel')

    expect(screen.getByRole('radio', { name: 'Paste it, or a .txt file' })).toBeEnabled()
    expect(screen.getByRole('radio', { name: 'A Google Doc' })).toBeEnabled()
    expect(screen.queryByText(/added from its page images/)).not.toBeInTheDocument()
  })
})
