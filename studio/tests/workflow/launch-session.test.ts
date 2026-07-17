/** Launch-session bootstrap: token parsing, one-shot import, URL cleanup. */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// The store module reads localStorage at import time (via model/library, which
// tolerates a missing storage) — stub an in-memory one before it is imported
// so persistence behaves identically on every node version.
vi.hoisted(() => {
  const data = new Map<string, string>();
  Object.defineProperty(globalThis, 'localStorage', {
    configurable: true,
    value: {
      getItem: (key: string) => data.get(key) ?? null,
      setItem: (key: string, value: string) => void data.set(key, value),
      removeItem: (key: string) => void data.delete(key),
      clear: () => data.clear(),
      key: (index: number) => [...data.keys()][index] ?? null,
      get length() {
        return data.size;
      },
    },
  });
});

import {
  applyLaunchSession,
  parseLaunchSessionId,
  resetLaunchSessionGuard,
} from '../../src/launch';
import { useEditorStore } from '../../src/pages/workflows/store';
import { loadFixtureText } from './helpers';

const initialState = useEditorStore.getState();

const state = () => useEditorStore.getState();

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

const SESSION = { id: 'tok123', name: 'CLI Flow', yaml: loadFixtureText('seq') };

beforeEach(() => {
  useEditorStore.setState(initialState, true);
  resetLaunchSessionGuard();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('parseLaunchSessionId', () => {
  it('accepts opaque URL-safe tokens of 1..64 chars', () => {
    expect(parseLaunchSessionId('?launch=aB3_-z')).toBe('aB3_-z');
    expect(parseLaunchSessionId('?launch=a')).toBe('a');
    expect(parseLaunchSessionId(`?launch=${'a'.repeat(64)}`)).toBe('a'.repeat(64));
    expect(parseLaunchSessionId('?page=jobs&launch=tok123')).toBe('tok123');
  });

  it('rejects missing, empty, oversized, or non-token values', () => {
    expect(parseLaunchSessionId('')).toBeNull();
    expect(parseLaunchSessionId('?other=1')).toBeNull();
    expect(parseLaunchSessionId('?launch=')).toBeNull();
    expect(parseLaunchSessionId(`?launch=${'a'.repeat(65)}`)).toBeNull();
    expect(parseLaunchSessionId('?launch=a.b')).toBeNull();
    // Percent-escapes decode to non-token characters and stay rejected.
    expect(parseLaunchSessionId('?launch=a%2Fb')).toBeNull();
    expect(parseLaunchSessionId('?launch=not%20safe')).toBeNull();
  });
});

describe('applyLaunchSession', () => {
  it('fetches the session, imports it, and strips the token from the URL', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(SESSION));
    vi.stubGlobal('fetch', fetchMock);
    const replaceUrl = vi.fn<(search: string) => void>();

    const applied = await applyLaunchSession({ search: '?launch=tok123&page=jobs', replaceUrl });

    expect(applied).toBe(true);
    expect(fetchMock).toHaveBeenCalledWith(
      '/v1/launch-sessions/tok123',
      expect.objectContaining({ method: 'GET' }),
    );
    // The workflow became the active document, loaded onto the canvas.
    expect(state().base.name).toBe('CLI Flow');
    expect(state().library.find((e) => e.id === state().activeId)?.name).toBe('CLI Flow');
    expect(state().nodes.map((n) => n.id)).toEqual(
      expect.arrayContaining(['start_1', 'llm_1', 'tt_1', 'end_1']),
    );
    // The opaque token is gone from the address bar; other params survive.
    expect(replaceUrl).toHaveBeenCalledTimes(1);
    expect(replaceUrl).toHaveBeenCalledWith('?page=jobs');
    expect(replaceUrl.mock.calls[0]![0]).not.toContain('launch');
  });

  it('consumes the session exactly once (StrictMode double-invoke)', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(SESSION));
    vi.stubGlobal('fetch', fetchMock);
    const replaceUrl = vi.fn<(search: string) => void>();

    const [first, second] = await Promise.all([
      applyLaunchSession({ search: '?launch=tok123', replaceUrl }),
      applyLaunchSession({ search: '?launch=tok123', replaceUrl }),
    ]);

    expect(first).toBe(true);
    expect(second).toBe(false);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(replaceUrl).toHaveBeenCalledTimes(1);
    expect(state().library.filter((e) => e.name.startsWith('CLI Flow'))).toHaveLength(1);
  });

  it('does not fetch when the param is missing or malformed', async () => {
    const fetchMock = vi.fn<typeof fetch>();
    vi.stubGlobal('fetch', fetchMock);
    const replaceUrl = vi.fn<(search: string) => void>();

    for (const search of [
      '',
      '?other=1',
      '?launch=',
      `?launch=${'a'.repeat(65)}`,
      '?launch=a.b',
      '?launch=not%20safe',
    ]) {
      expect(await applyLaunchSession({ search, replaceUrl })).toBe(false);
    }

    expect(fetchMock).not.toHaveBeenCalled();
    expect(replaceUrl).not.toHaveBeenCalled();
  });

  it('leaves the store and URL untouched when the session is unknown', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      jsonResponse(
        { error: { type: 'LaunchSessionNotFound', message: 'nope', request_id: 'r1' } },
        404,
      ),
    );
    vi.stubGlobal('fetch', fetchMock);
    const replaceUrl = vi.fn<(search: string) => void>();
    const before = state();

    const applied = await applyLaunchSession({ search: '?launch=missing', replaceUrl });

    expect(applied).toBe(false);
    expect(state().library).toBe(before.library);
    expect(state().nodes).toBe(before.nodes);
    expect(state().base).toBe(before.base);
    expect(replaceUrl).not.toHaveBeenCalled();
    expect(warn).toHaveBeenCalledTimes(1);
  });
});
