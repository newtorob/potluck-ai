"""A bounded local check of an immutable investigation draft."""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json
import time
import uuid

import aiosqlite

from . import investigation_draft_verification as contract
from . import investigation_draft_verification_v2 as contract_v2
from . import investigation_draft_verification_v3 as contract_v3
from . import investigations as inv
from . import investigation_context, investigation_structural_answer, investigation_structural_context
from . import prompt_cache
from .memory_db import get_memory_db_path

PROFILE = 'qwen3.8-27b-native-non-thinking'
MAX_RUNS = 3
OUTPUT_TOKENS = 2000
TIMEOUT_SECONDS = 240


def _contract(version):
    if version == contract.VERSION:
        return contract
    if version == contract_v2.VERSION:
        return contract_v2
    if version == contract_v3.VERSION:
        return contract_v3
    raise ValueError('Unknown verification version')


def report_sections(report):
    version = report['input']['version']
    implementation = _contract(version)
    sections = (implementation.SECTIONS if implementation is not contract else
                (('source_checks', 'source_checks'), ('claim_checks', 'claims'), ('group_checks', 'groups')))
    return [(key, report['input'][field]) for key, field in sections]


def _not_checked(implementation, envelope, reason):
    result = {'version': implementation.VERSION, 'status': 'not_checked', 'reason': reason, 'scope': implementation.SCOPE}
    if implementation is contract_v3:
        result['mechanical_warnings'] = contract_v3.heading_warnings(envelope)
    return result


def _input_policy(implementation):
    return getattr(implementation, 'INPUT_POLICY', 'all cited passages; no clipping or selection')


