import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from potluck_local.services import investigation_context as ctx
from potluck_local.services import investigation_calculations as calc
from potluck_local.services import investigations as inv
from potluck_local.services.evidence_lookup import EvidenceScope
from potluck_local.services.memory_db import init_memory_db
from potluck_local.services.source_collections import register_collection, scan_collection


def make(content, cid='C1'):
    return [{'kind': 'source', 'citation_id': cid, 'presented_content': content}]


@pytest.mark.parametrize('source', [
    '', '   \n\t  ', 'hello\n\n- list one\n- list two\n',
    '短い文。 ' * 300 + '終わり。',
    'a' * 2201, ('A decimal is 3.14 and Dr. A agrees. ' * 80),
    'prose.\n```python\n' + ('print("not. a split")\n' * 90) + '```\nend.\n',
    'prefix\n~~~\n' + ('code with. punctuation; and spaces\n' * 80),
])
def test_prefix_exact_including_whitespace_unicode_and_fences(source):
    snippets, trace = ctx.snippets_from_evidence(make(source), version='paragraph-v3')
    joined = ''.join(s['quote'] for s in snippets)
    assert joined == source[:len(joined)]
    assert all(0 < len(s['quote']) <= 1000 for s in snippets)
    assert trace['omitted_characters'] == len(source) - len(joined)
    assert trace['truncated'] == (len(joined) != len(source))


def test_prose_prefers_sentence_boundary_but_code_does_not():
    source = ('a' * 700 + '. ' + 'b' * 500 + '. end')
    snippets, trace = ctx.snippets_from_evidence(make(source), version='paragraph-v3')
    assert snippets[0]['quote'] == 'a' * 700 + '. '
    assert trace['sentence_splits'] == 1
    code = '```\n' + ('abc. def ' * 140) + '\n```\n'
    snippets, trace = ctx.snippets_from_evidence(make(code), version='paragraph-v3')
    assert trace['sentence_splits'] == 0 and ''.join(s['quote'] for s in snippets) == code
    nested = '```\n~~~\n' + ('code. text ' * 120) + '\n```\n'
    snippets, trace = ctx.snippets_from_evidence(make(nested), version='paragraph-v3')
    assert trace['sentence_splits'] == 0 and ''.join(s['quote'] for s in snippets) == nested


def test_capacity_counts_omitted_exactly():
    source = 'x' * 101_005
    snippets, trace = ctx.snippets_from_evidence(make(source), version='paragraph-v3')
    assert len(snippets) == 100 and trace['omitted_characters'] == 1005
    assert ''.join(s['quote'] for s in snippets) == source[:100000]
    assert trace['truncated']


def test_quantity_catalog_uses_exact_prefix_and_rejects_changed_context():
    source = 'Context. ' * 70 + 'before was 20 calendar days. After was 15 calendar days.' + ' trailing.' * 80
    snippets, trace = ctx.snippets_from_evidence(make(source), version='paragraph-v3')
    quantities = calc.quantity_catalog(snippets, source_context={'C1': source}, version='v4')
    assert [q['value'] for q in quantities] == ['20', '15']
    for q in quantities:
        quote = next(s['quote'] for s in snippets if s['snippet_id'] == q['snippet_id'])
        assert quote[q['start']:q['end']] == q['source_text']
    with pytest.raises(ValueError):
        calc.quantity_catalog(snippets, source_context={'C1': 'wrong'}, version='v4')


def test_v15_service_correction_export_rebuilds_v3(tmp_path, monkeypatch):
    monkeypatch.setenv('POTLUCK_MEMORY_DB_PATH', str(tmp_path / 'db.sqlite'))
    monkeypatch.setenv('POTLUCK_DATA_DIR', str(tmp_path))
    monkeypatch.setattr(inv, 'get_settings', lambda: SimpleNamespace(privacy=SimpleNamespace(memory_enabled=True), memory=SimpleNamespace(enabled=True)))
    class Runtime:
        def current_model_id(self): return 'model'
        def current_model_sha256(self): return 'a' * 64
        async def chat(self, messages, **kwargs):
            payload = json.loads(messages[1]['content'])
            if 'units' in payload:
                sid = payload['units'][0]['snippet_id']
                data = {'R1': {'slots': [{'aspect': 'A fact', 'evidence_ids': [sid]}], 'more': True}}
            else:
                sid = payload['snippets'][0]['snippet_id']
                data = {'R1': {'R1P1': {'status': 'supported', 'text': 'A fact.', 'snippet_ids': [sid]}}}
            return {'choices': [{'message': {'content': json.dumps(data)}}]}
    monkeypatch.setattr(inv, 'get_runtime', Runtime)
    async def run():
        await init_memory_db(); folder = tmp_path / 'docs'; folder.mkdir()
        (folder / 'a.md').write_text('# A\nUseful fact. ' + 'context words. ' * 100)
        cid = await register_collection('w', 'docs', folder); await scan_collection('w', cid)
        scope = EvidenceScope('w', (cid,))
        item = await inv.create_investigation(scope, 'Explain fact', 'useful', 'model', template_version='investigation-v15',
            reviewed_operations=[{'request': 'Explain fact', 'operation': 'answer'}])
        assert item['provenance']['snippet_trace']['version'] == 'paragraph-v3'
        corrected = deepcopy(item['brief']); corrected['answers'][0]['text'] = 'Reviewed fact.'; corrected['answers'][0]['status'] = 'supported'
        reviewed = await inv.review_investigation(scope, item['id'], 'correct', 'owner', 'review', corrected_brief=corrected)
        assert reviewed['review']['corrected_brief']['answers'][0]['text'] == 'Reviewed fact.'
        from potluck_local.routers.investigations import export
        text = (await export(item['id'], 'w', [cid]))['markdown']
        assert 'Original machine claims (unverified)' in text and 'Model-selected planning excerpt' in text
        assert 'Reviewed fact' in text
    asyncio.run(run())
