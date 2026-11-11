"""Opt-in evidence assessment that preserves the scope of governing policy rules."""
from copy import deepcopy
import json

from . import investigation_evidence_comparisons_v2 as previous

VERSION = 'evidence-coverage-v1'
COVERAGE_INSTRUCTION = (
    '\nFor policy comparisons, retain the general rule governing each side as well as its '
    'exceptions. An exception does not replace the general rule. When a selected use is '
    'funded from a budget, retain the budget amount, eligibility, proration, approval '
    'requirements and expiry or carryover restrictions that govern that use. Link the '
    'selected use to these prerequisite or limiting units. Do not mark a governing rule '
    'as background merely because another unit states a more specific use or exception. '
    'Exclude unrelated examples and benefits. These instructions apply only to rules '
    'that govern the selected question, not other parts of the full request.'
)


def prepare(envelope):
    result = previous.prepare(envelope)
    result['version'] = VERSION
    return result


def _previous_input(prepared):
    try:
        if previous.v1.common.digest(prepare(prepared['original'])) != previous.v1.common.digest(prepared):
            raise ValueError('Evidence input differs from its source envelope')
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError('Invalid evidence input') from exc
    result = deepcopy(prepared)
    result['version'] = previous.VERSION
    return result


def messages(prepared):
    result = previous.messages(_previous_input(prepared))
    result[0]['content'] += COVERAGE_INSTRUCTION
    payload = json.loads(result[1]['content'])
    payload['version'] = VERSION
    result[1]['content'] = json.dumps(payload, ensure_ascii=False)
    return result


def response_schema(prepared):
    return previous.response_schema(_previous_input(prepared))


def decode(raw, prepared):
    result = previous.decode(raw, _previous_input(prepared))
    result['version'] = VERSION
    result['input_sha256'] = previous.v1.common.digest(prepared)
    return result
