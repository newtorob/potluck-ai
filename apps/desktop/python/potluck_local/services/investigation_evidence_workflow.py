"""Detached, bounded evidence assessment with separately sourced value comparisons."""
from copy import deepcopy
import json
import time
from types import SimpleNamespace

from . import investigation_evidence_comparisons as sources
from . import investigation_evidence_comparisons_service as bounded
from . import investigation_evidence_coverage as coverage
from . import investigation_evidence_coverage_v2 as coverage_v2
from . import investigation_evidence_coverage_v3 as coverage_v3
from . import investigation_revision as revision
from . import investigation_value_inventory as inventory
from . import investigation_value_inventory_v2 as inventory_v2
from . import investigation_value_inventory_v3 as inventory_v3
from . import investigation_value_inventory_v4 as inventory_v4
from . import investigation_value_inventory_v5 as inventory_v5
from . import investigation_value_inventory_v6 as inventory_v6
from . import investigation_value_inventory_v7 as inventory_v7
from . import investigation_value_inventory_v8 as inventory_v8

VERSION = 'evidence-workflow-v1'
V2_VERSION = 'evidence-workflow-v2'
V3_VERSION = 'evidence-workflow-v3'
V4_VERSION = 'evidence-workflow-v4'
V4_INPUT_VERSION = 'evidence-workflow-input-v4'
V5_VERSION = 'evidence-workflow-v5'
V5_INPUT_VERSION = 'evidence-workflow-input-v5'
V6_VERSION = 'evidence-workflow-v6'
V6_INPUT_VERSION = 'evidence-workflow-input-v6'
V7_VERSION = 'evidence-workflow-v7'
V7_INPUT_VERSION = 'evidence-workflow-input-v7'
V8_VERSION = 'evidence-workflow-v8'
V8_INPUT_VERSION = 'evidence-workflow-input-v8'
V9_VERSION = 'evidence-workflow-v9'
V9_INPUT_VERSION = 'evidence-workflow-input-v9'
V10_VERSION = 'evidence-workflow-v10'
V10_INPUT_VERSION = 'evidence-workflow-input-v10'
TIMEOUT_SECONDS = 360
MAX_CALLS = 3


def _version_contract(version):
    if version == VERSION:
        return coverage
    if version == V2_VERSION:
        return coverage_v2
    if version in (V3_VERSION, V4_VERSION, V5_VERSION, V6_VERSION, V7_VERSION, V8_VERSION, V9_VERSION, V10_VERSION):
        return coverage_v3
    raise ValueError('Invalid evidence workflow version')


def _prepared_contract(prepared):
    if not isinstance(prepared, dict):
        raise ValueError('Invalid workflow input')
    if prepared.get('version') in (V4_INPUT_VERSION, V5_INPUT_VERSION, V6_INPUT_VERSION, V7_INPUT_VERSION, V8_INPUT_VERSION, V9_INPUT_VERSION, V10_INPUT_VERSION):
        if set(prepared) != {'version', 'context'}:
            raise ValueError('Invalid workflow input fields')
        coverage_v3.response_schema(prepared['context'])
        return {V4_INPUT_VERSION: V4_VERSION, V5_INPUT_VERSION: V5_VERSION, V6_INPUT_VERSION: V6_VERSION, V7_INPUT_VERSION: V7_VERSION, V8_INPUT_VERSION: V8_VERSION, V9_INPUT_VERSION: V9_VERSION, V10_INPUT_VERSION: V10_VERSION}[prepared['version']], coverage_v3
    for version in (VERSION, V2_VERSION, V3_VERSION):
        implementation = _version_contract(version)
        if prepared.get('version') == implementation.VERSION:
            return version, implementation
    raise ValueError('Invalid workflow input version')


def prepare(envelope, *, version=None):
    context = _version_contract(VERSION if version is None else version).prepare(envelope)
    return {'version': {V4_VERSION: V4_INPUT_VERSION, V5_VERSION: V5_INPUT_VERSION, V6_VERSION: V6_INPUT_VERSION, V7_VERSION: V7_INPUT_VERSION, V8_VERSION: V8_INPUT_VERSION, V9_VERSION: V9_INPUT_VERSION, V10_VERSION: V10_INPUT_VERSION}[version], 'context': context} if version in (V4_VERSION, V5_VERSION, V6_VERSION, V7_VERSION, V8_VERSION, V9_VERSION, V10_VERSION) else context