def original_input(item, *, version=contract.VERSION):
    implementation = _contract(version)
    p = item['provenance']
    if p.get('presentation_mode') in ('reviewed-claim-selection-v1', 'reviewed-claim-selection-v2', 'reviewed-claim-selection-v3', 'reviewed-claim-selection-v4', 'reviewed-claim-selection-v5', 'reviewed-claim-selection-v6', 'reviewed-claim-selection-v7', 'reviewed-claim-selection-v8', 'reviewed-claim-selection-v9', 'reviewed-claim-selection-v10', 'reviewed-claim-selection-v11', 'reviewed-claim-selection-v12', 'reviewed-claim-selection-v13', 'reviewed-claim-selection-v14', 'reviewed-claim-selection-v15', 'reviewed-claim-selection-v16', 'fixed-part-selection-v1', 'fixed-part-selection-v2', 'fixed-part-selection-v3', 'fixed-part-selection-v4', 'fixed-part-selection-v5', 'fixed-part-selection-v6', 'fixed-part-selection-v7', 'fixed-part-selection-v8', 'fixed-part-selection-v9', 'fixed-part-selection-v10', 'fixed-part-selection-v11', 'fixed-part-selection-v12', 'fixed-part-selection-v13', 'fixed-part-selection-v14', 'fixed-part-selection-v15', 'fixed-part-selection-v16', 'fixed-part-selection-v17', 'fixed-part-selection-v18', 'fixed-part-selection-v19', 'fixed-part-selection-v20', 'fixed-part-selection-v21', 'fixed-part-selection-v22', 'fixed-part-selection-v23', 'fixed-part-selection-v24', 'fixed-part-selection-v25', 'fixed-part-selection-v26', 'fixed-part-selection-v27', 'fixed-part-selection-v28', 'fixed-part-selection-v29', 'fixed-part-selection-v30', 'fixed-part-selection-v31', 'fixed-part-selection-v32'):
        from . import investigation_reviewed_evidence
        if p['presentation_mode'] == 'fixed-part-selection-v32':
            from . import investigation_fixed_part_answers_v32 as investigation_reviewed_claim_selection
        elif p['presentation_mode'] == 'fixed-part-selection-v31':
            from . import investigation_fixed_part_answers_v31 as investigation_reviewed_claim_selection
        elif p['presentation_mode'] == 'fixed-part-selection-v30':
            from . import investigation_fixed_part_answers_v30 as investigation_reviewed_claim_selection
        elif p['presentation_mode'] == 'fixed-part-selection-v29':
            from . import investigation_fixed_part_answers_v29 as investigation_reviewed_claim_selection
        elif p['presentation_mode'] == 'fixed-part-selection-v28':
            from . import investigation_fixed_part_answers_v28 as investigation_reviewed_claim_selection
        elif p['presentation_mode'] == 'fixed-part-selection-v27':
            from . import investigation_fixed_part_answers_v27 as investigation_reviewed_claim_selection
        elif p['presentation_mode'] == 'fixed-part-selection-v26':
            from . import investigation_fixed_part_answers_v26 as investigation_reviewed_claim_selection
        elif p['presentation_mode'] == 'fixed-part-selection-v25':
            from . import investigation_fixed_part_answers_v25 as investigation_reviewed_claim_selection
        elif p['presentation_mode'] == 'fixed-part-selection-v24':
            from . import investigation_fixed_part_answers_v24 as investigation_reviewed_claim_selection
        elif p['presentation_mode'] == 'fixed-part-selection-v23':
            from . import investigation_fixed_part_answers_v23 as investigation_reviewed_claim_selection
        elif p['presentation_mode'] == 'fixed-part-selection-v22':
            from . import investigation_fixed_part_answers_v22 as investigation_reviewed_claim_selection
        elif p['presentation_mode'] == 'fixed-part-selection-v21':
            from . import investigation_fixed_part_answers_v21 as investigation_reviewed_claim_selection
        elif p['presentation_mode'] == 'fixed-part-selection-v20':
            from . import investigation_fixed_part_answers_v20 as investigation_reviewed_claim_selection
        elif p['presentation_mode'] == 'fixed-part-selection-v19':
            from . import investigation_fixed_part_answers_v19 as investigation_reviewed_claim_selection
        elif p['presentation_mode'] == 'fixed-part-selection-v18':
            from . import investigation_fixed_part_answers_v18 as investigation_reviewed_claim_selection
        elif p['presentation_mode'] == 'fixed-part-selection-v17':
            from . import investigation_fixed_part_answers_v17 as investigation_reviewed_claim_selection
        elif p['presentation_mode'] == 'fixed-part-selection-v16':
            from . import investigation_fixed_part_answers_v16 as investigation_reviewed_claim_selection
        elif p['presentation_mode'] == 'fixed-part-selection-v15':
            from . import investigation_fixed_part_answers_v15 as investigation_reviewed_claim_selection
        elif p['presentation_mode'] == 'fixed-part-selection-v14':
            from . import investigation_fixed_part_answers_v14 as investigation_reviewed_claim_selection
        elif p['presentation_mode'] == 'fixed-part-selection-v13':
            from . import investigation_fixed_part_answers_v13 as investigation_reviewed_claim_selection
        elif p['presentation_mode'] == 'fixed-part-selection-v12':
            from . import investigation_fixed_part_answers_v12 as investigation_reviewed_claim_selection
        elif p['presentation_mode'] == 'fixed-part-selection-v11':
            from . import investigation_fixed_part_answers_v11 as investigation_reviewed_claim_selection
        elif p['presentation_mode'] == 'fixed-part-selection-v10':
            from . import investigation_fixed_part_answers_v10 as investigation_reviewed_claim_selection
        elif p['presentation_mode'] == 'fixed-part-selection-v9':
            from . import investigation_fixed_part_answers_v9 as investigation_reviewed_claim_selection
        elif p['presentation_mode'] == 'fixed-part-selection-v8':
            from . import investigation_fixed_part_answers_v8 as investigation_reviewed_claim_selection
        elif p['presentation_mode'] == 'fixed-part-selection-v7':
            from . import investigation_fixed_part_answers_v7 as investigation_reviewed_claim_selection
        elif p['presentation_mode'] == 'fixed-part-selection-v6':
            from . import investigation_fixed_part_answers_v6 as investigation_reviewed_claim_selection
        elif p['presentation_mode'] == 'fixed-part-selection-v5':
            from . import investigation_fixed_part_answers_v5 as investigation_reviewed_claim_selection
        elif p['presentation_mode'] == 'fixed-part-selection-v4':
            from . import investigation_fixed_part_answers_v4 as investigation_reviewed_claim_selection
        elif p['presentation_mode'] == 'fixed-part-selection-v3':
            from . import investigation_fixed_part_answers_v3 as investigation_reviewed_claim_selection
        elif p['presentation_mode'] == 'fixed-part-selection-v2':
            from . import investigation_fixed_part_answers_v2 as investigation_reviewed_claim_selection
        elif p['presentation_mode'] == 'fixed-part-selection-v1':
            from . import investigation_fixed_part_answers as investigation_reviewed_claim_selection
        elif p['presentation_mode'] == 'reviewed-claim-selection-v16':
            from . import investigation_reviewed_claim_selection_v16 as investigation_reviewed_claim_selection
        elif p['presentation_mode'] == 'reviewed-claim-selection-v15':
            from . import investigation_reviewed_claim_selection_v15 as investigation_reviewed_claim_selection
        elif p['presentation_mode'] == 'reviewed-claim-selection-v14':
            from . import investigation_reviewed_claim_selection_v14 as investigation_reviewed_claim_selection
        elif p['presentation_mode'] == 'reviewed-claim-selection-v13':
            from . import investigation_reviewed_claim_selection_v13 as investigation_reviewed_claim_selection
        elif p['presentation_mode'] == 'reviewed-claim-selection-v12':
            from . import investigation_reviewed_claim_selection_v12 as investigation_reviewed_claim_selection
        elif p['presentation_mode'] == 'reviewed-claim-selection-v11':
            from . import investigation_reviewed_claim_selection_v11 as investigation_reviewed_claim_selection
        elif p['presentation_mode'] == 'reviewed-claim-selection-v10':
            from . import investigation_reviewed_claim_selection_v10 as investigation_reviewed_claim_selection
        elif p['presentation_mode'] == 'reviewed-claim-selection-v9':
            from . import investigation_reviewed_claim_selection_v9 as investigation_reviewed_claim_selection
        elif p['presentation_mode'] == 'reviewed-claim-selection-v8':
            from . import investigation_reviewed_claim_selection_v8 as investigation_reviewed_claim_selection
        elif p['presentation_mode'] == 'reviewed-claim-selection-v7':
            from . import investigation_reviewed_claim_selection_v7 as investigation_reviewed_claim_selection
        elif p['presentation_mode'] == 'reviewed-claim-selection-v6':
            from . import investigation_reviewed_claim_selection_v6 as investigation_reviewed_claim_selection
        elif p['presentation_mode'] == 'reviewed-claim-selection-v5':
            from . import investigation_reviewed_claim_selection_v5 as investigation_reviewed_claim_selection
        elif p['presentation_mode'] == 'reviewed-claim-selection-v4':
            from . import investigation_reviewed_claim_selection_v4 as investigation_reviewed_claim_selection
        elif p['presentation_mode'] == 'reviewed-claim-selection-v3':
            from . import investigation_reviewed_claim_selection_v3 as investigation_reviewed_claim_selection
        elif p['presentation_mode'] == 'reviewed-claim-selection-v2':
            from . import investigation_reviewed_claim_selection_v2 as investigation_reviewed_claim_selection
        else:
            from . import investigation_reviewed_claim_selection
        snippets, trace = investigation_reviewed_evidence.snippets(item['evidence'])
        if snippets != p.get('snippet_catalog') or trace != p.get('snippet_trace'):
            raise ValueError('Invalid original source trace')
        investigation_reviewed_claim_selection.validate_brief(item, snippets)
        raise ValueError('Automatic verification is unsupported for reviewed claim selection')
    if (p.get('template_version') != 'investigation-v21'
            or p.get('presentation_mode') not in ('cited-components-v1', 'cited-components-v2', 'cited-components-v3', 'cited-components-v4', 'cited-components-v5', 'cited-components-v6')
            or p.get('unknown_mode') != 'cited-unknown-v4'
            or p.get('reliability_mode') != 'bounded-evidence-v1'):
        raise ValueError('Draft verification requires the component contract')
    if p.get('snippet_trace', {}).get('version') == 'reviewed-paragraph-v1':
        from . import investigation_reviewed_evidence
        snippets, trace = investigation_reviewed_evidence.snippets(item['evidence'])
        structural_evidence = investigation_reviewed_evidence.flattened(item['evidence'])
    else:
        snippets, trace = investigation_context.snippets_from_evidence(item['evidence'], version='paragraph-v3')
        structural_evidence = item['evidence']
    if snippets != p.get('snippet_catalog') or trace != p.get('snippet_trace'):
        raise ValueError('Invalid original source trace')
    structure = investigation_structural_context.catalog(structural_evidence, snippets, version='ordered-list-v2')
    if structure != p.get('structural_trace'):
        raise ValueError('Invalid original structural trace')
    investigation_structural_answer.validate_original(item['brief'], p['answer_coverage'], snippets,
        structure, specific_unknown=True, unknown_mode=p['unknown_mode'], presentation_mode=p['presentation_mode'])
    return implementation.prepare(item)


