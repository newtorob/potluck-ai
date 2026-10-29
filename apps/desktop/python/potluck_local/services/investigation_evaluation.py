"""Owner-reviewed evaluation references and deterministic comparison of human labels.

No source text is copied to a new store and no model is invoked by this module.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any

from potluck_local.services.evidence_lookup import EvidenceScope, resolve_citation
from potluck_local.services.investigations import get_investigation


async def propose_evaluation_case(
    scope: EvidenceScope, investigation_id: str, *, owner_confirmed: bool
) -> dict[str, Any]:
    """Create a reference-only proposal from a reviewed investigation and recorded outcome."""
    if owner_confirmed is not True:
        raise PermissionError('Explicit owner confirmation required')
    item = await get_investigation(scope, investigation_id)
    if item['status'] != 'reviewed' or not item['review'] or not item['outcome']:
        raise ValueError('Reviewed investigation and actual outcome required')
    citations = sorted({e['citation_id'] for e in item['evidence']})
    for citation in citations:
        if await resolve_citation(scope, citation) is None:
            raise ValueError('Investigation evidence unavailable')
    return {
        'investigation_id': investigation_id,
        'workspace': scope.workspace,
        'collection_ids': sorted(scope.collection_ids),
        'citation_ids': citations,
        'review_decision': item['review']['decision'],
        'outcome_result': item['outcome']['result'],
        'model_slug': item['model_slug'],
        'model_sha': item['model_sha'],
        'status': 'proposed',
    }


async def validate_evaluation_case(scope: EvidenceScope, case: dict[str, Any]) -> bool:
    """Revalidate a reference before using it; changes or revoked sources invalidate it."""
    try:
        current = await propose_evaluation_case(scope, case['investigation_id'], owner_confirmed=True)
        return current == case
    except (KeyError, TypeError, ValueError, LookupError, PermissionError):
        return False


def _rate(values: list[bool]) -> float | None:
    return sum(values) / len(values) if values else None


def _duration(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0:
        raise ValueError('Invalid duration')
    return float(value)


def _summarize(case_ids: set[str], run: dict[str, Any]) -> dict[str, Any]:
    outputs = run.get('outputs')
    if not isinstance(outputs, dict) or set(outputs) - case_ids:
        raise ValueError('Invalid output case IDs')
    missing = sorted(case_ids - set(outputs))
    metrics: dict[str, list[bool]] = {
        'citation_validity': [], 'semantic_support': [], 'unsupported_answer': [],
        'correction': [],
    }
    durations: dict[str, list[float]] = {'latency_seconds': [], 'time_to_accepted_seconds': []}
    incomplete = []
    for case_id, output in outputs.items():
        if not isinstance(output, dict):
            raise ValueError('Invalid output')
        complete = True
        for key in metrics:
            value = output.get(key)
            if value is None:
                complete = False
            elif type(value) is not bool:
                raise ValueError('Labels must be boolean or unavailable')
            else:
                metrics[key].append(value)
        for key in durations:
            duration = _duration(output.get(key))
            if duration is not None:
                durations[key].append(duration)
        if not complete:
            incomplete.append(case_id)
    failed = sorted(set(missing + incomplete) | {
        cid for cid, output in outputs.items()
        if output.get('citation_validity') is False or output.get('semantic_support') is False
        or output.get('unsupported_answer') is True
    })
    return {
        'case_count': len(case_ids), 'missing_outputs': missing,
        'all_case_pass_rate': (len(case_ids) - len(failed)) / len(case_ids),
        'incomplete_labels': sorted(incomplete), 'failed_cases': failed,
        'rates': {key: _rate(values) for key, values in metrics.items()},
        'labeled_counts': {key: len(values) for key, values in metrics.items()},
        'mean_seconds': {key: (sum(values) / len(values) if values else None)
                         for key, values in durations.items()},
        'timed_counts': {key: len(values) for key, values in durations.items()},
    }


def _compare_evaluations(
    manifest: dict[str, Any], baseline: dict[str, Any], candidate: dict[str, Any]
) -> dict[str, Any]:
    """Compare explicit labels without inferring support from citation existence.

    Runs may use different pinned models/configurations, which are returned for review.
    They must share the frozen corpus and case identifiers.
    """
    cases = manifest.get('cases')
    corpus = manifest.get('corpus_id')
    if not isinstance(corpus, str) or not corpus or not isinstance(cases, list) or not cases:
        raise ValueError('Invalid manifest')
    ids = [case.get('id') if isinstance(case, dict) else None for case in cases]
    if any(not isinstance(cid, str) or not cid for cid in ids) or len(set(ids)) != len(ids):
        raise ValueError('Invalid case IDs')
    manifest_sha = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
    ).hexdigest()
    for run in (baseline, candidate):
        if (run.get('corpus_id') != corpus or run.get('manifest_sha256') != manifest_sha
                or set(run.get('case_ids', [])) != set(ids)):
            raise ValueError('Run does not match frozen corpus and cases')
        if len(run['case_ids']) != len(ids):
            raise ValueError('Duplicate case IDs')
        config = run.get('model_config')
        if (not isinstance(config, dict) or not isinstance(config.get('model_sha256'), str)
                or not re.fullmatch(r'[0-9a-fA-F]{64}', config['model_sha256'])
                or not isinstance(config.get('settings'), dict)
                or not isinstance(config.get('runtime_version'), str)
                or not config['runtime_version'].strip()):
            raise ValueError('Pinned model and explicit settings required')
    return {
        'corpus_id': corpus, 'manifest_sha256': manifest_sha,
        'baseline': {'model_config': baseline['model_config'], **_summarize(set(ids), baseline)},
        'candidate': {'model_config': candidate['model_config'], **_summarize(set(ids), candidate)},
    }


def compare_evaluations(
    manifest: dict[str, Any], baseline: dict[str, Any], candidate: dict[str, Any]
) -> dict[str, Any]:
    """Compare artifacts, rejecting malformed inputs without exposing their contents."""
    try:
        return _compare_evaluations(manifest, baseline, candidate)
    except (TypeError, KeyError, AttributeError, OverflowError):
        raise ValueError('Invalid evaluation artifacts') from None


async def compare_reviewed_evaluations(
    scope: EvidenceScope, manifest: dict[str, Any], baseline: dict[str, Any],
    candidate: dict[str, Any],
) -> dict[str, Any]:
    """Compare owner-created cases only after each source reference is revalidated."""
    for case in manifest.get('cases', []):
        if not isinstance(case, dict) or not isinstance(case.get('proposal'), dict):
            raise ValueError('Every case requires an investigation proposal')
        if not await validate_evaluation_case(scope, case['proposal']):
            raise ValueError('Evaluation case is unavailable')
    return compare_evaluations(manifest, baseline, candidate)


async def propose_knowledge_update(
    scope: EvidenceScope, investigation_id: str, claim_key: str, content: str,
    citation_ids: list[str], *, owner_confirmed: bool,
) -> str:
    """Explicitly propose, never approve, a claim linked to reviewed cited sources."""
    if owner_confirmed is not True:
        raise PermissionError('Explicit owner confirmation required')
    item = await get_investigation(scope, investigation_id)
    if (item['status'] != 'reviewed' or not item['outcome'] or not item['review']
            or item['review']['decision'] not in ('accept', 'correct')):
        raise ValueError('Accepted or corrected investigation and outcome required')
    brief = (item['review']['corrected_brief'] if item['review']['decision'] == 'correct'
             else item['brief'])
    cited = {cid for section in ('facts', 'prior_fixes', 'hypotheses', 'recommended_checks')
             for entry in brief[section] for cid in entry['citation_ids']}
    if not citation_ids or len(set(citation_ids)) != len(citation_ids) or not set(citation_ids) <= cited:
        raise ValueError('Select citations from the reviewed brief')
    refs = []
    for cid in citation_ids:
        record = await resolve_citation(scope, cid)
        if record is None:
            raise ValueError('Citation unavailable')
        sources = record['sources'] if record['kind'] == 'reviewed_claim' else [record]
        for source in sources:
            ref = {key: source[key] for key in ('collection_id', 'document_id', 'revision_id', 'chunk_id')}
            if ref not in refs:
                refs.append(ref)
    from potluck_local.services.reviewed_knowledge import propose_claim
    return await propose_claim(scope.workspace, scope.collection_ids, claim_key, content, refs,
                               origin_investigation_id=investigation_id,
                               origin_citation_ids=citation_ids)


def _read_json(path: str) -> Any:
    return json.loads(Path(path).read_text(encoding='utf-8'))


def main() -> None:
    parser = argparse.ArgumentParser(description='Local owner investigation evaluation tools')
    parser.add_argument('--workspace', required=True)
    parser.add_argument('--collection', action='append', required=True)
    commands = parser.add_subparsers(dest='command', required=True)
    proposal = commands.add_parser('propose-case')
    proposal.add_argument('investigation_id')
    proposal.add_argument('--confirm', action='store_true', required=True)
    validation = commands.add_parser('validate-case')
    validation.add_argument('case_json')
    comparison = commands.add_parser('compare')
    comparison.add_argument('manifest_json')
    comparison.add_argument('baseline_json')
    comparison.add_argument('candidate_json')
    comparison.add_argument('--synthetic', action='store_true',
                            help='Compare explicit synthetic labels without source validation')
    knowledge = commands.add_parser('propose-knowledge')
    knowledge.add_argument('investigation_id')
    knowledge.add_argument('--key', required=True)
    knowledge.add_argument('--content-file', required=True)
    knowledge.add_argument('--citation', action='append', required=True)
    knowledge.add_argument('--confirm', action='store_true', required=True)
    args = parser.parse_args()
    scope = EvidenceScope(args.workspace, tuple(args.collection))

    async def execute() -> Any:
        if args.command == 'propose-case':
            return await propose_evaluation_case(scope, args.investigation_id, owner_confirmed=args.confirm)
        if args.command == 'validate-case':
            return {'valid': await validate_evaluation_case(scope, _read_json(args.case_json))}
        if args.command == 'compare':
            if args.synthetic:
                return compare_evaluations(_read_json(args.manifest_json),
                                           _read_json(args.baseline_json),
                                           _read_json(args.candidate_json))
            return await compare_reviewed_evaluations(
                scope, _read_json(args.manifest_json), _read_json(args.baseline_json),
                _read_json(args.candidate_json))
        claim_id = await propose_knowledge_update(
            scope, args.investigation_id, args.key,
            Path(args.content_file).read_text(encoding='utf-8'), args.citation,
            owner_confirmed=args.confirm)
        return {'claim_id': claim_id, 'status': 'proposed'}

    try:
        print(json.dumps(asyncio.run(execute()), indent=2))
    except Exception:
        parser.exit(1, "Evaluation operation failed; check inputs and current scope.\n")


if __name__ == '__main__':
    main()
