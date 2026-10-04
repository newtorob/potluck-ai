/**
 * Features that are built but not released yet.
 *
 * Each one is off unless its flag is set to "1" when the frontend is built,
 * so a release only shows what has been finished and validated. Turn one on
 * locally with, for example, VITE_POTLUCK_AGENT_WORKROOM=1 pnpm dev.
 *
 * openPoolContributor needs POTLUCK_OPEN_POOL_CONTRIBUTOR=1 in the engine as
 * well; without it the Contribute toggle only serves your own machines.
 */
const env = import.meta.env as Record<string, string | boolean | undefined>;
const on = (name: string): boolean => env[name] === '1';

export const FEATURES = {
  /** Agent runs, connector hooks and the task workroom. */
  agentWorkroom: on('VITE_POTLUCK_AGENT_WORKROOM'),
  /** Models across your machines and managed split launch. */
  managedSplit: on('VITE_POTLUCK_MANAGED_SPLIT'),
  /** The Contribute toggle also serving open-pool requests. */
  openPoolContributor: on('VITE_POTLUCK_OPEN_POOL_CONTRIBUTOR'),
} as const;
