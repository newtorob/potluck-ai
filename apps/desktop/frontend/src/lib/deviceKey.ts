/**
 * Contributor device-key registration.
 *
 * Closes the "onboarding trap" the prod coordinator's
 * `REQUIRE_REGISTERED_KEYS=true` posture creates: the Python contributor
 * sidecar (`potluck_local/contributor/client.py`) attests to the coordinator
 * with a per-machine Ed25519 key, but a fresh key is rejected outright unless
 * it was pre-registered to the signed-in member via the coordinator's
 * session-authed `POST /me/device-keys`. The contributor process has no
 * session of its own — only the frontend does — so registration has to
 * happen from here.
 *
 * Flow:
 *   1. GET the local sidecar's `/contributor/device-key` (loopback-only;
 *      lazily generates + persists the keypair on first call — see
 *      `potluck_local/routers/contributor.py`).
 *   2. POST that public key to the coordinator's `/me/device-keys`
 *      (session-authed — see `coordinatorAuth.ts`).
 *
 * Idempotent: a 409 (key already registered — to this member or, harmlessly,
 * because the call raced/retried) is treated as success.
 */

import { coordinatorSessionRequest, detailOf } from '@/lib/coordinatorAuth';

const LOCAL_BASE =
  (import.meta.env['VITE_LOCAL_API_URL'] as string | undefined) ?? 'http://127.0.0.1:8321';

interface DeviceKeyResponse {
  public_key: string;
}

/** GET /contributor/device-key on the local sidecar. */
export async function getContributorDeviceKey(): Promise<string> {
  const res = await fetch(`${LOCAL_BASE}/contributor/device-key`);
  if (!res.ok) {
    const body: unknown = await res.json().catch(() => ({}));
    const detail =
      typeof body === 'object' && body !== null && 'detail' in body
        ? String((body as { detail?: unknown }).detail)
        : res.statusText;
    throw new Error(`[deviceKey] could not read device key: ${detail}`);
  }
  const data = (await res.json()) as DeviceKeyResponse;
  return data.public_key;
}

/**
 * Register this machine's contributor device key with the coordinator.
 * Safe to call repeatedly — 409 (already registered) is treated as success.
 * Throws on any other failure (caller decides whether to surface it or just
 * log it, since this is best-effort ahead of the contributor process
 * actually attesting).
 */
export async function registerContributorDeviceKey(label?: string): Promise<void> {
  const publicKey = await getContributorDeviceKey();
  const res = await coordinatorSessionRequest('/me/device-keys', {
    public_key: publicKey,
    ...(label ? { label } : {}),
  });

  if (res.status === 201 || res.status === 409) {
    return; // registered now, or already was — both are success.
  }
  throw new Error(`[deviceKey] could not register device key: ${detailOf(res.body)}`);
}
