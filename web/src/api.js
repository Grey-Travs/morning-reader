// One place that talks to the server, so every caller gets errors in the same shape.
//
// The server answers every failure — handled or not — with `detail` as one object
// (see server/errors.Explained). That is the whole reason this file exists: without a
// single unwrapping point, half the interface shows a friendly sentence and the other
// half shows "[object Object]".

export class ApiError extends Error {
  constructor(explained, status) {
    super(explained?.title || `Request failed (${status})`)
    this.name = 'ApiError'
    this.explained = explained || {}
    this.status = status
  }
}

async function req(path, options = {}) {
  let response
  try {
    response = await fetch(path, options)
  } catch (cause) {
    // The server is not answering at all — a different problem from one it reported,
    // and the only one where telling the user to check it is running actually helps.
    throw new ApiError({
      code: 'offline',
      title: 'Morning Reader is not responding',
      what: 'The app could not reach its own server.',
      fixes: ['Check the server window is still open.', 'Reload this page.'],
      retryable: true,
    }, 0)
  }

  if (!response.ok) {
    let detail = null
    try {
      detail = (await response.json())?.detail
    } catch {
      // A response with no JSON body at all. Fall through to the status line.
    }
    throw new ApiError(detail || { code: 'request-failed', title: response.statusText },
      response.status)
  }
  if (response.status === 204) return null
  return response.json()
}

const json = (body) => ({
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
})

export const api = {
  health: () => req('/api/health'),

  listProjects: () => req('/api/projects'),
  getProject: (pid) => req(`/api/projects/${pid}`),
  getChapter: (pid, index) => req(`/api/projects/${pid}/chapters/${index}`),

  createFromText: (body) => req('/api/projects/text', json(body)),
  // A scanned work starts EMPTY — its chapters do not exist until the pages have
  // been read and built. See the route's docstring for why that is stored, not
  // inferred.
  createFromScans: (body) => req('/api/projects/scan', json(body)),
  uploadTxt: (file, { title = '', kind = 'novel', mode = 'heading' } = {}) => {
    const form = new FormData()
    form.append('file', file)
    const query = new URLSearchParams({ title, kind, mode })
    return req(`/api/projects/upload?${query}`, { method: 'POST', body: form })
  },

  // Google Docs — one of the four ingestion paths, and read-only. `status`
  // distinguishes "no OAuth client set up" from "not signed in", because the two need
  // different things from the user.
  googleStatus: () => req('/api/google/status'),
  googleConnect: () => req('/api/google/connect', json({})),
  googleDisconnect: () => req('/api/google/disconnect', json({})),
  createFromDoc: (body) => req('/api/projects/docs', json(body)),
  refreshSource: (pid) => req(`/api/projects/${pid}/source/refresh`, json({})),

  patchProject: (pid, body) => req(`/api/projects/${pid}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }),
  deleteProject: (pid) => req(`/api/projects/${pid}`, { method: 'DELETE' }),
  replaceSource: (pid, body) => req(`/api/projects/${pid}/source`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }),

  // Starting work returns immediately with a job id rather than blocking until it is
  // done — that is what makes it visible in Activity and survivable across a reload.
  run: (pid, body = {}) => req(`/api/projects/${pid}/run`, json({ kind: 'prepare', ...body })),
  cancel: (pid, stopCurrent = false) =>
    req(`/api/projects/${pid}/run/cancel`, json({ stop_current: stopCurrent })),
  resume: (pid) => req(`/api/projects/${pid}/run/resume`, json({})),

  // Reading. `from_audit` on the response says the English shown has NOT been
  // accepted yet — it is the review copy. The reader must surface that rather than
  // presenting un-reviewed work as finished.
  read: (pid, index) => req(`/api/projects/${pid}/read/${index}`),
  accept: (pid, index, english = null) =>
    req(`/api/projects/${pid}/chapters/${index}/accept`,
        json(english === null ? {} : { english })),

  // The glossary and the terms waiting on a human. Both come back together because
  // deciding whether a proposed term is new means looking at what is already there.
  glossary: (pid) => req(`/api/projects/${pid}/glossary`),
  approveTerm: (pid, entry) => req(`/api/projects/${pid}/glossary/approve`, json(entry)),
  rejectTerm: (pid, source) =>
    req(`/api/projects/${pid}/glossary/reject`, json({ source })),
  removeTerm: (pid, source) =>
    req(`/api/projects/${pid}/glossary/remove`, json({ source })),

  // Scanned pages. Every file is identified and measured from its own BYTES on the
  // server — the content type is advisory and the filename is a guess.
  pages: (pid) => req(`/api/projects/${pid}/pages`),
  page: (pid, pageId) => req(`/api/projects/${pid}/pages/${pageId}`),
  pageImageUrl: (pid, pageId) => `/api/projects/${pid}/pages/${pageId}/image`,
  uploadPages: (pid, files, label = '') => {
    const form = new FormData()
    for (const file of files) form.append('files', file)
    return req(`/api/projects/${pid}/pages?label=${encodeURIComponent(label)}`,
      { method: 'POST', body: form })
  },
  readPages: (pid, body = {}) => req(`/api/projects/${pid}/pages/read`, json(body)),
  reorderPages: (pid, ids) => req(`/api/projects/${pid}/pages/reorder`, json({ ids })),
  deletePages: (pid, ids) => req(`/api/projects/${pid}/pages/delete`, json({ ids })),
  setPageJoin: (pid, pageId, kind, glue = 'none') =>
    req(`/api/projects/${pid}/pages/${pageId}/join`, json({ kind, glue })),
  setPageStatus: (pid, pageId, status) =>
    req(`/api/projects/${pid}/pages/${pageId}/status`, json({ status })),
  proposeJoins: (pid) => req(`/api/projects/${pid}/pages/propose-joins`, json({})),
  buildFromPages: (pid) => req(`/api/projects/${pid}/pages/build`, json({})),

  queueOverview: () => req('/api/queue'),
  activeJob: (pid) => req(`/api/projects/${pid}/active-job`),

  streamUrl: (pid, jobId) => `/api/projects/${pid}/run/${jobId}/stream`,
}
