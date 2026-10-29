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
export type OperationType =
  | 'answer'
  | 'difference'
  | 'percentage_point_difference'
  | 'percent_change'
  | 'percentage';
export interface ReviewedOperation {
  request: string;
  operation: OperationType;
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
    extraction_notices?: Array<{
      snippet_id: string;
      citation_id: string;
      source_text: string;
      reason: string;
    }>;
    calculation_traces?: Array<{
      request_id: string;
      operation: OperationType;
      status: string;
      formula?: string | null;
      evaluated_expression?: string | null;
      result?: string | null;
      unit?: string | null;
      reason?: string | null;
      caveat?: string | null;
      algorithm_version?: string | null;
      rounding?: { mode: string; max_fractional_digits: number } | null;
      inputs?: Array<{
        id: string;
        citation_id: string;
        source_text: string;
        unit_source_text?: string;
        unit_source_start?: number;
        unit_source_end?: number;
      }>;
      normalized_inputs?: Array<{
        role: string;
        value: string;
        unit: string;
        conversion_factor: string;
      }>;
    }>;
  };
  review: null | { decision: string; reviewer: string; reason: string; corrected_brief?: Brief };
  outcome: null | { reviewer: string; result: string; notes: string };
}
export interface InvestigationScope {
  workspace: string;
  collection_ids: string[];
}