def validate_reports(item):
    reports = item['provenance'].get('draft_verifications', [])
    if not isinstance(reports, list) or len(reports) > MAX_RUNS:
        raise ValueError('Invalid verification history')
    if not reports:
        return []
    envelopes = {}
    seen = set()
    for report in reports:
        if not isinstance(report, dict) or not isinstance(report.get('input'), dict):
            raise ValueError('Invalid verification binding')
        version = report['input'].get('version')
        implementation = _contract(version)
        if version not in envelopes:
            envelopes[version] = original_input(item, version=version)
        envelope = envelopes[version]
        if (not isinstance(report, dict) or set(report) != {'id', 'kind', 'created_at', 'input',
                'input_sha256', 'result', 'result_sha256', 'settings', 'input_budget', 'elapsed_ms'}
                or not isinstance(report.get('id'), str) or not report['id'] or report['id'] in seen
                or report.get('input_sha256') != contract.digest(envelope)
                or report.get('input') != envelope
                or report.get('kind') != 'model_assessment_unverified'):
            raise ValueError('Invalid verification binding')
        seen.add(report['id'])
        settings = report['settings']
        if (not isinstance(settings, dict) or set(settings) != {'profile', 'seed', 'model_slug',
                'model_sha256', 'max_tokens', 'timeout_seconds', 'generation_calls', 'input_policy'}
                or settings.get('profile') != PROFILE or type(settings.get('seed')) is not int
                or not 0 <= settings['seed'] <= 4294967295
                or settings.get('model_slug') != item['model_slug'] or settings.get('model_sha256') != item['model_sha']
                or settings.get('max_tokens') != OUTPUT_TOKENS or settings.get('timeout_seconds') != TIMEOUT_SECONDS
                or type(settings.get('generation_calls')) is not int or settings['generation_calls'] not in (0, 1)
                or settings.get('input_policy') != _input_policy(implementation)
                or type(report['elapsed_ms']) is not int or report['elapsed_ms'] < 0):
            raise ValueError('Invalid verification settings')
        try:
            if datetime.fromisoformat(report['created_at']).utcoffset() is None:
                raise ValueError('Missing verification timezone')
        except (TypeError, ValueError) as exc:
            raise ValueError('Invalid verification timestamp') from exc
        budget = report['input_budget']
        if budget is not None:
            messages = [{'role': 'system', 'content': implementation.INSTRUCTION},
                        {'role': 'user', 'content': json.dumps(envelope, ensure_ascii=False)}]
            if (not isinstance(budget, dict) or set(budget) != {'input_tokens', 'context_tokens',
                    'output_tokens', 'messages_sha256', 'schema_sha256'}
                    or any(type(budget.get(k)) is not int or budget[k] <= 0 for k in ('input_tokens', 'context_tokens'))
                    or budget.get('output_tokens') != OUTPUT_TOKENS
                    or budget.get('messages_sha256') != contract.digest(messages)
                    or budget.get('schema_sha256') != contract.digest(implementation.response_schema(envelope))):
                raise ValueError('Invalid verification budget')
        if not isinstance(report['result'], dict):
            raise ValueError('Invalid verification result')
        if report.get('result_sha256') != contract.digest(report.get('result')):
            raise ValueError('Invalid verification result')
        result = report['result']
        if 'checks' in result:
            if (settings['generation_calls'] != 1 or budget is None
                    or budget['input_tokens'] + OUTPUT_TOKENS > budget['context_tokens']):
                raise ValueError('Invalid verification generation')
            if implementation.decode(json.dumps(result['checks']), envelope) != result:
                raise ValueError('Invalid verification decision')
        elif result != _not_checked(implementation, envelope, result.get('reason')) or result['reason'] not in (
                            'capacity', 'timeout', 'invalid_output', 'inference_failed', 'model_changed', 'not_applicable'):
            raise ValueError('Invalid verification failure')
    return reports


