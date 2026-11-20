"""Scoring checks exercise content binding, not automatic semantic judgment."""
import copy
import json
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from potluck_local.services import investigation_regression as regression
from potluck_local.services.evidence_lookup import EvidenceScope

SCOPE = EvidenceScope('team', ('c',))


def record(iid='original'):
    return {'id': iid, 'workspace': 'team', 'collection_ids': ['c'],
            'issue': 'When may we release the seed transfer?', 'model_slug': 'owned',
            'model_sha': 'a' * 64, 'status': 'reviewed',
            'brief': {'answers': [{'text': 'Release after owner approval; urgent requests still need a signature.',
                                    'citation_ids': ['claim:policy']}], 'facts': [],
                      'prior_fixes': [], 'hypotheses': [], 'recommended_checks': [],
                      'questions': ['When?'], 'model_uncertainties': []},
            'evidence': [{'citation_id': 'claim:policy',
                          'content': 'Release after owner approval. Urgent requests still need a signature.',
                          'sources': [{'revision_id': 'rev1', 'content': 'Owner approval and signature.'}]}],
            'review': {'decision': 'reject', 'reviewer': 'Historical reviewer', 'reason': 'Earlier presentation issue.'},
            'outcome': {'result': 'inconclusive', 'notes': 'No business result observed.'},
            'provenance': {'model_slug': 'owned', 'model_sha256': 'a' * 64,
                           'generation_settings': {'temperature': 0}, 'template_version': 'fixed'}}


def reviewer(identity='Source assessor', relationship='rubric_author'):
    return {'type': 'agent', 'identity': identity, 'relationship': relationship,
            'notes': 'Source-aware unblinded test assessment, no verified independence.'}


def rubric():
    return {'case_id': 'approval', 'review': reviewer(), 'criteria': [
        {'id': 'prerequisite', 'kind': 'semantic', 'requirement': 'Retain owner approval before release.',
         'source_excerpts': [{'citation_id': 'claim:policy', 'quote': 'Release after owner approval.'}]},
        {'id': 'exception', 'kind': 'semantic', 'requirement': 'Urgency does not waive the signature.',
         'source_excerpts': [{'citation_id': 'claim:policy', 'quote': 'Urgent requests still need a signature.'}]}]}


def judgments(case, candidate):
    return {'schema': regression.ASSESSMENT, 'case_sha256': regression.canonical_sha256(case),
            'candidate_sha256': regression.canonical_sha256(candidate), 'reviewer': reviewer(),
            'judgments': [{'criterion_id': criterion['id'], 'verdict': 'pass',
                           'rationale': 'The cited answer explicitly retains this source requirement.',
                           'citation_ids': ['claim:policy'],
                           'answer_excerpts': [{'path': '/answers/0/text',
                                                'quote': 'Release after owner approval; urgent requests still need a signature.'}]}
                          for criterion in case['criteria']]}


@pytest_asyncio.fixture
async def artifacts(monkeypatch):
    items = {'original': record(), 'candidate': record('candidate')}
    items['candidate']['status'] = 'draft'
    items['candidate']['review'] = None
    items['candidate']['outcome'] = None
    # A future candidate can use another pinned model with its own exact provenance.
    items['candidate']['model_sha'] = 'b' * 64
    items['candidate']['model_slug'] = 'candidate-model'
    items['candidate']['provenance'].update(model_sha256='b' * 64, model_slug='candidate-model')
    async def get(scope, iid):
        assert scope == SCOPE
        return copy.deepcopy(items[iid])
    getter = AsyncMock(side_effect=get)
    monkeypatch.setattr(regression, 'get_investigation', getter)
    monkeypatch.setattr(regression, '_files_current', AsyncMock())
    case = await regression.freeze_case(SCOPE, regression._proposal(regression._record(items['original'])), rubric(), confirmed=True)
    candidate = await regression.snapshot_candidate(SCOPE, case, regression.canonical_sha256(case), 'candidate')
    return items, getter, case, candidate


def assess(case, candidate, review):
    return regression.assess_artifacts(case, regression.canonical_sha256(case), candidate,
                                       regression.canonical_sha256(candidate), review)


