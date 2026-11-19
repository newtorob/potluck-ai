"""V37 saved lifecycle using a fake runtime; no native model or inference."""
import asyncio
from copy import deepcopy
import json
import sqlite3
import pytest
import test_investigation_fixed_part_answers_service as shared
from potluck_local.services import investigations as inv
from potluck_local.services import investigation_request_parts as parts
from potluck_local.routers import investigations as routes

@pytest.mark.parametrize('source_contents', [False, True])
def test_v37_create_replay_correct_and_export(tmp_path, monkeypatch, source_contents):

    async def run():
        path, scope, _, _, _ = await shared.setup(tmp_path, monkeypatch)
        question = shared.QUESTION
        fragment = 'the exact expiry date'
        assert fragment in question
        plan = parts.prepare(question, [question], origin='fixture_supplied', request=question)

        class FakeRuntime(shared.FixedRuntime):

            async def chat(self, messages, **kwargs):
                self.calls.append(deepcopy((messages, kwargs)))
                payload = json.loads(messages[1]['content'])
                uid = payload['claims'][0]['units'][0]['id'] if 'claims' in payload else payload['sources'][0]['id']
                clauses = [question[:question.index(fragment)], question[question.index(fragment):]]
                if len(self.calls) == 1:
                    raw = json.dumps({'parts': {'P1': clauses}})
                else:
                    raw = json.dumps({'F1': {'question_kind': 'world_rule_or_value', 'finding': 'established', 'sources': [uid]}, 'F2': {'question_kind': 'source_text_contents' if source_contents else 'world_rule_or_value', 'finding': 'not_established', 'sources': [uid]}})
                return {'choices': [{'message': {'content': raw}, 'finish_reason': 'stop'}]}
        runtime = FakeRuntime()
        monkeypatch.setattr(inv, 'get_runtime', lambda: runtime)
        item = await inv.create_investigation(scope, question, 'Access', 'model', template_version='investigation-v21', reviewed_operations=[{'request': question, 'operation': 'answer'}], evidence_selection='reviewed-current-v1', presentation_mode='fixed-part-selection-v37', generation_profile='qwen3.8-27b-native-non-thinking', prompt_encoding='compact-calculation-v1', reliability_mode='bounded-evidence-v1', question_part_plan=plan)
        answer = item['brief']['answers'][0]
        assert answer['status'] == ('supported' if source_contents else 'unknown')
        assert fragment in answer['text']
        if source_contents:
            assert 'Not established by the selected reviewed statements:' in answer['text']
            from potluck_local.services import investigation_readable_presentation as readable
            projection = readable.project(answer, item['evidence'])
            assert projection is not None and 'Not established by the selected reviewed statements:' in projection['markdown'] and fragment in projection['markdown']
        assert 'selected reviewed statements' in answer['text'].lower()
        assert answer['source_excerpts'] and all((s['citation_id'] for s in answer['source_excerpts']))
        assert len(runtime.calls) == 3 and [call[1]['max_tokens'] for call in runtime.calls] == [256, 512, 512]
        budget = item['provenance']['input_budget']
        assert [s['stage'] for s in budget['stages']] == ['clauses', 'proposal', 'necessity']
        assert [s['model_input_contract'] for s in budget['stages']] == ['fixed-part-selection-v36', 'fixed-part-selection-v37', 'fixed-part-selection-v37']
        reopened = await inv.get_investigation(scope, item['id'])
        assert reopened['brief'] == item['brief']
        exported = (await routes.export(item['id'], scope.workspace, list(scope.collection_ids)))['markdown']
        assert fragment in exported
        if source_contents:
            assert 'Not established by the selected reviewed statements:' in exported
        tampered_trace = deepcopy(item['provenance'])
        tampered_trace['reviewed_claim_selection']['review']['typed_findings']['F2']['finding'] = 'established'
        with sqlite3.connect(path) as db:
            db.execute('UPDATE investigations SET provenance=? WHERE id=?', (json.dumps(tampered_trace), item['id']))
        with pytest.raises((ValueError, LookupError)):
            await inv.get_investigation(scope, item['id'])
        with sqlite3.connect(path) as db:
            db.execute('UPDATE investigations SET provenance=? WHERE id=?', (json.dumps(item['provenance']), item['id']))
        tampered = deepcopy(item['brief'])
        if source_contents:
            tampered['answers'][0]['passage_presentation']['acknowledgements'][0]['answer'] = 'Yes'
        else:
            tampered['answers'][0]['passage_presentation']['unresolved_requests'][0]['text'] = 'invented'
        with sqlite3.connect(path) as db:
            db.execute('UPDATE investigations SET brief=? WHERE id=?', (json.dumps(tampered), item['id']))
        with pytest.raises((ValueError, LookupError)):
            await inv.get_investigation(scope, item['id'])
        with sqlite3.connect(path) as db:
            db.execute('UPDATE investigations SET brief=? WHERE id=?', (json.dumps(item['brief']), item['id']))
        corrected = deepcopy(item['brief'])
        corrected['answers'][0]['text'] = 'Approval applies; the selected statements do not establish the exact expiry date.'
        reviewed = await inv.review_investigation(scope, item['id'], 'correct', 'reviewer', 'Synthetic correction', corrected_brief=corrected)
        assert reviewed['brief'] == item['brief']
        reopened = await inv.get_investigation(scope, item['id'])
        assert reopened['review']['corrected_brief']['answers'][0]['text'] == corrected['answers'][0]['text']
        assert (await routes.export(item['id'], scope.workspace, list(scope.collection_ids)))['markdown']
    asyncio.run(run())
