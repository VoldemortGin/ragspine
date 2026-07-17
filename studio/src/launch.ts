/**
 * One-shot bootstrap for CLI launch sessions (`/studio/?launch=<token>`).
 *
 * `ragspine workflow serve <file> --open` opens Studio with an opaque
 * URL-safe token; the workflow itself is fetched from the backend so its
 * path/content never appear in the URL. After a successful import the
 * token is removed from the address bar so a refresh does not re-import.
 */

import { fetchLaunchSession } from './api/client';
import { useEditorStore } from './pages/workflows/store';

const LAUNCH_PARAM = 'launch';
const TOKEN_PATTERN = /^[A-Za-z0-9_-]{1,64}$/;

/**
 * Extracts the opaque `launch` token from a query string, or null when the
 * parameter is absent or not a well-formed token. The value is never
 * interpreted as content.
 */
export function parseLaunchSessionId(search: string): string | null {
  const token = new URLSearchParams(search).get(LAUNCH_PARAM);
  return token !== null && TOKEN_PATTERN.test(token) ? token : null;
}

export interface ApplyLaunchSessionOptions {
  /** Query string to read the token from (defaults to `window.location.search`). */
  search?: string;
  /**
   * Installs the post-import query string — `''` or `'?…'` with `launch`
   * removed (defaults to `history.replaceState` on the current URL).
   */
  replaceUrl?: (search: string) => void;
}

/** One-shot guard: StrictMode double-invokes the mount effect. */
let consumed = false;

/** Test-only: allows applyLaunchSession to run again. */
export function resetLaunchSessionGuard(): void {
  consumed = false;
}

function searchWithoutLaunch(search: string): string {
  const params = new URLSearchParams(search);
  params.delete(LAUNCH_PARAM);
  const rest = params.toString();
  return rest === '' ? '' : `?${rest}`;
}

function replaceBrowserSearch(search: string): void {
  const { pathname, hash } = window.location;
  window.history.replaceState(window.history.state, '', `${pathname}${search}${hash}`);
}

/**
 * Imports the CLI-selected workflow once per page load. Resolves true when a
 * launch session was fetched and loaded into the editor; false when there is
 * no (valid) token, the session was already consumed, or the fetch failed —
 * in which case the store is left untouched.
 */
export async function applyLaunchSession(options?: ApplyLaunchSessionOptions): Promise<boolean> {
  if (consumed) return false;
  const search =
    options?.search ?? (typeof window === 'undefined' ? '' : window.location.search);
  const id = parseLaunchSessionId(search);
  if (id === null) return false;
  consumed = true;
  try {
    const session = await fetchLaunchSession(id);
    useEditorStore.getState().createFromTemplate({ name: session.name, yaml: session.yaml });
  } catch {
    console.warn(`Launch session "${id}" could not be loaded; starting without it.`);
    return false;
  }
  (options?.replaceUrl ?? replaceBrowserSearch)(searchWithoutLaunch(search));
  return true;
}