@pytest.mark.asyncio
async def test_explicit_assessment_preserves_rejection_and_does_not_infer_independence(artifacts):
    items, getter, case, candidate = artifacts
    report = await regression.assess_current(SCOPE, case, regression.canonical_sha256(case),
                                             candidate, regression.canonical_sha256(candidate), judgments(case, candidate))
    assert report['status'] == 'pass'
    assert report['counts'] == {'pass': 2, 'fail': 0, 'unknown': 0}
    assert report['source_validation'] == 'current_scoped_records_revalidated'
    assert report['verified_independence'] is False
    assert report['owner_acceptance_inferred'] is False
    assert report['answer_variant'] == 'saved_original_brief'
    assert report['model_sha256'] == 'b' * 64
    assert items['original']['review']['decision'] == 'reject'
    assert items['candidate']['review'] is None
    assert getter.await_count >= 10


@pytest.mark.asyncio
async def test_no_assessment_from_valid_reference_or_outcome(artifacts):
    _, _, case, candidate = artifacts
    review = judgments(case, candidate)
    for row in review['judgments']:
        row.update(verdict='unknown', rationale='Not assessed yet.', citation_ids=[], answer_excerpts=[])
    result = assess(case, candidate, review)
    assert result['status'] == 'incomplete' and result['all_criteria_pass_rate'] == 0
    review['judgments'][0].update(verdict='fail', rationale='Prerequisite omitted.')
    assert assess(case, candidate, review)['status'] == 'fail'


@pytest.mark.asyncio
async def test_required_confirmation_precedes_reference_read(monkeypatch):
    getter = AsyncMock()
    monkeypatch.setattr(regression, 'get_investigation', getter)
    with pytest.raises(PermissionError):
        await regression.freeze_case(SCOPE, {}, rubric(), confirmed=False)
    getter.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('field', ['issue', 'brief', 'evidence', 'review', 'outcome', 'provenance'])
async def test_same_ids_cannot_hide_changed_reference_fields(artifacts, field):
    items, _, case, _ = artifacts
    value = items['original'][field]
    if isinstance(value, str):
        items['original'][field] += 'changed'
    elif isinstance(value, list):
        value[0]['sources'][0]['revision_id'] = 'rev2'
    else:
        value['changed'] = True
    with pytest.raises(ValueError):
        await regression.validate_current_case(SCOPE, case, regression.canonical_sha256(case))


@pytest.mark.asyncio
async def test_trusted_hash_rejects_rewritten_rubric_and_transplanted_labels(artifacts):
    _, _, case, candidate = artifacts
    review = judgments(case, candidate)
    old_case_sha = regression.canonical_sha256(case)
    old_candidate_sha = regression.canonical_sha256(candidate)
    case['criteria'][0]['requirement'] = 'Approve everything'
    with pytest.raises(ValueError, match='trusted'):
        regression.assess_artifacts(case, old_case_sha, candidate, old_candidate_sha, review)
    case['criteria'][0]['requirement'] = rubric()['criteria'][0]['requirement']
    candidate['original_brief']['answers'][0]['text'] = 'Release immediately without approval.'
    with pytest.raises(ValueError, match='trusted'):
        regression.assess_artifacts(case, regression.canonical_sha256(case), candidate, old_candidate_sha, review)
    with pytest.raises(ValueError, match='Assessment does not match'):
        assess(case, candidate, review)


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['missing', 'duplicate', 'extra', 'missing-rationale', 'verdict', 'extra-fields', 'foreign-citation', 'empty-pass', 'unattached-citation', 'bad-quote', 'metadata', 'wrong-field', 'negative-index', 'leading-zero', 'large-index'])
async def test_bad_judgments_never_score_as_success(artifacts, mutation):
    _, _, case, candidate = artifacts
    review = judgments(case, candidate)
    row = review['judgments'][0]
    if mutation == 'missing': review['judgments'].pop()
    elif mutation == 'duplicate': review['judgments'].append(copy.deepcopy(row))
    elif mutation == 'extra': review['judgments'].append({**row, 'criterion_id': 'extra'})
    elif mutation == 'missing-rationale': row['rationale'] = ' '
    elif mutation == 'verdict': row['verdict'] = True
    elif mutation == 'extra-fields': row['score'] = 100
    elif mutation == 'foreign-citation': row['citation_ids'] = ['claim:other']
    elif mutation == 'empty-pass': row['answer_excerpts'] = []
    elif mutation == 'unattached-citation': row['citation_ids'] = []
    elif mutation == 'bad-quote': row['answer_excerpts'][0]['quote'] = 'Not present'
    else:
        row['answer_excerpts'][0]['path'] = {'metadata': '/questions/0/text', 'wrong-field': '/answers/0/answer',
            'negative-index': '/answers/-1/text', 'leading-zero': '/answers/00/text', 'large-index': '/answers/99999/text'}[mutation]
    with pytest.raises(ValueError):
        assess(case, candidate, review)