async def verify_investigation(scope, iid, *, seed=7, version=contract.VERSION):
    """Keep draft/review ownership unchanged, even when the checker finds no issue."""
    implementation = _contract(version)
    if type(seed) is not int or not 0 <= seed <= 4294967295:
        raise ValueError('Invalid verification seed')
    item = await inv.get_investigation(scope, iid)
    if item['status'] != 'draft':
        raise ValueError('Only unreviewed drafts can be checked')
    reports = validate_reports(item)
    if len(reports) >= MAX_RUNS:
        raise ValueError('Verification attempt limit reached')
    envelope = original_input(item, version=version)
    schema = implementation.response_schema(envelope)
    messages = [{'role': 'system', 'content': implementation.INSTRUCTION},
                {'role': 'user', 'content': json.dumps(envelope, ensure_ascii=False)}]
    runtime = inv.get_runtime()
    model, sha = item['model_slug'], item['model_sha']
    if runtime.current_model_id() != model or runtime.current_model_sha256() != sha:
        raise ValueError('Requested model is not loaded locally')
    started = time.monotonic()
    budget = None
    settings = {'profile': PROFILE, 'seed': seed, 'model_slug': model, 'model_sha256': sha,
                'max_tokens': OUTPUT_TOKENS, 'timeout_seconds': TIMEOUT_SECONDS,
                'generation_calls': 0, 'input_policy': _input_policy(implementation)}
    result = _not_checked(implementation, envelope, 'not_applicable')
    token = prompt_cache.request_is_local.set(False)
    stage = 'inference'
    try:
        if envelope['groups']:
            async with asyncio.timeout(TIMEOUT_SECONDS):
                await runtime.validate_investigation_profile(PROFILE, expected_model=model, expected_sha256=sha)
                measurement = await runtime.measure_investigation_prompt(messages,
                    response_format={'type': 'json_object', 'schema': schema}, expected_model=model,
                    expected_sha256=sha, generation_profile=PROFILE)
                count, capacity = measurement['input_tokens'], measurement['context_tokens']
                if type(count) is not int or count <= 0 or type(capacity) is not int or capacity < 1:
                    raise ValueError('Invalid input measurement')
                budget = {'input_tokens': count, 'context_tokens': capacity, 'output_tokens': OUTPUT_TOKENS,
                          'messages_sha256': contract.digest(messages), 'schema_sha256': contract.digest(schema)}
                if count + OUTPUT_TOKENS > capacity:
                    result['reason'] = 'capacity'
                else:
                    settings['generation_calls'] = 1
                    reply = await runtime.chat(messages, temperature=0, max_tokens=OUTPUT_TOKENS,
                        expected_model=model, expected_sha256=sha, bounded_generation=True,
                        expected_context_tokens=capacity,
                        response_format={'type': 'json_object', 'schema': schema},
                        generation_profile=PROFILE, seed=seed)
                    if runtime.current_model_id() != model or runtime.current_model_sha256() != sha:
                        result['reason'] = 'model_changed'
                    else:
                        stage = 'decode'
                        choice = reply['choices'][0]
                        if choice.get('finish_reason') != 'stop':
                            raise ValueError('Incomplete verification output')
                        result = implementation.decode(choice['message']['content'], envelope)
    except asyncio.TimeoutError:
        result['reason'] = 'timeout'
    except (ValueError, KeyError, TypeError, IndexError):
        result = _not_checked(implementation, envelope, 'invalid_output' if stage == 'decode' else 'inference_failed')
    except Exception:
        # Transport implementations have different exception types. Cancellation
        # remains a BaseException and must propagate instead of saving a result.
        result['reason'] = 'inference_failed'
    finally:
        prompt_cache.request_is_local.reset(token)
    inv._privacy()
    report = {'id': str(uuid.uuid4()), 'kind': 'model_assessment_unverified',
              'created_at': datetime.now(timezone.utc).isoformat(), 'input': envelope,
              'input_sha256': contract.digest(envelope), 'result': result,
              'result_sha256': contract.digest(result), 'settings': settings, 'input_budget': budget,
              'elapsed_ms': round((time.monotonic() - started) * 1000)}
    provenance = deepcopy(item['provenance'])
    provenance['draft_verifications'] = [*reports, report]
    async with aiosqlite.connect(get_memory_db_path(), timeout=30) as db:
        await db.execute('BEGIN IMMEDIATE')
        inv._privacy()
        cursor = await db.execute('SELECT collection_id,document_id,revision_id,chunk_id,claim_id '
                                  'FROM investigation_bindings WHERE investigation_id=?', (iid,))
        if not await inv._bindings_valid(db, [tuple(row) for row in await cursor.fetchall()], scope):
            raise LookupError('Investigation unavailable')
        cursor = await db.execute("SELECT brief,evidence,provenance FROM investigations "
                                  "WHERE id=? AND workspace=? AND scope=? AND status='draft'",
                                  (iid, scope.workspace, json.dumps(sorted(scope.collection_ids))))
        current = await cursor.fetchone()
        if not current or any(json.loads(value) != item[key] for value, key in zip(current, ('brief', 'evidence', 'provenance'))):
            raise ValueError('Investigation changed during verification')
        await inv._validate_v15_current(db, scope, item)
        await db.execute('UPDATE investigations SET provenance=? WHERE id=?', (json.dumps(provenance), iid))
        await db.commit()
    return await inv.get_investigation(scope, iid)