def _context_input(prepared, version):
    return prepared['context'] if version in (V4_VERSION, V5_VERSION, V6_VERSION, V7_VERSION, V8_VERSION, V9_VERSION, V10_VERSION) else prepared


def _inventory_contract(version):
    return inventory_v8 if version == V10_VERSION else inventory_v7 if version == V9_VERSION else inventory_v6 if version == V8_VERSION else inventory_v5 if version == V7_VERSION else inventory_v4 if version == V6_VERSION else inventory_v3 if version == V5_VERSION else inventory_v2 if version == V4_VERSION else inventory


def _raw_context(result):
    return {'verdict': result['verdict'], 'units': result['model_classifications'],
            'dependencies': result['dependencies'],
            'comparisons': [{k: row[k] for k in ('kind', 'unit_ids')}
                            for row in result['comparisons']]}


def compose(prepared, context, scanned=None, matched=None):
    """Replay all component bindings before combining their unverified evidence."""
    try:
        version, implementation = _prepared_contract(prepared)
        context_input = _context_input(prepared, version)
        scanner = _inventory_contract(version)
        raw = _raw_context(context)
        if sources.common.digest(implementation.decode(json.dumps(raw), context_input)) != sources.common.digest(context):
            raise ValueError('Context assessment failed replay')
        value_input = sources.prepare(context_input['original'])
        scope = scanner.request_scope(value_input)
        skip_reason = 'outside_scope' if scope == 'outside_scope' else None
        if scope == 'outside_scope':
            if scanned is not None or matched is not None:
                raise ValueError('Unexpected comparison stage')
        else:
            if scanned is None:
                raise ValueError('Missing value inventory')
            scan_raw = scanner.raw_response(scanned) if version in (V9_VERSION, V10_VERSION) else {'classifications': scanned['classifications'], 'context_classifications': scanned['context_classifications']} if version in (V7_VERSION, V8_VERSION) else {'classifications': scanned['classifications'],
                        'type_context_units': scanned['type_context_units']} if version in (V4_VERSION, V5_VERSION, V6_VERSION, V7_VERSION, V8_VERSION, V9_VERSION, V10_VERSION) else {'facts': [{k: f[k] for k in ('unit_id', 'value')}
                                  for f in scanned['facts']],
                        'type_context_units': scanned['type_context_units']}
            if sources.common.digest(scanner.decode(json.dumps(scan_raw), value_input)) != sources.common.digest(scanned):
                raise ValueError('Inventory failed replay')
            if not scanned['facts']:
                if matched is not None:
                    raise ValueError('Unexpected matcher result')
                skip_reason = 'empty_inventory'
            else:
                match_input = scanner.prepare_match(value_input, scanned)
                if matched is None or sources.common.digest(scanner.match_decode(
                        json.dumps(matched['classifications']), match_input)) != sources.common.digest(matched):
                    raise ValueError('Matcher failed replay')
                if ('requested_fact' in matched['classifications'].values()
                        and context['verdict'] == 'not_stated'):
                    raise ValueError('Requested fact conflicts with absent-fact verdict')
                kind = matched['comparison_kind']
                if kind != 'none':
                    row = {'kind': kind, 'unit_ids': matched['unit_ids']}
                    # Preserve every context decision. Conflicting stages fail instead of
                    # quietly deleting evidence or turning absence into an answer.
                    if not any(c['kind'] == kind and set(c['unit_ids']) == set(row['unit_ids'])
                               for c in raw['comparisons']):
                        raw['comparisons'].append(row)
        result = implementation.decode(json.dumps(raw), context_input)
        result.update(version=version, kind='evidence_workflow_unverified',
                      semantic_coverage_verified=False,
                      context_assessment_sha256=sources.common.digest(context),
                      value_inventory_sha256=sources.common.digest(scanned) if scanned else None,
                      value_match_sha256=sources.common.digest(matched) if matched else None,
                      comparison_skip_reason=skip_reason,
                      component_versions={'context': implementation.VERSION,
                                          'inventory': scanner.VERSION,
                                          'match': scanner.MATCH_VERSION})
        return result
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError('Invalid workflow component') from exc