@pytest.mark.asyncio
async def test_omission_can_fail_without_fabricating_a_quote(artifacts):
    _, _, case, candidate = artifacts
    review = judgments(case, candidate)
    review['judgments'][1].update(verdict='fail', rationale='The exception is absent; the reviewer inspected all answer text.',
                                  answer_excerpts=[], citation_ids=[])
    report = assess(case, candidate, review)
    assert report['status'] == 'fail' and report['all_criteria_pass_rate'] == .5
    assert report['source_validation'] == 'archive_only'


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['source-quote', 'source-citation', 'duplicate', 'empty', 'independent'])
async def test_rubric_integrity_is_checked(artifacts, mutation):
    items, _, _, _ = artifacts
    r = rubric()
    if mutation == 'source-quote': r['criteria'][0]['source_excerpts'][0]['quote'] = 'Urgency waives approval.'
    elif mutation == 'source-citation': r['criteria'][0]['source_excerpts'][0]['citation_id'] = 'claim:other'
    elif mutation == 'duplicate': r['criteria'].append(copy.deepcopy(r['criteria'][0]))
    elif mutation == 'empty': r['criteria'] = []
    else: r['review']['independent'] = True
    with pytest.raises(ValueError):
        await regression.freeze_case(SCOPE, regression._proposal(regression._record(items['original'])), r, confirmed=True)


@pytest.mark.asyncio
async def test_stale_or_revoked_records_block_current_reporting(artifacts, monkeypatch):
    _, _, case, candidate = artifacts
    monkeypatch.setattr(regression, 'get_investigation', AsyncMock(side_effect=LookupError('Revoked')))
    with pytest.raises(LookupError):
        await regression.assess_current(SCOPE, case, regression.canonical_sha256(case), candidate,
                                        regression.canonical_sha256(candidate), judgments(case, candidate))


@pytest.mark.asyncio
async def test_candidate_question_evidence_model_and_provenance_cannot_drift(artifacts):
    items, _, case, candidate = artifacts
    items['candidate']['brief']['answers'][0]['text'] = 'A different reply'
    with pytest.raises(ValueError, match='changed since'):
        await regression.assess_current(SCOPE, case, regression.canonical_sha256(case), candidate,
                                        regression.canonical_sha256(candidate), judgments(case, candidate))
    items['candidate']['issue'] = 'Another question'
    with pytest.raises(ValueError, match='differs'):
        await regression.snapshot_candidate(SCOPE, case, regression.canonical_sha256(case), 'candidate')
    items['candidate']['issue'] = case['question']
    items['candidate']['provenance']['model_sha256'] = 'c' * 64
    with pytest.raises(ValueError, match='identity'):
        await regression.snapshot_candidate(SCOPE, case, regression.canonical_sha256(case), 'candidate')


@pytest.mark.asyncio
async def test_mutation_during_double_read_is_not_frozen(artifacts, monkeypatch):
    items, _, _, _ = artifacts
    first, second = copy.deepcopy(items['original']), copy.deepcopy(items['original'])
    second['review']['reason'] = 'Changed during freeze'
    monkeypatch.setattr(regression, 'get_investigation', AsyncMock(side_effect=[first, second]))
    with pytest.raises(ValueError, match='changed during'):
        await regression.freeze_case(SCOPE, regression._proposal(regression._record(first)), rubric(), confirmed=True)


