"""Opt-in local regression artifacts with explicit, content-bound reviewer judgments.

No inference, automatic semantic grading, database writes or source approval.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import os
import stat
import re
from pathlib import Path, PurePosixPath
from typing import Any

import aiosqlite

from potluck_local.services.evidence_lookup import EvidenceScope
from potluck_local.services.investigations import get_investigation
from potluck_local.services.memory_db import get_memory_db_path

CASE = 'company-brain-rubric-case-v1'
CANDIDATE = 'company-brain-original-candidate-v1'
ASSESSMENT = 'company-brain-criterion-assessment-v1'
REPORT = 'company-brain-criterion-report-v1'
MAX_BYTES = 4 * 1024 * 1024


def canonical_sha256(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                     allow_nan=False).encode('utf-8')
    if len(raw) > MAX_BYTES:
        raise ValueError('Regression artifact exceeds the size limit')
    return hashlib.sha256(raw).hexdigest()


def _keys(value: Any, keys: str) -> None:
    if not isinstance(value, dict) or set(value) != set(keys.split()):
        raise ValueError('Invalid regression artifact fields')


def _text(value: Any, limit: int = 8000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError('Expected bounded nonempty text')
    return value


def _hash(value: Any) -> str:
    if not isinstance(value, str) or re.fullmatch(r'[a-f0-9]{64}', value) is None:
        raise ValueError('Expected a canonical SHA256 digest')
    return value


def _ids(value: Any) -> list[str]:
    if (not isinstance(value, list) or not value or len(value) > 100
            or any(not isinstance(x, str) or not x.strip() or len(x) > 512 for x in value)
            or len(set(value)) != len(value)):
        raise ValueError('Expected unique nonempty IDs')
    return value


def _review(value: Any) -> None:
    _keys(value, 'type identity relationship notes')
    if value['type'] not in ('human', 'agent') or value['relationship'] not in (
            'rubric_author', 'separate_reviewer'):
        raise ValueError('Declare reviewer type and relationship to the rubric')
    _text(value['identity'], 256)
    _text(value['notes'])


def _record(item: dict[str, Any]) -> dict[str, Any]:
    fields = 'id workspace collection_ids issue model_slug model_sha status brief evidence review outcome provenance'
    result = {k: copy.deepcopy(item[k]) for k in fields.split()}
    _ids(result['collection_ids'])
    result['collection_ids'].sort()
    _hash(result['model_sha'])
    if result['status'] not in ('draft', 'reviewed'):
        raise ValueError('Only available saved answers can be evaluated')
    for field in ('id', 'workspace', 'issue', 'model_slug'):
        _text(result[field])
    if (not isinstance(result['brief'], dict) or not isinstance(result['provenance'], dict)
            or not isinstance(result['evidence'], list) or not result['evidence']):
        raise ValueError('Saved answer, evidence and provenance required')
    provenance = result['provenance']
    if (provenance.get('model_slug') != result['model_slug']
            or provenance.get('model_sha256') != result['model_sha']
            or not isinstance(provenance.get('generation_settings'), dict)):
        raise ValueError('Candidate model identity and generation settings must be pinned')
    _text(provenance.get('template_version'), 256)
    _ids([e['citation_id'] for e in result['evidence']])
    canonical_sha256(result)
    return result


def _proposal(item: dict[str, Any]) -> dict[str, Any]:
    if item['status'] != 'reviewed' or not item['review'] or not item['outcome']:
        raise ValueError('Recorded review and outcome required for the reference')
    return {'investigation_id': item['id'], 'workspace': item['workspace'],
            'collection_ids': item['collection_ids'],
            'citation_ids': sorted(e['citation_id'] for e in item['evidence']),
            'review_decision': item['review']['decision'],
            'outcome_result': item['outcome']['result'], 'model_slug': item['model_slug'],
            'model_sha': item['model_sha'], 'status': 'proposed'}


def _context(item: dict[str, Any]) -> str:
    # Preserve the full ordered evidence packet, including source revisions and bytes.
    return canonical_sha256({k: item[k] for k in ('workspace', 'collection_ids', 'issue', 'evidence')})


def _file_matches(root: Path, relative: str, expected_sha256: str) -> bool:
    """Read only a registered regular file, anchored through non-symlink directories."""
    from potluck_local.services.source_collections import MAX_FILE_BYTES
    parts = PurePosixPath(relative).parts
    if (os.open not in os.supports_dir_fd or not hasattr(os, 'O_NOFOLLOW')
            or not parts or PurePosixPath(relative).as_posix() != relative
            or PurePosixPath(relative).is_absolute() or '\\' in relative
            or any(p in ('.', '..') for p in parts)
            or root.resolve() != root or not root.is_dir()):
        return False
    opened = []
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY
    try:
        directory = os.open(root, flags)
        opened.append(directory)
        root_identity = os.fstat(directory)
        for part in parts[:-1]:
            directory = os.open(part, flags, dir_fd=directory)
            opened.append(directory)
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(fd, 'rb') as source:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode):
                return False
            raw = source.read(MAX_FILE_BYTES + 1)
            after = os.fstat(source.fileno())
        path = root.joinpath(*parts)
        if (len(raw) > MAX_FILE_BYTES or path.resolve() != path
                or (root.stat().st_dev, root.stat().st_ino) != (root_identity.st_dev, root_identity.st_ino)):
            return False
        current = path.stat(follow_symlinks=False)
        identity = lambda v: (v.st_dev, v.st_ino, v.st_size, v.st_mtime_ns, v.st_ctime_ns)
        return identity(before) == identity(after) == identity(current) and hashlib.sha256(raw).hexdigest() == expected_sha256
    except (OSError, ValueError):
        return False
    finally:
        for fd in reversed(opened):
            os.close(fd)


async def _files_current(scope: EvidenceScope, item: dict[str, Any]) -> None:
    sources = [source for entry in item['evidence']
               for source in (entry['sources'] if entry.get('kind') == 'reviewed_claim' else [entry])]
    refs = {(s['collection_id'], s['document_id'], s['revision_id']) for s in sources}
    if not refs or len(refs) > 100:
        raise ValueError('Bounded source file identities required')
    uri = get_memory_db_path().resolve().as_uri() + '?mode=ro'
    async with aiosqlite.connect(uri, uri=True) as db:
        for collection, document, revision in refs:
            if collection not in scope.collection_ids:
                raise ValueError('Source file is outside the selected scope')
            cursor = await db.execute(
                'SELECT c.root_path,d.relative_path,d.content_sha FROM source_collections c '
                'JOIN source_documents d ON d.collection_id=c.id WHERE c.workspace=? '
                'AND c.id=? AND d.id=? AND d.revision_id=? AND c.scan_status=?',
                (scope.workspace, collection, document, revision, 'ready'))
            row = await cursor.fetchone()
            if not row or not await asyncio.to_thread(_file_matches, Path(row[0]), row[1], row[2]):
                raise ValueError('A source file changed or is unavailable; scan and review before reuse')


async def _read(scope: EvidenceScope, iid: str) -> dict[str, Any]:
    item = _record(await get_investigation(scope, iid))
    if (item['id'] != iid or item['workspace'] != scope.workspace
            or item['collection_ids'] != sorted(scope.collection_ids)):
        raise ValueError('Saved record scope does not match')
    await _files_current(scope, item)
    return item


def _criteria(criteria: Any, evidence: list[dict[str, Any]]) -> None:
    if not isinstance(criteria, list) or not 1 <= len(criteria) <= 50:
        raise ValueError('Provide one to fifty explicit criteria')
    _ids([c.get('id') if isinstance(c, dict) else None for c in criteria])
    content = {e['citation_id']: e['content'] for e in evidence}
    for criterion in criteria:
        _keys(criterion, 'id kind requirement source_excerpts')
        if criterion['kind'] not in ('semantic', 'citation', 'clarity'):
            raise ValueError('Invalid criterion kind')
        _text(criterion['requirement'])
        excerpts = criterion['source_excerpts']
        if not isinstance(excerpts, list) or not 1 <= len(excerpts) <= 20:
            raise ValueError('Source excerpts required for every criterion')
        for excerpt in excerpts:
            _keys(excerpt, 'citation_id quote')
            quote = _text(excerpt['quote'])
            if excerpt['citation_id'] not in content or quote not in content[excerpt['citation_id']]:
                raise ValueError('Rubric quote does not match the supplied source')
        pairs = [(e['citation_id'], e['quote']) for e in excerpts]
        if len(set(pairs)) != len(pairs):
            raise ValueError('Duplicate rubric source excerpts')


async def freeze_case(scope: EvidenceScope, proposal: dict[str, Any], rubric: dict[str, Any],
                      *, confirmed: bool) -> dict[str, Any]:
    if confirmed is not True:
        raise PermissionError('Explicit rubric confirmation required')
    _keys(rubric, 'case_id criteria review')
    _text(rubric['case_id'], 128)
    _review(rubric['review'])
    item = await _read(scope, proposal['investigation_id'])
    if proposal != _proposal(item):
        raise ValueError('Reference does not match the current saved record')
    _criteria(rubric['criteria'], item['evidence'])
    case = {'schema': CASE, 'case_id': rubric['case_id'], 'proposal': copy.deepcopy(proposal),
            'question': item['issue'], 'context_sha256': _context(item),
            'reference_record_sha256': canonical_sha256(item),
            'criteria': copy.deepcopy(rubric['criteria']), 'rubric_review': copy.deepcopy(rubric['review'])}
    if canonical_sha256(await _read(scope, item['id'])) != case['reference_record_sha256']:
        raise ValueError('Reference changed during rubric freezing')
    return case


async def validate_current_case(scope: EvidenceScope, case: dict[str, Any], case_sha256: str) -> None:
    _keys(case, 'schema case_id proposal question context_sha256 reference_record_sha256 criteria rubric_review')
    if case['schema'] != CASE or canonical_sha256(case) != _hash(case_sha256):
        raise ValueError('Case does not match the trusted frozen digest')
    rubric = {'case_id': case['case_id'], 'criteria': case['criteria'], 'review': case['rubric_review']}
    current = await freeze_case(scope, case['proposal'], rubric, confirmed=True)
    if current != case:
        raise ValueError('Frozen reference or evidence is no longer current')


async def snapshot_candidate(scope: EvidenceScope, case: dict[str, Any], case_sha256: str,
                             investigation_id: str) -> dict[str, Any]:
    await validate_current_case(scope, case, case_sha256)
    item = await _read(scope, investigation_id)
    if _context(item) != case['context_sha256']:
        raise ValueError('Candidate question or supplied evidence differs from the frozen case')
    candidate = {'schema': CANDIDATE, 'case_sha256': case_sha256,
                 'investigation_id': investigation_id, 'record_sha256': canonical_sha256(item),
                 'context_sha256': _context(item), 'model_slug': item['model_slug'],
                 'model_sha256': item['model_sha'], 'provenance': item['provenance'],
                 'original_brief': item['brief']}
    if canonical_sha256(await _read(scope, investigation_id)) != candidate['record_sha256']:
        raise ValueError('Candidate changed while taking the snapshot')
    await validate_current_case(scope, case, case_sha256)
    return candidate


def _answer_excerpt(candidate: dict[str, Any], excerpt: Any) -> set[str]:
    _keys(excerpt, 'path quote')
    path = _text(excerpt['path'], 256)
    match = re.fullmatch(r'/(answers|facts|prior_fixes|hypotheses|recommended_checks)/(0|[1-9][0-9]{0,3})/text', path)
    if not match:
        raise ValueError('Use an original answer text path')
    section, index = match.groups()
    brief = candidate['original_brief']
    entries = brief.get(section) if isinstance(brief, dict) else None
    if not isinstance(entries, list) or int(index) >= len(entries):
        raise ValueError('Answer text path is unavailable')
    entry = entries[int(index)]
    if not isinstance(entry, dict) or not isinstance(entry.get('text'), str) or _text(excerpt['quote']) not in entry['text']:
        raise ValueError('Answer quote does not match its original text field')
    if 'snippet_ids' not in entry:
        return set(_ids(entry['citation_ids'])) if entry.get('citation_ids') else set()
    selected = entry['snippet_ids']
    if selected != []:
        _ids(selected)
    catalog = candidate['provenance'].get('snippet_catalog')
    if not isinstance(catalog, list):
        raise ValueError('Original snippet catalog is required')
    by_id = {}
    for snippet in catalog:
        _keys(snippet, 'snippet_id citation_id quote')
        sid = _text(snippet['snippet_id'], 512)
        _text(snippet['citation_id'], 512)
        _text(snippet['quote'])
        if sid in by_id:
            raise ValueError('Duplicate original snippet IDs')
        by_id[sid] = snippet
    if any(sid not in by_id for sid in selected):
        raise ValueError('Answer selected an unavailable snippet')
    expected = [by_id[sid] for sid in selected]
    if entry.get('source_excerpts') != expected:
        raise ValueError('Answer source excerpts do not match selected original snippets')
    citations = {snippet['citation_id'] for snippet in expected}
    if 'citation_ids' in entry and set(_ids(entry['citation_ids'])) != citations:
        raise ValueError('Ambiguous answer citation bindings')
    return citations


def assess_artifacts(case: dict[str, Any], case_sha256: str, candidate: dict[str, Any],
                     candidate_sha256: str, assessment: dict[str, Any]) -> dict[str, Any]:
    """Validate bindings and summarize explicit judgments, with no current-source claim."""
    _hash(case_sha256)
    _hash(candidate_sha256)
    if canonical_sha256(case) != case_sha256 or canonical_sha256(candidate) != candidate_sha256:
        raise ValueError('Artifacts differ from trusted frozen digests')
    _keys(case, 'schema case_id proposal question context_sha256 reference_record_sha256 criteria rubric_review')
    _keys(candidate, 'schema case_sha256 investigation_id record_sha256 context_sha256 model_slug model_sha256 provenance original_brief')
    if (case['schema'] != CASE or candidate['schema'] != CANDIDATE
            or candidate['case_sha256'] != case_sha256
            or candidate['context_sha256'] != case['context_sha256']):
        raise ValueError('Candidate does not belong to this case')
    _hash(candidate['model_sha256'])
    _review(case['rubric_review'])
    _keys(assessment, 'schema case_sha256 candidate_sha256 reviewer judgments')
    if (assessment['schema'] != ASSESSMENT or assessment['case_sha256'] != case_sha256
            or assessment['candidate_sha256'] != candidate_sha256):
        raise ValueError('Assessment does not match the frozen case and candidate')
    _review(assessment['reviewer'])
    if assessment['reviewer']['identity'] == case['rubric_review']['identity'] and assessment['reviewer']['relationship'] != 'rubric_author':
        raise ValueError('The rubric author must declare that relationship')
    criteria = case['criteria']
    _ids([c['id'] for c in criteria])
    judgments = assessment['judgments']
    if not isinstance(judgments, list):
        raise ValueError('Explicit criterion judgments required')
    ids = _ids([j.get('criterion_id') if isinstance(j, dict) else None for j in judgments])
    if set(ids) != {c['id'] for c in criteria}:
        raise ValueError('Judge every frozen criterion exactly once')
    by_id = {c['id']: c for c in criteria}
    for judgment in judgments:
        _keys(judgment, 'criterion_id verdict rationale citation_ids answer_excerpts')
        verdict = judgment['verdict']
        if verdict not in ('pass', 'fail', 'unknown'):
            raise ValueError('Invalid criterion verdict')
        _text(judgment['rationale'])
        excerpts = judgment['answer_excerpts']
        if not isinstance(excerpts, list) or len(excerpts) > 20:
            raise ValueError('Invalid answer excerpts')
        cited: set[str] = set()
        for excerpt in excerpts:
            cited |= _answer_excerpt(candidate, excerpt)
        selected = judgment['citation_ids']
        if not isinstance(selected, list) or (selected and _ids(selected) != selected):
            raise ValueError('Invalid judgment citations')
        expected = {e['citation_id'] for e in by_id[judgment['criterion_id']]['source_excerpts']}
        if not set(selected) <= expected:
            raise ValueError('Judgment cites a source outside its criterion')
        if verdict == 'pass' and (not excerpts or set(selected) != expected or not expected <= cited):
            raise ValueError('Passing judgments need quoted answer text and every criterion source citation')
    counts = {v: sum(j['verdict'] == v for j in judgments) for v in ('pass', 'fail', 'unknown')}
    status = 'fail' if counts['fail'] else 'incomplete' if counts['unknown'] else 'pass'
    return {'schema': REPORT, 'case_id': case['case_id'], 'case_sha256': case_sha256,
            'candidate_sha256': candidate_sha256, 'assessment_sha256': canonical_sha256(assessment),
            'status': status, 'criterion_count': len(criteria), 'counts': counts,
            'all_criteria_pass_rate': counts['pass'] / len(criteria),
            'reviewer': copy.deepcopy(assessment['reviewer']),
            'rubric_review': copy.deepcopy(case['rubric_review']),
            'source_validation': 'archive_only', 'answer_variant': 'saved_original_brief',
            'semantic_verification': 'Explicit reviewer judgments; quote checks verify location only.',
            'verified_independence': False, 'owner_acceptance_inferred': False,
            'model_slug': candidate['model_slug'], 'model_sha256': candidate['model_sha256'],
            'candidate_provenance_sha256': canonical_sha256(candidate['provenance']),
            'judgments': copy.deepcopy(judgments)}


async def assess_current(scope: EvidenceScope, case: dict[str, Any], case_sha256: str,
                         candidate: dict[str, Any], candidate_sha256: str,
                         assessment: dict[str, Any]) -> dict[str, Any]:
    report = assess_artifacts(case, case_sha256, candidate, candidate_sha256, assessment)
    current = await snapshot_candidate(scope, case, case_sha256, candidate['investigation_id'])
    if current != candidate:
        raise ValueError('Candidate record changed since its snapshot')
    report['source_validation'] = 'current_scoped_records_revalidated'
    return report


def _read_json(path: str) -> Any:
    def pairs(rows):
        result = {}
        for key, value in rows:
            if key in result:
                raise ValueError('Duplicate JSON keys')
            result[key] = value
        return result
    with Path(path).open('rb') as handle:
        raw = handle.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError('Artifact too large')
    return json.loads(raw, object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite JSON')))


def _read_artifact(path: str) -> Any:
    value = _read_json(path)
    if isinstance(value, dict) and set(value) == {'sha256', 'artifact'}:
        if canonical_sha256(value['artifact']) != _hash(value['sha256']):
            raise ValueError('Artifact envelope digest mismatch')
        return value['artifact']
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description='Local explicit Company Brain regression reviews')
    parser.add_argument('--workspace', required=True)
    parser.add_argument('--collection', action='append', required=True)
    commands = parser.add_subparsers(dest='command', required=True)
    freeze = commands.add_parser('freeze')
    freeze.add_argument('proposal_json')
    freeze.add_argument('rubric_json')
    freeze.add_argument('--confirm', action='store_true', required=True)
    for name in ('snapshot', 'assess'):
        command = commands.add_parser(name)
        command.add_argument('case_json')
        command.add_argument('--case-sha256', required=True)
        if name == 'snapshot':
            command.add_argument('investigation_id')
        else:
            command.add_argument('candidate_json')
            command.add_argument('assessment_json')
            command.add_argument('--candidate-sha256', required=True)
    args = parser.parse_args()
    scope = EvidenceScope(args.workspace, tuple(args.collection))

    async def execute():
        if args.command == 'freeze':
            return await freeze_case(scope, _read_artifact(args.proposal_json),
                                     _read_artifact(args.rubric_json), confirmed=args.confirm)
        case = _read_artifact(args.case_json)
        if args.command == 'snapshot':
            return await snapshot_candidate(scope, case, args.case_sha256, args.investigation_id)
        return await assess_current(scope, case, args.case_sha256, _read_artifact(args.candidate_json),
                                    args.candidate_sha256, _read_artifact(args.assessment_json))
    try:
        result = asyncio.run(execute())
        print(json.dumps({'sha256': canonical_sha256(result), 'artifact': result}, indent=2,
                         ensure_ascii=False, allow_nan=False))
        if args.command == 'assess' and result['status'] != 'pass':
            parser.exit(2)
    except Exception:
        parser.exit(1, 'Regression operation failed; check frozen inputs and current source scope.\n')


if __name__ == '__main__':
    main()
