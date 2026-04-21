/**
 * VerifyingLink — "Check your email" waiting screen.
 *
 * TODO:
 *   - Shown after requestMagicLink succeeds
 *   - Deep-link from email calls cloudApi.verifyMagicLink(token)
 *   - Electron handles the potluck:// scheme via ipc.ts and passes token here
 *   - On verified: writes session to authStore, navigates to /chat
 */
export function VerifyingLink() {
  return null;
}