@pytest.mark.parametrize('raw', ['{"x":1,"x":2}', '{"x":{"y":1,"y":2}}', '{"x":NaN}', '{"x":Infinity}'])
def test_strict_json_never_silently_changes_meaning(tmp_path, raw):
    file = tmp_path / 'artifact.json'; file.write_text(raw)
    with pytest.raises(ValueError): regression._read_json(str(file))


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', [None, 'unselected', 'missing-selected', 'wrong-excerpt', 'duplicate-snippet', 'wrong-citation'])
async def test_actual_fixed_part_text_and_selected_snippet_bindings(artifacts, mutation):
    items, _, case, _ = artifacts
    item = items['candidate']
    snippet = {'snippet_id': 'S1', 'citation_id': 'claim:policy',
               'quote': 'Release after owner approval.'}
    item['provenance']['snippet_catalog'] = [snippet]
    entry = item['brief']['answers'][0]
    entry.pop('citation_ids')
    entry.update(snippet_ids=['S1'], source_excerpts=[copy.deepcopy(snippet)])
    if mutation == 'unselected': entry['snippet_ids'] = []
    elif mutation == 'missing-selected': entry['snippet_ids'] = ['S2']
    elif mutation == 'wrong-excerpt': entry['source_excerpts'][0]['quote'] = 'Not the original excerpt'
    elif mutation == 'duplicate-snippet': item['provenance']['snippet_catalog'].append(copy.deepcopy(snippet))
    elif mutation == 'wrong-citation': entry['source_excerpts'][0]['citation_id'] = 'claim:other'
    candidate = await regression.snapshot_candidate(SCOPE, case, regression.canonical_sha256(case), 'candidate')
    review = judgments(case, candidate)
    if mutation:
        with pytest.raises(ValueError): assess(case, candidate, review)
    else:
        assert assess(case, candidate, review)['status'] == 'pass'


def test_sealed_envelope_round_trip_and_tampering(tmp_path):
    data = {'example': 'Unicode café, two lines\nsecond line'}
    path = tmp_path / 'sealed.json'
    path.write_text(json.dumps({'sha256': regression.canonical_sha256(data), 'artifact': data}))
    assert regression._read_artifact(str(path)) == data
    path.write_text(json.dumps({'sha256': 'a' * 64, 'artifact': data}))
    with pytest.raises(ValueError, match='envelope'): regression._read_artifact(str(path))


@pytest.mark.asyncio
async def test_nonexistent_allowed_text_index_does_not_pass(artifacts):
    _, _, case, candidate = artifacts
    review = judgments(case, candidate)
    review['judgments'][0]['answer_excerpts'][0]['path'] = '/answers/3/text'
    with pytest.raises(ValueError, match='unavailable'): assess(case, candidate, review)


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['workspace', 'collections', 'status', 'missing-config', 'bad-model'])
async def test_candidate_scope_availability_and_pins_are_required(artifacts, change):
    items, _, case, _ = artifacts
    item = items['candidate']
    if change == 'workspace': item['workspace'] = 'another-team'
    elif change == 'collections': item['collection_ids'] = ['other-folder']
    elif change == 'status': item['status'] = 'error'
    elif change == 'missing-config': item['provenance'].pop('generation_settings')
    else: item['model_sha'] = 'not-a-model-hash'
    with pytest.raises(ValueError):
        await regression.snapshot_candidate(SCOPE, case, regression.canonical_sha256(case), 'candidate')


@pytest.mark.asyncio
async def test_quotes_retain_exact_unicode_and_newlines(artifacts):
    items, _, case, _ = artifacts
    items['candidate']['brief']['answers'][0]['text'] = 'Approval café\nSignature required.'
    candidate = await regression.snapshot_candidate(SCOPE, case, regression.canonical_sha256(case), 'candidate')
    review = judgments(case, candidate)
    for row in review['judgments']:
        row['answer_excerpts'] = [{'path': '/answers/0/text', 'quote': 'café\nSignature'}]
    assert assess(case, candidate, review)['status'] == 'pass'
    review['judgments'][0]['answer_excerpts'][0]['quote'] = 'cafe\u0301\nSignature'
    with pytest.raises(ValueError): assess(case, candidate, review)


