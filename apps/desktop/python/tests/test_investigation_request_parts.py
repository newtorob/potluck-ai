from copy import deepcopy

import pytest

from potluck_local.services import investigation_request_parts as parts


def test_application_assigns_ids_and_preserves_original_context():
    plan = parts.prepare('In the archived policy, what rule and expiry date?',
                         ['What rule applies?', 'What is the expiry date?'],
                         origin='reviewer_supplied')
    assert plan['parts'] == [
        {'id': 'P1', 'request': 'What rule applies?'},
        {'id': 'P2', 'request': 'What is the expiry date?'}]
    assert parts.validate(plan, question=plan['question'], request=plan['request']) == plan
    assert parts.prepare(plan['question'], [p['request'] for p in plan['parts']],
                         origin=plan['origin']) == plan


@pytest.mark.parametrize('origin', parts.ORIGINS)
def test_origin_never_establishes_question_coverage_or_owner_approval(origin):
    # A deliberately incomplete plan remains structurally valid and visibly unverified.
    plan = parts.prepare('What rule and expiry date?', ['What rule applies?'], origin=origin)
    assert plan['coverage'] == 'not_verified'
    assert 'approved' not in plan and 'accepted' not in plan


@pytest.mark.parametrize('rows', [[], ['x'] * 7, 'x', ('x',), [None], [{}],
                                 [' '], ['x' * 501], ['Rule?', '  RULE?\n']])
def test_malformed_or_lexically_duplicate_parts_fail(rows):
    with pytest.raises(ValueError):
        parts.prepare('Question?', rows, origin='model_proposed')


@pytest.mark.parametrize('question,scoped_request,origin', [
    ('', None, 'reviewer_supplied'), ('x' * 4001, None, 'reviewer_supplied'),
    ('Q?', '', 'reviewer_supplied'), ('Q?', True, 'reviewer_supplied'),
    ('Q?', None, []), ('Q?', None, 'owner_accepted'), (None, None, 'fixture_supplied')])
def test_invalid_context_or_authority_label_fails(question, scoped_request, origin):
    with pytest.raises(ValueError):
        parts.prepare(question, ['Detail?'], request=scoped_request, origin=origin)


def test_question_request_origin_order_and_content_bind_the_plan():
    base = parts.prepare('Question?', ['Rule?', 'Expiry?'], request='Selected policy?',
                         origin='fixture_supplied')
    variants = [parts.prepare('Other?', ['Rule?', 'Expiry?'], request='Selected policy?', origin='fixture_supplied'),
                parts.prepare('Question?', ['Rule?', 'Expiry?'], request='Other?', origin='fixture_supplied'),
                parts.prepare('Question?', ['Expiry?', 'Rule?'], request='Selected policy?', origin='fixture_supplied'),
                parts.prepare('Question?', ['Rule?', 'Owner?'], request='Selected policy?', origin='fixture_supplied'),
                parts.prepare('Question?', ['Rule?', 'Expiry?'], request='Selected policy?', origin='model_proposed')]
    assert all(p['plan_sha256'] != base['plan_sha256'] for p in variants)
    for field, value in [('question', 'Other?'), ('request', 'Other?'),
                         ('coverage', 'complete'), ('version', 'other'), ('plan_sha256', '0' * 64)]:
        bad = {**base, field: value}
        with pytest.raises(ValueError):
            parts.validate(bad)
    bad = deepcopy(base)
    bad['parts'][0]['id'] = 'model-id'
    with pytest.raises(ValueError):
        parts.validate(bad)
    with pytest.raises(ValueError):
        parts.validate(variants[0], question=base['question'])
    with pytest.raises(ValueError):
        parts.validate(variants[1], request=base['request'])


def test_no_mutable_alias_from_plan_validation():
    plan = parts.prepare('Q?', ['Rule?'], origin='fixture_supplied')
    checked = parts.validate(plan)
    checked['parts'][0]['request'] = 'Changed?'
    assert plan['parts'][0]['request'] == 'Rule?'
