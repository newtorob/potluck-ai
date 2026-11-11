from copy import deepcopy
import hashlib
import json

import pytest

from potluck_local.services.investigation_prompt_encoding import COMPACT, encode


def test_compaction_preserves_every_warning_and_original_evidence():
    notices = [{'snippet_id': f'S{i}', 'citation_id': f'source:{i}',
                'source_text': '2^(-age / half_life)', 'start': 5, 'end': 25,
                'reason': 'Unextracted number; unsupported or incomplete.' if i % 2 else 'Other warning.'}
               for i in range(1, 31)]
    data = {'issue': 'Compare weights.', 'snippets': [{'snippet_id': 'S1', 'text': '文書'}],
            'quantities': [{'id': 'N1', 'value': '24', 'unit': 'h'}],
            'extraction_notices': notices, 'evidence_notices': ['Context truncated.']}
    before = deepcopy(data)
    text, trace = encode(data)
    packed = json.loads(text)
    restored = []
    for compact, original in zip(packed['extraction_notices'], notices):
        row = dict(compact)
        row['reason'] = packed['extraction_notice_reasons'][row.pop('reason_index')]
        row['citation_id'] = original['citation_id']
        restored.append(row)
    assert restored == notices and data == before
    for key in ('issue', 'snippets', 'quantities', 'evidence_notices'):
        assert packed[key] == data[key]
    assert trace['notice_count'] == 30 and trace['reason_count'] == 2
    assert trace['version'] == COMPACT
    assert trace['encoded_characters'] < trace['original_characters']
    assert trace['encoded_sha256'] == hashlib.sha256(text.encode()).hexdigest()
    assert trace['original_sha256'] == hashlib.sha256(json.dumps(data, ensure_ascii=False).encode()).hexdigest()


@pytest.mark.parametrize('notices', [None, []])
def test_requests_without_warnings_keep_exact_prompt_bytes(notices):
    data = {'issue': 'What is documented?', 'snippets': []}
    if notices is not None:
        data['extraction_notices'] = notices
    text, trace = encode(data)
    assert text == json.dumps(data, ensure_ascii=False)
    assert trace['original_sha256'] == trace['encoded_sha256']

@pytest.mark.parametrize('template,encoding', [
    ('investigation-v21', 'unknown'), ('investigation-v20', COMPACT),
    ('investigation-v4', COMPACT), ('investigation-v21', []),
])
def test_invalid_encoding_is_rejected_before_accessing_runtime(template, encoding, monkeypatch):
    import asyncio
    from potluck_local.services import investigations
    from potluck_local.services.evidence_lookup import EvidenceScope
    monkeypatch.setattr(investigations, 'get_runtime', lambda: pytest.fail('must reject before runtime'))
    with pytest.raises(ValueError, match='Unsupported investigation prompt encoding'):
        asyncio.run(investigations.create_investigation(EvidenceScope('w', ('c',)),
            'Question', 'query', 'model', template_version=template, prompt_encoding=encoding))
