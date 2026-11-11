"""Opt-in evidence coverage with a scoped source-comparison gate."""
from copy import deepcopy
import json

from . import investigation_evidence_coverage as v1
from . import investigation_evidence_comparisons as sources
from . import investigation_value_inventory as inventory
from .investigation_planned_answers import _unique_object

VERSION = 'evidence-coverage-v2'
POLICY_INSTRUCTION = (
    '\nThis selected question is outside version and identifier comparison scope. '
    'Return comparisons as an empty array. Represent policy qualifiers using unit '
    'classifications and dependencies.'
)


def prepare(envelope):
    result = v1.prepare(envelope)
    result['version'] = VERSION
    return result


def _previous_input(prepared):
    try:
        if sources.common.digest(prepare(prepared['original'])) != sources.common.digest(prepared):
            raise ValueError('Evidence input differs from its source envelope')
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError('Invalid evidence input') from exc
    result = deepcopy(prepared)
    result['version'] = v1.VERSION
    return result


def _scope(prepared):
    return inventory.request_scope(sources.prepare(prepared['original']))


def messages(prepared):
    result = v1.messages(_previous_input(prepared))
    payload = json.loads(result[1]['content'])
    payload['version'] = VERSION
    if _scope(prepared) == 'outside_scope':
        result[0]['content'] += POLICY_INSTRUCTION
    result[1]['content'] = json.dumps(payload, ensure_ascii=False)
    return result


def response_schema(prepared):
    result = v1.response_schema(_previous_input(prepared))
    if _scope(prepared) == 'outside_scope':
        result['properties']['comparisons']['maxItems'] = 0
    return result


def decode(raw, prepared):
    previous = _previous_input(prepared)
    if _scope(prepared) == 'outside_scope':
        try:
            if not isinstance(raw, str) or len(raw) > 14000:
                raise ValueError('Invalid evidence response')
            data = json.loads(raw, object_pairs_hook=_unique_object)
            if not isinstance(data, dict) or data.get('comparisons') != []:
                raise ValueError('Comparisons are outside the selected question scope')
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError('Invalid evidence response') from exc
    result = v1.decode(raw, previous)
    result['version'] = VERSION
    result['input_sha256'] = sources.common.digest(prepared)
    return result
