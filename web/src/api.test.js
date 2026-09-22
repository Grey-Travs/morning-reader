import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError, api } from './api'

// The server answers every failure with `detail` as ONE object shape
// (server/errors.Explained). These pin the unwrapping, because without a single
// unwrapping point half the interface shows a friendly sentence and the other half
// shows "[object Object]".

function respondWith({ ok = true, status = 200, body = {}, reject = false } = {}) {
  if (reject) {
    global.fetch = vi.fn().mockRejectedValue(new TypeError('Failed to fetch'))
    return
  }
  global.fetch = vi.fn().mockResolvedValue({
    ok,
    status,
    statusText: 'Internal Server Error',
    json: async () => body,
  })
}

afterEach(() => { vi.restoreAllMocks() })

describe('successful calls', () => {
  it('returns the parsed body', async () => {
    respondWith({ body: { projects: [{ id: 'abc' }] } })
    await expect(api.listProjects()).resolves.toEqual({ projects: [{ id: 'abc' }] })
  })

  it('sends run requests as JSON with a task kind', async () => {
    respondWith({ body: { job_id: 'j1', queued: [1, 2] } })
    await api.run('abc', { indices: [1, 2] })

    const [path, options] = global.fetch.mock.calls[0]
    expect(path).toBe('/api/projects/abc/run')
    expect(options.method).toBe('POST')
    expect(JSON.parse(options.body)).toEqual({ kind: 'prepare', indices: [1, 2] })
  })
})

describe('failures', () => {
  it('unwraps the explanation the server sent', async () => {
    respondWith({
      ok: false,
      status: 404,
      body: {
        detail: {
          code: 'not-found',
          title: 'That project does not exist.',
          what: '',
          fixes: [],
          retryable: false,
        },
      },
    })

    await expect(api.getProject('nope')).rejects.toMatchObject({
      name: 'ApiError',
      status: 404,
      explained: { code: 'not-found', title: 'That project does not exist.' },
    })
  })

  it('uses the explanation title as the error message', async () => {
    respondWith({
      ok: false,
      status: 429,
      body: { detail: { code: 'rate-limited', title: "Your plan's limit was reached" } },
    })

    await expect(api.listProjects()).rejects.toThrow("Your plan's limit was reached")
  })

  it('survives an error response with no JSON body at all', async () => {
    // A proxy or a crash can produce one; falling over here would replace a bad
    // response with a blank screen.
    global.fetch = vi.fn().mockResolvedValue({
      ok: false,
      status: 502,
      statusText: 'Bad Gateway',
      json: async () => { throw new SyntaxError('Unexpected end of JSON input') },
    })

    const error = await api.listProjects().catch((e) => e)
    expect(error).toBeInstanceOf(ApiError)
    expect(error.status).toBe(502)
    expect(error.explained.title).toBe('Bad Gateway')
  })

  it('distinguishes a server that is not answering from one that reported a problem', async () => {
    // The only case where telling the user to check the server is running helps.
    respondWith({ reject: true })

    const error = await api.health().catch((e) => e)
    expect(error.status).toBe(0)
    expect(error.explained.code).toBe('offline')
    expect(error.explained.fixes.length).toBeGreaterThan(0)
  })
})

describe('the stream url', () => {
  it('points at the job that belongs to the project', () => {
    expect(api.streamUrl('abc', 'job1')).toBe('/api/projects/abc/run/job1/stream')
  })
})
