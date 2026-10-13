/**
 * Pick the recovery action that matches a failed reply.
 *
 * A 503 can mean "no model loaded", but it also covers every routing failure:
 * no peer matched, the contributor disconnected, no coordinator is
 * configured. A "Load a model" button on those would send people to the model
 * library, where nothing they did could fix the problem.
 *
 * So classify on what the message actually says, and offer nothing when the
 * text does not identify a cause. A button that leads somewhere useless is
 * worse than no button.
 */

export type ChatRecoveryAction = 'load-model' | 'check-network' | null;

export interface ChatRecovery {
  action: ChatRecoveryAction;
  label: string;
  route: string;
}

/** This machine has no model loaded. The user can fix that in the library. */
const LOCAL_MODEL_PATTERNS = [
  'no model loaded',
  'no local model',
  'no model is loaded',
  'model is not loaded',
  'load a model',
  'load one first',
];

/** The failure is somewhere on the network path, not in the local model slot. */
const NETWORK_PATTERNS = [
  'contributor',
  'peer',
  'coordinator',
  'circle',
  'household',
  'mesh',
  'open pool',
  'no online',
  'routing',
];

/**
 * Choose a recovery for an error message, or null when the cause is unclear.
 *
 * Local model problems win over network ones when a message names both (as
 * "No model loaded on this node, and no household peer matched." does),
 * because loading a model here is the one repair the user can always finish.
 */
export function classifyChatError(errorText: string | null | undefined): ChatRecovery {
  const lowered = typeof errorText === 'string' ? errorText.toLowerCase() : '';
  if (!lowered) return { action: null, label: '', route: '' };

  if (LOCAL_MODEL_PATTERNS.some((p) => lowered.includes(p))) {
    return { action: 'load-model', label: 'Load a model', route: '/models' };
  }
  if (NETWORK_PATTERNS.some((p) => lowered.includes(p))) {
    return { action: 'check-network', label: 'Check routing', route: '/network' };
  }
  return { action: null, label: '', route: '' };
}
