/**
 * Words for a network request the coordinator turned away before any answer
 * started.
 *
 * The coordinator's own detail is written for API callers ("...or use
 * scope=pool"). In the chat the person needs to know what happened and what
 * they can do about it. Returns null where there is nothing better to say
 * than the coordinator's line, so the caller keeps that.
 */
export function networkRefusalCopy(
  scope: 'circle' | 'cooperative',
  status: number,
  modelSlug: string,
): string | null {
  if (status === 401) {
    return 'Your sign-in has expired. Sign in again to send requests over the network.';
  }
  if (scope === 'circle') {
    if (status === 503) {
      return `No machine in this circle is offering ${modelSlug} right now. Ask a member to open Potluck with sharing on, or pick another circle model.`;
    }
    if (status === 404) {
      return 'This circle no longer exists. Pick another circle in the scope menu.';
    }
    return null;
  }
  if (status === 503) {
    return `No machine on the open pool is offering ${modelSlug} right now.`;
  }
  return null;
}
