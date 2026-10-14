"""Token counts from text length: about four characters a token, rounded up."""
from __future__ import annotations

import pytest

from potluck_local.services import anthropic_messages as am
from potluck_local.services import token_estimate as te


@pytest.mark.parametrize("chars, tokens", [(0, 0), (1, 1), (3, 1), (4, 1), (5, 2), (8, 2), (9, 3), (4001, 1001)])
def test_about_four_characters_a_token_rounded_up(chars, tokens):
    assert te.tokens_for_chars(chars) == tokens
    assert te.estimate_tokens("x" * chars) == tokens


@pytest.mark.parametrize("value, want", [(0, 0), (7, 7), (-1, None), (True, None), (2.0, None), ("3", None), (None, None)])
def test_an_engine_count_is_a_whole_number_of_zero_or_more(value, want):
    assert te.engine_count(value) == want


def test_the_prompt_text_holds_messages_calls_and_tools_as_the_messages_route_has_always_counted_them():
    call = {"id": "c", "type": "function", "function": {"name": "get_weather", "arguments": {"city": "Paris"}}}
    messages = [{"role": "system", "content": "be brief"}, {"role": "user", "content": "q"},
                {"role": "assistant", "content": "", "tool_calls": [call]}, {"role": "tool", "content": "sunny", "tool_call_id": "c"}]
    tools = [{"type": "function", "function": {"name": "get_weather"}}]
    text = te.prompt_text(messages, tools)
    assert text == ('be brief\nq\n\nget_weather\n{"city": "Paris"}\nsunny\n'
                    '[{"type": "function", "function": {"name": "get_weather"}}]')
    req = am.parse_request({"model": "m", "messages": [{"role": "user", "content": "q"}]}, default_max_tokens=1, default_temperature=0.7)
    assert am.prompt_text(req) == te.prompt_text(req.prepared.messages, req.prepared.tools) == "q"


def test_the_messages_translation_still_offers_the_helpers_it_always_did():
    assert am.tokens_for_chars(9) == 3
