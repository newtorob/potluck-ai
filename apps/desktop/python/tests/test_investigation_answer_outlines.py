"""Organization is optional and lossless, and never grants evidence authority."""
import asyncio
from copy import deepcopy
import json
import time

import pytest

from potluck_local.services import investigation_answer_outlines as view
from potluck_local.services import investigation_readable_presentation as readable

QUESTION = 'What hosting should we use, what should we show and what should we keep off camera?'


def fixture():
    quotes = ['Host unlisted only if comments are off.', '\n',
              'Show the workflow after confirmation, unless the service is unavailable.',
              'Keep credentials and real customer data off camera.']
    rows = [{'snippet_id': f'S{i}', 'citation_id': 'claim:one', 'quote': quote}
            for i, quote in enumerate(quotes)]
    content = '\n'.join(quotes)
    answer = {'question_id': 'R1', 'status': 'supported', 'text': content,
              'snippet_ids': [row['snippet_id'] for row in rows], 'source_excerpts': rows,
              'passage_presentation': {'version': 'fixed-part-selection-v32'}}
    evidence = [{'citation_id': 'claim:one', 'kind': 'reviewed_claim', 'content': content}]
    draft = {'titles': ['Hosting', 'What to show', 'What to keep off camera'], 'groups': [0, 1, 2]}
    return answer, evidence, draft


def test_schema_is_admitted_by_the_real_native_runtime_boundary():
    from potluck_local.services.local_server import LocalServerModel
    from potluck_local.services.runtime import _structured_response_format
    # The real schema gate only inspects the adapter type; no process or model is needed.
    native = object.__new__(LocalServerModel)
    response_format = {'type': 'json_object', 'schema': view.SCHEMA}
    assert _structured_response_format(native, response_format) == response_format


def test_lossless_complete_coverage_source_order_and_no_mutation():
    answer, evidence, draft = fixture(); original = deepcopy((answer, evidence, draft))
    outline = view.compile_outline(QUESTION, answer, evidence, draft)
    assert outline['source_label_snippet_ids'] == ['S1']
    assert [b['snippet_ids'] for s in outline['sections'] for b in s['blocks']] == [['S0'], ['S2'], ['S3']]
    assert outline['source_contents'] == {'claim:one': evidence[0]['content']}
    assert (answer, evidence, draft) == original


@pytest.mark.parametrize('change', [
    {'groups': [0, 1]}, {'groups': [0, 1, True]}, {'groups': [0, 1, 3]},
    {'groups': [0, 0, 0]}, {'titles': ['Hosting', 'Shipping', 'Camera']},
    {'titles': ['Hosting approved', 'What to show', 'Camera']},
    {'titles': ['Hosting', 'SHOW', 'show']}, {'titles': ['Hosting\n', 'Show', 'Camera']},
    {'extra': 'invented'}, {'titles': []}, {'titles': ['What', 'Show', 'Camera']},
])
def test_partial_invented_malformed_or_incompatible_drafts_rejected(change):
    answer, evidence, draft = fixture()
    with pytest.raises(ValueError):
        view.compile_outline(QUESTION, answer, evidence, {**draft, **change})


@pytest.mark.parametrize('change', ['quote', 'source', 'duplicate', 'unknown', 'conflicting', 'oversized'])
def test_unbound_ambiguous_or_ineligible_answers_rejected(change):
    answer, evidence, draft = fixture()
    if change == 'quote': answer['source_excerpts'][0]['quote'] = 'Other text'
    if change == 'source': evidence[0]['content'] = 'Changed source'
    if change == 'duplicate': answer['snippet_ids'][1] = 'S0'
    if change in ('unknown', 'conflicting'): answer['status'] = change
    if change == 'oversized':
        answer['source_excerpts'] = [dict(answer['source_excerpts'][0], snippet_id=f'S{i}') for i in range(65)]
        answer['snippet_ids'] = [row['snippet_id'] for row in answer['source_excerpts']]
    with pytest.raises(ValueError): view.compile_outline(QUESTION, answer, evidence, draft)


