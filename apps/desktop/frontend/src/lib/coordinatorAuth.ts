/**
 * Session-authenticated coordinator calls.
 *
 * A handful of coordinator routes require the member's login session rather
 * than the (optional) coordinator token:
 *
 *   - POST /mesh/households      (household bootstrap)
 *   - POST /mesh/pairing-tokens  (pairing-token issuance)
 *   - POST /me/device-keys       (device-key pre-registration)
 *
 * All three resolve identity via `resolve_session_member_id`, which accepts
 * an `Authorization: Bearer <session token>` header — the desktop app's
 * fallback for a cross-origin cookie (see coordinator's
 * `middleware/session_auth.py`).
 *
 * The session token itself lives wherever cloudApi.ts keeps it: the Electron
 * main process (packaged app) or an httpOnly cookie (dev/browser) — see
 * authStore.ts. It is never persisted in renderer state and main process
 * never hands the raw value to renderer JS, so in the packaged app these
 * calls are proxied through main via `window.potluck.cloud.coordinatorRequest`,
 * which attaches the bearer itself and returns `{ status, body }`. This
 * mirrors cloudApi.ts's dual-path transport, except the coordinator origin is
 * caller-supplied — the coordinator is a member-operated node the user points
 * the app at (see connectionStore.ts), not a fixed api.trypotluck.ai host —
 * so main.js allowlists the exact paths this module calls rather than fixing
 * the origin.
 *
 * In dev (Vite dev server, no bridge), the coordinator almost always runs
 * with `POTLUCK_SESSION_AUTH_ENABLED` unset (dev passthrough — see
 * `session_auth.py`), so an absent/empty bearer is fine there; we still
 * attach the auth store's in-memory token when one happens to be present
 * (e.g. moments after a fresh sign-in) as a best effort.
 */

import { coordinatorBase } from '@/stores/connectionStore';
import { useAuthStore } from '@/stores/authStore';

export interface CoordinatorSessionResult {
  status: number;
  body: unknown;
}

interface CoordinatorBridge {
  coordinatorRequest: (
    baseUrl: string,
    method: string,
    path: string,
    body?: unknown,
  ) => Promise<{ status: number; body: unknown }>;
}

function bridge(): CoordinatorBridge | undefined {
  if (typeof window === 'undefined' || window.location.protocol !== 'file:') {
    return undefined;
  }
  return (window as unknown as { potluck?: { cloud?: CoordinatorBridge } }).potluck?.cloud;
}

/** Best-effort extraction of a `{ detail: string }` error body, matching
 *  FastAPI's default error shape. Falls back to a generic message. */
export function detailOf(body: unknown, fallback = 'unknown error'): string {
  if (typeof body === 'object' && body !== null && 'detail' in body) {
    const d = (body as { detail?: unknown }).detail;
    if (typeof d === 'string') return d;
  }
  return fallback;
}

/**
 * POST to a session-authed coordinator route. Attaches the member's session
 * bearer (packaged app: via the main-process proxy; dev: best-effort from
 * the in-memory auth store). Never throws on a non-2xx HTTP response — the
 * caller inspects `status`/`body`; a transport failure resolves `status: 0`.
 */
export async function coordinatorSessionRequest(
  path: string,
  body?: unknown,
): Promise<CoordinatorSessionResult> {
  const b = bridge();
  if (b) {
    return b.coordinatorRequest(coordinatorBase(), 'POST', path, body);
  }

  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  const token = useAuthStore.getState().session?.token;
  if (token) headers['Authorization'] = `Bearer ${token}`;

  try {
    const init: RequestInit = { method: 'POST', headers };
    if (body !== undefined) {
      init.body = JSON.stringify(body);
    }
    const res = await fetch(`${coordinatorBase()}${path}`, init);
    const text = await res.text();
    let parsed: unknown = null;
    if (text.length > 0) {
      try {
        parsed = JSON.parse(text);
      } catch {
        parsed = { raw: text };
      }
    }
    return { status: res.status, body: parsed };
  } catch (err) {
    return {
      status: 0,
      body: { detail: err instanceof Error ? err.message : 'network error' },
    };
  }
}
