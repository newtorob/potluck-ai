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
