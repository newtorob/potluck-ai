export interface AvailableModel {
  id: string; sha256: string | null; local: boolean; idleMachineCount: number;
  machines: { name: string; meshIp: string; idle: boolean }[];
}
export interface ModelAvailability {
  schemaVersion: 1; scope: 'mine'; checkedAt: number;
  models: AvailableModel[];
  installedModels: { id: string; sizeBytes: number }[];
  machines: { name: string; meshIp: string; responding: boolean; splitReady: boolean; splitCompatible: boolean; backend: string | null; freeMemoryBytes: number | null }[];
  additionalModels: number; splitWorkerCount: number; splitEngineAvailable: boolean; truncated: boolean;
}
export interface SplitModelPin {
  id: string; format: 'gguf'; sha256: string; sizeBytes: number;
  source?: { url: string } | null;
}
export interface SplitSelection { model: SplitModelPin; peers: string[]; context: number }
export interface SplitStatus {
  state: string; error: string | null;
  session: null | { id: string; model: SplitModelPin; context: number; phase: string; elapsedSeconds: number; loadSeconds: number | null;
    workers: { name: string; sentBytes: number; receivedBytes: number; modelMiB: number }[];
    placement: { device: string; modelMiB: number }[] };
}
