export interface SourceCollection {
  id: string;
  name: string;
  scan_status: string;
  last_scan_at: string | null;
}
export interface BriefItem {
  text: string;
  citation_ids: string[];
  source_excerpts?: Array<{ citation_id: string; quote: string }>;
}
export interface Brief {
  questions?: Array<{ id: string; text: string }>;
  answers?: Array<{
    question_id: string;
    status: 'supported' | 'unknown' | 'conflicting';
    text: string;
    snippet_ids: string[];
    source_excerpts?: SourceExcerpt[];
  }>;
  facts: BriefItem[];
  prior_fixes: BriefItem[];
  hypotheses: BriefItem[];
  recommended_checks: BriefItem[];
  missing_evidence?: string[];
  model_uncertainties?: string[];
}
export interface SourceExcerpt {
  snippet_id: string;
  citation_id: string;
  quote: string;
}
export interface Investigation {
  id: string;
  workspace: string;
  collection_ids: string[];
  issue: string;
  model_slug: string;
  status: 'draft' | 'reviewed' | 'invalidated';
  brief: Brief;
  evidence: Array<Record<string, unknown>>;
  retrieval_notices?: string[];
  created_at: string;
  provenance?: {
    schema_version?: number;
    question_origin?: 'explicit' | 'planned';
    snippet_catalog?: SourceExcerpt[];
    retrieval_notices?: string[];
    model_sha256?: string;
    template_version?: string;
    retrieval_version?: string;
    elapsed_ms?: number;
  };
  review: null | { decision: string; reviewer: string; reason: string; corrected_brief?: Brief };
  outcome: null | { reviewer: string; result: string; notes: string };
}
export interface InvestigationScope {
  workspace: string;
  collection_ids: string[];
}