@pytest.mark.parametrize('verdict,exit_code', [('pass', 0), ('fail', 2), ('unknown', 2)])
def test_cli_round_trip_envelopes_and_nonpassing_exit_codes(tmp_path, monkeypatch, capsys, verdict, exit_code):
    import asyncio
    import sys
    item = record()
    monkeypatch.setattr(regression, 'get_investigation', AsyncMock(return_value=item))
    monkeypatch.setattr(regression, '_files_current', AsyncMock())
    case = asyncio.run(regression.freeze_case(SCOPE, regression._proposal(regression._record(item)), rubric(), confirmed=True))
    candidate = asyncio.run(regression.snapshot_candidate(SCOPE, case, regression.canonical_sha256(case), 'original'))
    review = judgments(case, candidate)
    if verdict != 'pass':
        review['judgments'][0].update(verdict=verdict, rationale='Explicit test judgment.',
                                     answer_excerpts=[], citation_ids=[])
    paths = []
    for name, value in [('case', case), ('candidate', candidate), ('assessment', review)]:
        path = tmp_path / (name + '.json')
        path.write_text(json.dumps({'sha256': regression.canonical_sha256(value), 'artifact': value}))
        paths.append(str(path))
    monkeypatch.setattr(sys, 'argv', ['regression', '--workspace', 'team', '--collection', 'c', 'assess',
                                     paths[0], paths[1], paths[2], '--case-sha256', regression.canonical_sha256(case),
                                     '--candidate-sha256', regression.canonical_sha256(candidate)])
    if exit_code:
        with pytest.raises(SystemExit) as stopped: regression.main()
        assert stopped.value.code == exit_code
    else: regression.main()
    report = json.loads(capsys.readouterr().out)['artifact']
    assert report['status'] == ('incomplete' if verdict == 'unknown' else verdict)


def test_cli_invalid_input_redacts_private_contents(tmp_path, monkeypatch, capsys):
    import sys
    path = tmp_path / 'bad.json'; path.write_text('{"private-data":"PRIVATE-COMPANY-CANARY"}')
    monkeypatch.setattr(sys, 'argv', ['regression', '--workspace', 'team', '--collection', 'c',
                                     'freeze', str(path), str(path), '--confirm'])
    with pytest.raises(SystemExit) as stopped: regression.main()
    assert stopped.value.code == 1
    captured = capsys.readouterr()
    assert not captured.out and 'PRIVATE-COMPANY-CANARY' not in captured.err


@pytest.mark.parametrize('change', [None, 'content', 'leaf-link', 'parent-link', 'missing', 'oversize', 'traversal'])
def test_current_file_bytes_are_checked_without_following_links(tmp_path, change):
    import hashlib
    root = tmp_path / 'docs'; root.mkdir(); (root / 'sub').mkdir()
    path = root / 'sub/source.md'; path.write_bytes(b'Original source bytes\r\n')
    digest = hashlib.sha256(path.read_bytes()).hexdigest(); relative = 'sub/source.md'
    if change == 'content': path.write_text('Changed without a scan')
    elif change == 'leaf-link':
        other = tmp_path / 'outside.md'; other.write_bytes(path.read_bytes()); path.unlink(); path.symlink_to(other)
    elif change == 'parent-link':
        (root / 'sub').rename(tmp_path / 'outside'); (root / 'sub').symlink_to(tmp_path / 'outside', target_is_directory=True)
    elif change == 'missing': path.unlink()
    elif change == 'oversize': path.write_bytes(b'x' * (2 * 1024 * 1024 + 1))
    elif change == 'traversal': relative = '../docs/sub/source.md'
    assert regression._file_matches(root, relative, digest) is (change is None)


@pytest.mark.asyncio
async def test_read_only_file_guard_rejects_unscanned_edits_and_wrong_workspace(tmp_path, monkeypatch):
    import hashlib
    import sqlite3
    root = tmp_path / 'docs'; root.mkdir(); source = root / 'source.md'; source.write_text('Approved bytes')
    path = tmp_path / 'sources.db'; db = sqlite3.connect(path)
    db.executescript('CREATE TABLE source_collections (id,workspace,root_path,scan_status); CREATE TABLE source_documents (id,collection_id,revision_id,relative_path,content_sha);')
    db.execute('INSERT INTO source_collections VALUES (?,?,?,?)', ('c','team',str(root),'ready'))
    db.execute('INSERT INTO source_documents VALUES (?,?,?,?,?)', ('doc','c','rev','source.md',hashlib.sha256(source.read_bytes()).hexdigest())); db.commit(); db.close()
    before = path.read_bytes(); monkeypatch.setattr(regression, 'get_memory_db_path', lambda: path)
    item = {'evidence': [{'kind':'source', 'collection_id':'c', 'document_id':'doc', 'revision_id':'rev'}]}
    await regression._files_current(SCOPE, item)
    source.write_text('Changed but not scanned')
    with pytest.raises(ValueError, match='changed'): await regression._files_current(SCOPE, item)
    with pytest.raises(ValueError): await regression._files_current(EvidenceScope('foreign',('c',)), item)
    assert path.read_bytes() == before
