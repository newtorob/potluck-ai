export interface SourceCollection {
  id: string;
  name: string;
  scan_status: string;
  last_scan_at: string | null;
}
export interface BriefItem {
  text: string;
  citation_ids: string[];
}
export interface Brief {
  facts: BriefItem[];
  prior_fixes: BriefItem[];
  hypotheses: BriefItem[];
  recommended_checks: BriefItem[];
  missing_evidence: string[];
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
  created_at: string;
  provenance?: {
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
