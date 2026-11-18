export interface PilotSource {
  citation_id: string;
  collection_id: string;
  document_id: string;
  revision_id: string;
  chunk_id: string;
  relative_path: string;
  heading: string;
  content: string;
}
export interface PilotClaim {
  id: string;
  claim_key: string;
  content: string;
  status: 'proposed' | 'approved' | 'superseded';
  reviewer?: string | null;
  reason?: string | null;
  sources: PilotSource[];
}
export interface PilotEvidence {
  source_chunks: PilotSource[];
  truncated: boolean;
  missing_evidence: string[];
  scope_status: { requested: number; ready: number };
}

export interface PilotContextPreview {
  preview_id: string;
  content: string;
  citation_ids: string[];
  documents: Array<{
    relative_path: string;
    collection_name: string;
    collection_id: string;
    document_id: string;
    revision_id: string;
    opening: PilotSource[];
    excerpts: PilotSource[];
  }>;
  limitations: string[];
}

export interface PilotSetupDraft {
  version: string;
  question: string;
  question_parts: { parts: string[]; coverage: 'not_verified'; limitations: string[] };
  query_trace: { effective_query: string; omitted_terms: string[] };
  candidates: Array<{
    relative_path: string;
    collection_id: string;
    document_id: string;
    revision_id: string;
    preview: PilotContextPreview | null;
    notice: string;
    match?: {
      version:
        | 'document-topics-v1'
        | 'document-topics-v2'
        | 'document-topics-v3'
        | 'document-topics-v4';
      title_terms: string[];
      text_terms: string[];
    };
  }>;
  truncated: boolean;
  limitations: string[];
}
export type PilotSourceDraft = PilotSetupDraft['candidates'][number];

export interface PilotModelSetup {
  version: 'company-brain-model-setup-v1';
  model: {
    id: string;
    name: string;
    sha256: string;
    size_bytes: number;
    context_tokens: number;
    backend: 'llama-server';
  };
  platform_supported: boolean;
  installed: boolean;
  artifact_verified: boolean;
  ready: boolean;
  can_import: boolean;
  can_prepare: boolean;
  can_unload: boolean;
  active_model: string | null;
  issues: Array<{ code: string; message: string; blocks: Array<'import' | 'prepare'> }>;
  resources: {
    estimated_ram_bytes: number;
    available_ram_bytes: number | null;
    required_disk_bytes: number;
    disk_free_bytes: number | null;
  };
  warnings: string[];
}
