/**
 * Model domain types.
 *
 * Shapes mirror the Python sidecar model registry and cloud API registry.
 * See guide §4.2 and apps/desktop/python/potluck_local/routers/models.py.
 */

export type ModelTier = 'small' | 'medium' | 'large';
export type ModelStatus = 'available' | 'downloading' | 'installed' | 'loading' | 'loaded';

export interface Model {
  /** Unique slug used in API paths, e.g. "llama-3.1-8b-instruct-q5" */
  slug: string;
  name: string;
  description: string;
  /** Parameter count as a display string, e.g. "8B" */
  parameters: string;
  /** Quantisation label, e.g. "Q5_K_M" */
  quantisation: string;
  /** Compressed file size in bytes */
  sizeBytes: number;
  /** Minimum recommended hardware tier */
  minTier: ModelTier;
  status: ModelStatus;
  installed: boolean;
}

export interface InstalledModel extends Model {
  status: Extract<ModelStatus, 'installed' | 'loading' | 'loaded'>;
  /** Absolute path to the .gguf file on disk */
  localPath: string;
}

export interface DownloadProgress {
  modelId: string;
  bytesDownloaded: number;
  bytesTotal: number;
  /** Bytes per second */
  speedBps: number;
  etaSeconds: number;
  status: 'downloading' | 'verifying' | 'complete' | 'error' | 'cancelled';
}
