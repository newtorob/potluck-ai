"""Owner API boundaries and complete review-to-proposal lifecycle."""
import asyncio
import json
from types import SimpleNamespace

from fastapi.testclient import TestClient

from potluck_local.app import create_app
from potluck_local.services import investigations
from potluck_local.services.source_collections import register_collection, scan_collection


def test_owner_workflow_and_revocation(tmp_path, monkeypatch):
    monkeypatch.setenv('POTLUCK_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('POTLUCK_MEMORY_DB_PATH', str(tmp_path / 'memory.db'))
    monkeypatch.delenv('POTLUCK_SIDECAR_TOKEN', raising=False)
    monkeypatch.setattr(investigations, 'get_settings', lambda: SimpleNamespace(
        privacy=SimpleNamespace(memory_enabled=True), memory=SimpleNamespace(enabled=True)))

    class Runtime:
        def current_model_id(self): return 'test-model'
        def current_model_sha256(self): return 'a' * 64
        async def chat(self, messages, **kwargs):
            assert messages[0]['role'] == 'system'
            evidence = json.loads(messages[1]['content'])['evidence']
            brief = dict(facts=[{'text': 'Cache worker restarted.',
                                'citation_ids': [evidence[0]['citation_id']],
                                'source_excerpts': [{'citation_id': evidence[0]['citation_id'], 'quote': 'The cache worker restarted.'}]}],
                         prior_fixes=[], hypotheses=[], recommended_checks=[], model_uncertainties=[])
            return {'choices': [{'message': {'content': json.dumps(brief)}}]}

    monkeypatch.setattr(investigations, 'get_runtime', Runtime)
    client = TestClient(create_app(), client=('127.0.0.1', 44444))
    assert client.get('/company-brain/collections?workspace=demo').json() == {'collections': []}
    source = tmp_path / 'sources'
    source.mkdir()
    document = source / 'incident.md'
    document.write_text('# Incident\nThe cache worker restarted.')
    cid = asyncio.run(register_collection('demo', 'incident', source))
    asyncio.run(scan_collection('demo', cid))
    scope = {'workspace': 'demo', 'collection_ids': [cid]}
    response = client.post('/company-brain/investigations', json={
        **scope, 'issue': 'Explain cache incident', 'query': 'cache', 'model_slug': 'test-model'})
    assert response.status_code == 200, response.text
    item = response.json()
    base = '/company-brain/investigations/' + item['id']
    assert client.post(base + '/outcome', json={**scope, 'reviewer': 'owner',
        'result': 'resolved', 'notes': 'Checked'}).status_code == 400
    assert client.post(base + '/evaluation-case', json={**scope,
        'owner_confirmed': True}).status_code == 400
    bad_brief = {**item['brief'], 'facts': [{**item['brief']['facts'][0], 'source_excerpts': []}]}
    invalid = client.post(base + '/review', json={**scope, 'decision': 'correct',
        'reviewer': 'owner', 'reason': 'Fix', 'corrected_brief': bad_brief})
    assert invalid.status_code == 400
    assert invalid.json()['detail'].startswith('invalid_review_brief:')
    assert client.post(base + '/review', json={**scope, 'decision': 'correct',
        'reviewer': 'owner', 'reason': 'Verified event, cause unknown',
        'corrected_brief': {**item['brief'], 'model_uncertainties': ['Root cause unknown']}}).status_code == 200
    assert client.post(base + '/outcome', json={**scope, 'reviewer': 'owner',
        'result': 'unresolved', 'notes': 'Restart observed, root cause not found'}).status_code == 200
    assert client.post(base + '/evaluation-case', json={**scope,
        'owner_confirmed': False}).status_code == 403
    proposal = client.post(base + '/evaluation-case', json={**scope, 'owner_confirmed': True})
    assert proposal.status_code == 200
    assert 'Cache worker restarted.' not in proposal.text
    knowledge = client.post(base + '/knowledge-proposal', json={**scope,
        'owner_confirmed': True, 'claim_key': 'cache-event', 'content': 'Cache worker restarted.',
        'citation_ids': item['brief']['facts'][0]['citation_ids']})
    assert knowledge.status_code == 200, knowledge.text
    assert knowledge.json()['status'] == 'proposed'
    exported = client.get(base + '/export', params=scope)
    assert exported.status_code == 200
    markdown = exported.json()['markdown']
    assert 'Root cause unknown' in markdown
    assert '## Model questions (unverified)' in markdown
    assert '## Retrieval notices' in markdown
    assert 'Source excerpt (' in markdown
    assert r'The cache worker restarted\.' in markdown
    assert client.get(base, params={**scope, 'workspace': 'other'}).status_code == 404
    document.write_text('# Incident\nThe cache incident report was withdrawn.')
    asyncio.run(scan_collection('demo', cid))
    assert client.get(base + '/export', params=scope).status_code == 404
    revoked = client.post(base + '/review', json={**scope, 'decision': 'correct',
        'reviewer': 'owner', 'reason': 'Fix', 'corrected_brief': bad_brief})
    assert revoked.status_code == 404
    assert revoked.json()['detail'] == 'Investigation unavailable'
    assert client.post(base + '/evaluation-case', json={**scope,
        'owner_confirmed': True}).status_code == 404


def test_company_brain_routes_keep_existing_local_auth_boundary(tmp_path, monkeypatch):
    monkeypatch.setenv('POTLUCK_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('POTLUCK_SIDECAR_TOKEN', 'owner-test-token')
    app = create_app()
    remote = TestClient(app, client=('100.64.5.8', 40000))
    local = TestClient(app, client=('127.0.0.1', 40000))
    for method, path in [('GET', '/company-brain/collections?workspace=demo'),
                         ('POST', '/company-brain/investigations'),
                         ('POST', '/company-brain/investigations/id/review'),
                         ('POST', '/company-brain/investigations/id/evaluation-case'),
                         ('POST', '/company-brain/investigations/id/knowledge-proposal')]:
        assert remote.request(method, path).status_code == 403
        assert local.request(method, path).status_code == 401


def test_export_separates_legacy_and_escapes_untrusted_content(monkeypatch):
    from potluck_local.routers import investigations as routes

    item = {
        'id': 'i1', 'workspace': 'demo', 'collection_ids': ['c1'], 'issue': '# unsafe',
        'status': 'reviewed', 'model_slug': 'model', 'model_sha': 'sha', 'created_at': 'today',
        'provenance': {}, 'review': None, 'outcome': None, 'evidence': [],
        'brief': {'facts': [{'text': '<script>', 'citation_ids': [],
                             'source_excerpts': [{'citation_id': 'raw', 'quote': '*quoted*'}]}],
                  'prior_fixes': [], 'hypotheses': [], 'recommended_checks': [],
                  'missing_evidence': ['old notice']},
    }

    async def get_item(scope, iid):
        return item

    monkeypatch.setattr(routes, 'get_investigation', get_item)
    result = asyncio.run(routes.export('i1', 'demo', ['c1']))['markdown']
    assert r'\# unsafe' in result
    assert r'\<script\>' in result
    assert r'\*quoted\*' in result
    assert 'Historical combined missing evidence (provenance unknown)' in result
    assert '## Model questions' not in result