class Runtime:
    def __init__(self, raw=None, failure=None, measure=None, finish='stop'):
        self.raw = raw or json.dumps({'groups': {'r0': 'P1', 'r1': 'P2', 'r2': 'P3'}}); self.failure = failure
        self.measured = measure or {'input_tokens': 200, 'context_tokens': 8192}
        self.finish = finish; self.calls = []; self.measure_calls = []
    def current_model_id(self): return 'model'
    def current_model_sha256(self): return 'sha'
    async def measure_investigation_prompt(self, messages, **kwargs):
        from potluck_local.services.local_server import LocalServerModel
        from potluck_local.services.runtime import _structured_response_format
        _structured_response_format(object.__new__(LocalServerModel), kwargs['response_format'])
        self.measure_calls.append(kwargs)
        return self.measured
    async def chat(self, messages, **kwargs):
        self.calls.append(kwargs)
        if self.failure: raise self.failure
        return {'choices': [{'finish_reason': self.finish, 'message': {'content': self.raw}}]}


def produce(runtime, **kwargs):
    answer, evidence, _ = fixture()
    return asyncio.run(view.produce(QUESTION, answer, evidence, runtime=runtime,
        model='model', model_sha='sha', profile=view.PROFILE, seed=7,
        deadline=time.monotonic() + 60, question_parts=fixture()[2]['titles'], **kwargs))


def test_one_bounded_call_and_persisted_reload_without_inference():
    runtime = Runtime(); answer, evidence, _ = fixture()
    record = produce(runtime)
    assert record['receipt']['status'] == 'organized' and len(runtime.calls) == 1
    assert runtime.calls[0]['max_tokens'] == 768 and runtime.calls[0]['bounded_generation'] is True
    assert runtime.calls[0]['expected_context_tokens'] == 8192
    assert runtime.calls[0]['response_format']['schema']['properties']['groups']['required'] == ['r0', 'r1', 'r2']
    assert runtime.calls[0]['response_format']['schema']['properties']['groups']['properties']['r0']['enum'] == ['context', 'P1', 'P2', 'P1+P2', 'P3', 'P1+P3', 'P2+P3', 'P1+P2+P3']
    item = {'issue': QUESTION, 'brief': {'answers': [answer]}, 'evidence': evidence,
            'provenance': {'automatic_answer_outlines': {'R1': record}, 'question_part_plan': {'parts': [{'request': title} for title in fixture()[2]['titles']]}}}
    assert readable.decorate(json.loads(json.dumps(item)))['answer_outlines']['R1'] == view.saved_outline(QUESTION, answer, evidence, record, question_parts=fixture()[2]['titles'])
    assert len(runtime.calls) == 1
    item['review'] = {'decision': 'correct'}
    assert 'answer_outlines' not in readable.decorate(item)
    item.pop('review'); item['status'] = 'invalidated'
    assert 'answer_outlines' not in readable.decorate(item)
    assert view.saved_outline(QUESTION + ' changed', answer, evidence, record) is None
    answer['text'] += ' changed'
    assert view.saved_outline(QUESTION, answer, evidence, record) is None


@pytest.mark.parametrize('groups', ['012', [0, 1, 2], {'r0':'P1'}, {'r0':'P1','r1':'P2','r2':'P4'}, {'r0':'P1','r1':'P1','r2':'P1'}])
def test_new_native_output_rejects_bad_lengths_characters_and_old_array_wire(groups):
    record = produce(Runtime(raw=json.dumps({'groups': groups})))
    assert record['receipt']['status'] == 'original_fallback' and record['receipt']['native_calls'] == 1


def test_saved_legacy_array_outline_remains_readable_without_repair():
    answer, evidence, draft = fixture()
    record = {'version': 'automatic-answer-outline-v1', 'binding_sha256': view._binding(QUESTION, answer, evidence), 'draft': draft}
    assert view.saved_outline(QUESTION, answer, evidence, record) == view.compile_outline(QUESTION, answer, evidence, draft)


