"""Opt-in explicit-relation adapter with the shared exact-source lifecycle checks."""
from . import investigation_fixed_part_answers as base
from . import investigation_part_selection_v20 as selection

VERSION = selection.VERSION
MAX_CLAIMS = base.MAX_CLAIMS
MAX_UNITS = base.MAX_UNITS
MAX_SELECTED_UNITS = base.MAX_SELECTED_UNITS
MAX_VISIBLE_CHARS = base.MAX_VISIBLE_CHARS
messages = selection.messages
response_schema = selection.response_schema
decode_selection = selection.decode_selection
review_messages = selection.review_messages
review_schema = selection.review_schema
corrected_brief = base.corrected_brief


def prepare(question, evidence, request=None, *, question_part_plan, unit_plans=None):
    return selection.prepare(question, evidence, question_part_plan,
                             request=request, unit_plans=unit_plans)


def input_receipt(prepared):
    return base.input_receipt(prepared, _contract=selection)


def review_receipt(prepared, first):
    return base.review_receipt(prepared, first, _contract=selection)


def make_brief(question, operation, evidence, snippets, raw_bundle, *,
               question_part_plan, unit_plans=None):
    return base.make_brief(question, operation, evidence, snippets, raw_bundle,
                           question_part_plan=question_part_plan, unit_plans=unit_plans,
                           _contract=selection)


def validate_brief(item, snippets):
    return base.validate_brief(item, snippets, _contract=selection)
