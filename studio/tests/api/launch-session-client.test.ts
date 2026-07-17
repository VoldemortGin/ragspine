/** Typed client requests for the CLI launch-session boundary. */

import { afterEach, describe, expect, it, vi } from 'vitest';

import { ApiError, fetchLaunchSession } from '../../src/api/client';

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('launch session client', () => {
  it('fetches the session with GET and forwards cancellation', async () => {
    const payload = { id: 'tok_1-A', name: 'CLI Flow', yaml: 'app: {}' };
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(payload));
    vi.stubGlobal('fetch', fetchMock);
    const controller = new AbortController();

    const session = await fetchLaunchSession('tok_1-A', controller.signal);

    expect(session).toEqual(payload);
    expect(fetchMock).toHaveBeenCalledWith(
      '/v1/launch-sessions/tok_1-A',
      expect.objectContaining({ method: 'GET', signal: controller.signal }),
    );
  });

  it('URL-encodes the session id', async () => {
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValue(jsonResponse({ id: 'a/b', name: 'x', yaml: '' }));
    vi.stubGlobal('fetch', fetchMock);

    await fetchLaunchSession('a/b');

    expect(fetchMock).toHaveBeenCalledWith(
      '/v1/launch-sessions/a%2Fb',
      expect.objectContaining({ method: 'GET' }),
    );
  });

  it('surfaces unknown sessions as an ApiError', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      jsonResponse(
        {
          error: {
            type: 'LaunchSessionNotFound',
            message: 'unknown launch session',
            request_id: 'req-404',
          },
        },
        404,
      ),
    );
    vi.stubGlobal('fetch', fetchMock);

    const error = await fetchLaunchSession('missing').catch((reason: unknown) => reason);

    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({
      status: 404,
      type: 'LaunchSessionNotFound',
      requestId: 'req-404',
    });
  });
});