def test_adjacent_condition_packet_preserves_exact_quotes_and_prevents_detachment():
    answer, evidence, _ = fixture()
    quotes = ['Show the quiz.', 'If it fails, skip the optional shot.', 'Keep tokens off camera.']
    answer['source_excerpts'] = [{'snippet_id': f'S{i}', 'citation_id': 'claim:one', 'quote': q} for i,q in enumerate(quotes)]
    answer['snippet_ids'] = [row['snippet_id'] for row in answer['source_excerpts']]
    evidence[0]['content'] = '\n\n'.join(quotes)
    evidence[0]['sources'] = [{'content': '\n\n'.join(quotes[:2])}, {'content': quotes[2]}]
    assert view.condition_packets(answer['source_excerpts'], evidence) == [[0, 1], [2]]
    draft = {'titles': ['Show', 'Keep off camera'], 'groups': [0, 1, 1]}
    with pytest.raises(ValueError, match='Detached conditional'):
        view.compile_outline(QUESTION, answer, evidence, draft, keep_conditions=True)
    draft['groups'] = [0, 0, 1]
    result = view.compile_outline(QUESTION, answer, evidence, draft, keep_conditions=True)
    assert result['sections'][0]['blocks'][0]['snippet_ids'] == ['S0', 'S1']


@pytest.mark.parametrize('ambiguous', [False, True])
def test_condition_packets_never_guess_across_sections_or_ambiguous_support(ambiguous):
    rows = [{'citation_id': 'claim:x', 'quote': q} for q in ['Show the quiz.', 'If it fails, skip the shot.']]
    support = [{'content': rows[0]['quote']}, {'content': rows[1]['quote']}]
    if ambiguous: support.append({'content': rows[0]['quote'] + '\n\n' + rows[1]['quote']})
    assert view.condition_packets(rows, [{'kind': 'reviewed_claim', 'citation_id': 'claim:x', 'sources': support}]) == [[0], [1]]


def test_saved_current_outline_binds_the_support_metadata_and_reviewed_topics():
    answer, evidence, _ = fixture(); record = produce(Runtime())
    parts = fixture()[2]['titles']
    assert view.saved_outline(QUESTION, answer, evidence, record, question_parts=parts)
    assert view.saved_outline(QUESTION, answer, evidence, record, question_parts=[*parts[:2], 'Other topic']) is None
    evidence[0]['sources'] = [{'content': 'Changed support metadata'}]
    assert view.saved_outline(QUESTION, answer, evidence, record, question_parts=parts) is None


@pytest.mark.parametrize('runtime', [Runtime(raw='{"titles":[],"titles":[],"groups":[]}'),
    Runtime(raw='not JSON'), Runtime(raw='{}'), Runtime(finish='length'),
    Runtime(failure=TimeoutError()), Runtime(failure=RuntimeError('private provider details')),
    Runtime(measure={'input_tokens': 6001, 'context_tokens': 8192}),
    Runtime(measure={'input_tokens': 6000, 'context_tokens': 6100}),
    Runtime(measure={'input_tokens': True, 'context_tokens': 8192})])
def test_optional_failure_returns_safe_fallback_without_retry_or_private_error(runtime):
    record = produce(runtime)
    assert record['receipt']['status'] == 'original_fallback' and len(runtime.calls) <= 1
    assert 'draft' not in record and 'private' not in json.dumps(record)


def test_cancellation_propagates_and_restores_cache_context():
    from potluck_local.services import prompt_cache
    runtime = Runtime(failure=asyncio.CancelledError())
    token = prompt_cache.request_is_local.set(True)
    try:
        with pytest.raises(asyncio.CancelledError): produce(runtime)
        assert prompt_cache.request_is_local.get() is True
    finally: prompt_cache.request_is_local.reset(token)