async def generate(prepared, runtime, *, model, model_sha, seed=7):
    """Use at most three local calls under one deadline, without implicit retries."""
    prepared = deepcopy(prepared)
    version, implementation = _prepared_contract(prepared)
    context_input = _context_input(prepared, version)
    scanner = _inventory_contract(version)
    implementation.response_schema(context_input)
    if type(seed) is not int or not 0 <= seed <= 4294967295:
        raise ValueError('Invalid workflow seed')
    if runtime.current_model_id() != model or runtime.current_model_sha256() != model_sha:
        raise ValueError('Requested model is not loaded locally')
    started = time.monotonic()
    attempt = {'version': version, 'input': prepared,
               'input_sha256': sources.common.digest(prepared), 'stages': [],
               'status': 'not_generated', 'reason': None, 'failed_stage': None,
               'generation_calls': 0, 'timeout_seconds': TIMEOUT_SECONDS,
               'maximum_generation_calls': MAX_CALLS}

    async def stage(name, data, implementation):
        remaining = TIMEOUT_SECONDS - (time.monotonic() - started)
        if remaining <= 0:
            record = {'status': 'not_generated', 'reason': 'workflow_timeout',
                      'generation_calls': 0, 'elapsed_ms': 0}
        else:
            try:
                record = await bounded.generate_contract(
                    data, runtime, implementation=implementation, model=model,
                    model_sha=model_sha, seed=seed,
                    timeout_seconds=min(bounded.TIMEOUT_SECONDS, remaining))
            except ValueError:
                record = {'status': 'not_generated', 'reason': 'stage_precondition_failed',
                          'generation_calls': 0, 'elapsed_ms': 0}
        attempt['stages'].append({'name': name, **record})
        attempt['generation_calls'] += record['generation_calls']
        if record['status'] != 'proposal':
            attempt.update(reason=record['reason'], failed_stage=name)
            return None
        return record['result']

    try:
        context = await stage('context', context_input, implementation)
        if context is None:
            return attempt
        value_input = sources.prepare(context_input['original'])
        scanned = matched = None
        if scanner.request_scope(value_input) != 'outside_scope':
            scanned = await stage('inventory', value_input, scanner)
            if scanned is None:
                return attempt
            if scanned['facts']:
                match_contract = SimpleNamespace(
                    VERSION=scanner.MATCH_VERSION, messages=scanner.match_messages,
                    response_schema=scanner.match_response_schema, decode=scanner.match_decode)
                matched = await stage('match', scanner.prepare_match(value_input, scanned), match_contract)
                if matched is None:
                    return attempt
        try:
            result = compose(prepared, context, scanned, matched)
        except ValueError:
            attempt.update(reason='composition_failed', failed_stage='composition')
        else:
            attempt.update(status='proposal', result=result)
        return attempt
    finally:
        attempt['elapsed_ms'] = round((time.monotonic() - started) * 1000)


async def propose(scope, iid, group_id, *, seed=7, version=None):
    """Return an opt-in detached proposal only while the scoped draft is unchanged."""
    from . import investigations as inv

    _version_contract(VERSION if version is None else version)
    item = await inv.get_investigation(scope, iid)
    if item['status'] != 'draft':
        raise ValueError('Only unreviewed drafts can receive proposals')
    envelopes = revision.prepare_original(item)
    envelope = next((e for e in envelopes if e['group']['id'] == group_id), None)
    if envelope is None:
        raise ValueError('Unknown or inapplicable revision group')
    attempt = await generate(prepare(envelope, version=version), inv.get_runtime(),
                             model=item['model_slug'], model_sha=item['model_sha'], seed=seed)
    current = await inv.get_investigation(scope, iid)
    fields = ('status', 'brief', 'evidence', 'provenance', 'review', 'model_slug', 'model_sha')
    if any(current.get(key) != item.get(key) for key in fields):
        raise ValueError('Investigation changed during revision')
    return {'investigation_id': iid, 'original_sha256': sources.common.digest(item), 'attempt': attempt}
