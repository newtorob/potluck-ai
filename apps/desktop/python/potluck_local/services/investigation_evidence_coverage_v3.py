"""Opt-in coverage guidance for explicit negative facts and procedural context."""
from copy import deepcopy
import json

from . import investigation_evidence_coverage_v2 as v2
from . import investigation_evidence_comparisons as sources
from . import investigation_section_context as sections

VERSION = 'evidence-coverage-v3'
COVERAGE_INSTRUCTION = (
    '\nAn explicit negative or unimplemented state in a supplied source is a stated fact, '
    'not an absence of evidence. Use not_stated only when the requested fact is not stated '
    'in the supplied records; preserve any stated qualifications and uncertainty. For a '
    'compound selected question, retain evidence for each requested part, including '
    'procedural stages and timing as well as general requirements. Consider governing source '
    'headings and list introductions, including exclusions, when interpreting their units. '
    'For a selected group, cover only that group; do not infer facts or missing context. '
    'These are unverified model assessments and source selection is not proof of correctness.'
)


def prepare(envelope):
    result = v2.prepare(envelope)
    result['version'] = VERSION
    return result


def _previous_input(prepared):
    try:
        if sources.common.digest(prepare(prepared['original'])) != sources.common.digest(prepared):
            raise ValueError('Evidence input differs from its source envelope')
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError('Invalid evidence input') from exc
    result = deepcopy(prepared)
    result['version'] = v2.VERSION
    return result


def messages(prepared):
    result = v2.messages(_previous_input(prepared))
    result[0]['content'] += COVERAGE_INSTRUCTION
    payload = json.loads(result[1]['content'])
    payload['version'] = VERSION
    result[1]['content'] = json.dumps(payload, ensure_ascii=False)
    return result


def response_schema(prepared):
    return v2.response_schema(_previous_input(prepared))


def decode(raw, prepared):
    result = v2.decode(raw, _previous_input(prepared))
    result['version'] = VERSION
    result['input_sha256'] = sources.common.digest(prepared)
    return sections.retain(prepared, result)