def test_optional_deadline_keeps_model_generation_alive_and_discards_late_projection(monkeypatch):
    async def run():
        release = asyncio.Event(); cancelled = []
        class Slow(Runtime):
            async def chat(self, messages, **kwargs):
                try: await release.wait()
                except asyncio.CancelledError:
                    cancelled.append(True); raise
                return await super().chat(messages, **kwargs)
        runtime = Slow(); answer, evidence, draft = fixture()
        monkeypatch.setattr(view, 'MAX_SECONDS', .02)
        record = await view.produce(QUESTION, answer, evidence, runtime=runtime, model='model',
            model_sha='sha', profile=view.PROFILE, seed=7, deadline=time.monotonic()+5,
            question_parts=draft['titles'])
        assert record['receipt']['reason'] == 'timeout' and len(view._pending) == 1
        assert not cancelled and 'draft' not in record
        release.set()
        await asyncio.gather(*view._pending)
        await asyncio.sleep(0)
        assert not view._pending and not cancelled and 'draft' not in record
    asyncio.run(run())


def test_explicit_parent_cancellation_stops_the_owned_generation():
    async def run():
        started = asyncio.Event(); cancelled = []
        class Waiting(Runtime):
            async def chat(self, messages, **kwargs):
                started.set()
                try: await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.append(True); raise
        answer, evidence, draft = fixture()
        task = asyncio.create_task(view.produce(QUESTION, answer, evidence, runtime=Waiting(),
            model='model', model_sha='sha', profile=view.PROFILE, seed=7,
            deadline=time.monotonic()+5, question_parts=draft['titles']))
        await started.wait(); task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        await asyncio.sleep(0); await asyncio.sleep(0)
        assert cancelled and not view._pending
    asyncio.run(run())


def test_single_paragraph_covering_multiple_questions_stays_intact_once():
    answer, evidence, _ = fixture()
    quote = 'Run reconciliation in the request. If the plan read fails, write nothing.'
    answer['source_excerpts'] = [{'snippet_id': 'S0', 'citation_id': 'claim:one', 'quote': quote}]
    answer['snippet_ids'] = ['S0']; evidence[0]['content'] = quote
    parts = ['Where should reconciliation run?', 'What happens if the plan read fails?']
    result = asyncio.run(view.produce(QUESTION, answer, evidence,
        runtime=Runtime(raw='{"groups":{"r0":"P1+P2"}}'), model='model', model_sha='sha',
        profile=view.PROFILE, seed=7, deadline=time.monotonic()+5, question_parts=parts))
    assert result['receipt']['status'] == 'organized'
    outline = view.saved_outline(QUESTION, answer, evidence, result, question_parts=parts)
    assert outline['sections'] == [{'title': ' / '.join(parts),
        'blocks': [{'label': 'Cited passages', 'snippet_ids': ['S0']}]}]
    assert outline['source_contents'] == {'claim:one': quote}


@pytest.mark.parametrize('groups', [[1, 1, 1], [1, 2, 8], [1, True, 4], [1, 2], '124'])
def test_shared_outline_rejects_missing_topics_out_of_range_boolean_or_partial_coverage(groups):
    answer, evidence, draft = fixture(); topics = [view.CONTEXT_TITLE, *draft['titles']]
    with pytest.raises(ValueError):
        view.compile_shared(QUESTION, answer, evidence, {'titles': topics, 'groups': groups}, approved_titles=topics)


def test_legacy_v4_group_indices_keep_their_original_meaning():
    answer, evidence, draft = fixture(); topics = [view.CONTEXT_TITLE, *draft['titles']]
    old = {'version': 'automatic-answer-outline-v4',
        'binding_sha256': view._binding(QUESTION, answer, evidence, topics, bind_support=True),
        'draft': {'titles': topics, 'groups': [1, 2, 3]}}
    outline = view.saved_outline(QUESTION, answer, evidence, old, question_parts=draft['titles'])
    assert [s['title'] for s in outline['sections']] == draft['titles']
    assert outline['sections'][-1]['blocks'][0]['snippet_ids'] == ['S3']


def test_explicit_later_plan_steps_and_completion_cannot_become_before_requirements():
    topics = ['Source context', 'What must happen before adding the panel?', 'What should it measure?', 'What is optional?']
    rows = [{'quote': text} for text in ['**Fix.** After committing the current dashboard:',
        '1. Add the sending-approved metric.', '2. Record the month\'s revenue.',
        '**Done when.** Both founders read it two weeks running.']]
    result = view._assignment_options(topics, rows, [[0], [1], [2], [3]])
    assert 'P1' in result['r0']
    assert all('P1' not in key for key in result['r1'] + result['r2'])
    assert result['r3'] == ['context']


def test_before_building_list_inherits_its_explicit_stage_and_requested_completion_stays_available():
    topics = ['Source context', 'What checks are required before building?', 'How should it work?', 'What counts as done?']
    rows = [{'quote': text} for text in ['**Fix.** Set up the data path. Before building:',
        '- Check the size limit.', '- Confirm the dev-store read.', '**Done when.** The fallback works.']]
    result = view._assignment_options(topics, rows, [[0], [1], [2], [3]])
    assert all('P1' in result[key] for key in ['r0', 'r1', 'r2'])
    assert 'P1' not in result['r3'] and 'P2+P3' in result['r3']


def test_saved_stage_guard_rejects_temporally_inverted_assignment():
    answer, evidence, _ = fixture()
    quotes = ['**Fix.** After source approval:', '1. Add the metric.', '**Done when.** Both reviewers read it.']
    answer['source_excerpts'] = [{'snippet_id': f'S{i}', 'citation_id': 'claim:one', 'quote': q} for i,q in enumerate(quotes)]
    answer['snippet_ids'] = [r['snippet_id'] for r in answer['source_excerpts']]
    evidence[0]['content'] = '\n\n'.join(quotes)
    topics = ['Source context', 'What happens before building?', 'What should it measure?']
    with pytest.raises(ValueError, match='source stage'):
        view.compile_shared(QUESTION, answer, evidence, {'titles':topics,'groups':[1,2,1]},
            approved_titles=topics, respect_stages=True)
    result = view.compile_shared(QUESTION, answer, evidence, {'titles':topics,'groups':[1,2,0]},
        approved_titles=topics, respect_stages=True)
    assert result['sections'][-1]['blocks'][0]['snippet_ids'] == ['S2']


def test_generic_before_sending_keeps_numbered_setup_checklist_available_to_both_parts():
    topics = ['Source context', 'What should a setup call cover before the store is sending?',
              'What outcome should be recorded?']
    rows = [{'quote': text} for text in [
        '**Fix.**',
        '1. Every call checks the sender, enables the embed and sends a test email.',
        '2. Record whether sending works and note any blocker.',
    ]]
    options = view._assignment_options(topics, rows, [[0], [1], [2]])
    assert 'P1' in options['r1'] and 'P1+P2' in options['r1']
    assert 'P1+P2' in options['r2']
    answer, evidence, _ = fixture()
    answer['source_excerpts'] = [{'snippet_id': f'S{i}', 'citation_id': 'claim:one', 'quote': row['quote']}
                                 for i, row in enumerate(rows)]
    answer['snippet_ids'] = [r['snippet_id'] for r in answer['source_excerpts']]
    evidence[0]['content'] = '\n\n'.join(row['quote'] for row in rows)
    output = view.compile_shared(QUESTION, answer, evidence,
        {'titles': topics, 'groups': [0, 3, 2]}, approved_titles=topics, respect_stages=True)
    blocks = [b for section in output['sections'] for b in section['blocks']]
    assert {sid for b in blocks for sid in b['snippet_ids']} == {'S0', 'S1', 'S2'}
    assert output['original_text'] == answer['text']
    assert output['original_snippet_ids'] == answer['snippet_ids']
    assert 'S1' in output['sections'][0]['blocks'][0]['snippet_ids']
    assert all(row['quote'] in evidence[0]['content'] for row in rows)


def test_separate_after_boundary_keeps_later_steps_out_of_before_question():
    topics = ['Source context', 'What happens before approval?', 'What happens after?']
    rows = [{'quote': text} for text in ['**Fix.**', 'After approval:', '1. Send the message.']]
    options = view._assignment_options(topics, rows, [[0], [1], [2]])
    assert all('P1' not in key for key in options['r2'])
    assert 'P2' in options['r2']
