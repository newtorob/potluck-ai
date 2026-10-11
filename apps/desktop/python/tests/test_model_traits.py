"""What a chat template can do is found by rendering it."""
from __future__ import annotations

from pathlib import Path

import pytest

from potluck_local.services import model_traits as mt
from potluck_local.services.model_traits import TemplateTraits

# The tool section and message loop of the Qwen 2.5 template (qwen-2.5-0.5b-instruct-q4), shortened.
QWEN_TOOLS = """{%- if tools %}
    {{- '<|im_start|>system\\n' }}
    {{- "\\n\\n# Tools\\n\\n<tools>" }}
    {%- for tool in tools %}
        {{- "\\n" }}
        {{- tool | tojson }}
    {%- endfor %}
    {{- "\\n</tools><|im_end|>\\n" }}
{%- endif %}
{%- for message in messages %}
    {{- '<|im_start|>' + message.role + '\\n' + message.content + '<|im_end|>\\n' }}
{%- endfor %}
{%- if add_generation_prompt %}{{- '<|im_start|>assistant\\n' }}{%- endif %}"""

# No tool section: the variable is mentioned in a comment, which does not make the template use it.
GEMMA_LIKE = ("{# tools are not supported #}{%- for m in messages %}<start_of_turn>{{ m.role }}\n{{ m.content }}<end_of_turn>\n"
              "{%- endfor %}{%- if add_generation_prompt %}<start_of_turn>model\n{%- endif %}")

# Phi-4 mini reads tools from the system message, not from the `tools` variable.
SYSTEM_TOOLS = ("{%- for m in messages %}{{ m.role }}: {{ m.content }}{%- if m.tools %} <|tool|>{{ m.tools }}<|/tool|>{%- endif %}\n"
                "{%- endfor %}{%- if add_generation_prompt %}assistant: {%- endif %}")

# A DeepSeek R1 style template: the prompt ends inside the reasoning block.
R1_LIKE = ("{%- for m in messages %}<|User|>{{ m.content }}{%- endfor %}"
           "{%- if add_generation_prompt %}<|Assistant|><think>\n{%- endif %}")

# A Qwen3 style hybrid: the model opens the block itself unless thinking is switched off.
QWEN3_LIKE = (QWEN_TOOLS.replace("{{- '<|im_start|>assistant\\n' }}",
              "{{- '<|im_start|>assistant\\n' }}{%- if enable_thinking is defined and enable_thinking is false %}"
              "{{- '<think>\\n\\n</think>\\n\\n' }}{%- endif %}"))


class FakeModel:
    def __init__(self, template=None, chat_format=None, with_metadata=True):
        if with_metadata:
            self.metadata = {"tokenizer.chat_template": template} if template is not None else {}
        if chat_format is not None:
            self.chat_format = chat_format


def test_a_template_that_renders_tools_supports_them():
    assert mt.traits_of_template(QWEN_TOOLS) == TemplateTraits(supports_tools=True)


def test_a_template_that_never_uses_the_tools_variable_does_not():
    assert mt.traits_of_template(GEMMA_LIKE).supports_tools is False


def test_a_template_that_keeps_tools_somewhere_else_does_not_count():
    assert mt.traits_of_template(SYSTEM_TOOLS).supports_tools is False


def test_a_template_that_ends_its_prompt_inside_a_think_block_opens_reasoning():
    t = mt.traits_of_template(R1_LIKE)
    assert t.opens_reasoning and t.thinks and not t.supports_tools


def test_a_hybrid_thinking_template_thinks_but_does_not_open_the_block_itself():
    t = mt.traits_of_template(QWEN3_LIKE)
    assert t.thinks and t.supports_tools and not t.opens_reasoning


def test_an_unreadable_template_supports_nothing_and_does_not_raise():
    assert mt.traits_of_template("{% if %}") == TemplateTraits()
    assert mt.traits_of_template("{% if enable_thinking %}{% if %}").thinks is True  # the switch is still seen in the text
    assert mt.traits_of_template("{{ raise_exception('no') }}") == TemplateTraits()


def test_a_model_reports_its_templates_traits():
    assert mt.traits_of_model(FakeModel(QWEN_TOOLS)).supports_tools is True
    assert mt.traits_of_model(FakeModel(QWEN_TOOLS, chat_format="chat_template.default")).supports_tools is True


def test_a_builtin_chat_format_ignores_tools_whatever_the_metadata_says():
    assert mt.traits_of_model(FakeModel(QWEN_TOOLS, chat_format="llama-3")) == TemplateTraits()


def test_a_model_with_no_template_supports_nothing():
    assert mt.traits_of_model(FakeModel(None)) == TemplateTraits()


def test_a_backend_without_metadata_cannot_say():
    assert mt.traits_of_model(FakeModel(with_metadata=False)) is None
    assert mt.traits_of_model(None) is None


def test_runtime_traits_never_raise():
    class Boom:
        def template_traits(self):
            raise RuntimeError("x")

    class Fine:
        def template_traits(self):
            return TemplateTraits(supports_tools=True)

    assert mt.traits_of_runtime(Boom()) is None
    assert mt.traits_of_runtime(object()) is None
    assert mt.traits_of_runtime(Fine()).supports_tools is True


# ---------------------------------------------------------------------------
# Fitting a template: Llama 3.x tool instructions, and two calls in one message
# ---------------------------------------------------------------------------

# The chat template inside Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf, as the model file carries it.
LLAMA31 = Path(__file__).with_name("llama31_chat_template.jinja").read_text()
EOT, BOT = "<|eot_id|>", "<|begin_of_text|>"
WEATHER = {"type": "function", "function": {"name": "get_weather", "description": "Current weather for a city",
           "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}}
ASK = {"role": "user", "content": "What's the weather in Paris?"}


def _call(call_id, name="get_weather", **arguments):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


def _prompt(template, messages, tools=None):
    from llama_cpp.llama_chat_format import Jinja2ChatFormatter

    kwargs = {"tools": tools} if tools else {}
    return Jinja2ChatFormatter(template=template, eos_token=EOT, bos_token=BOT)(messages=messages, **kwargs).prompt


def test_the_llama_3_template_as_shipped_writes_the_tool_instructions_into_the_users_message():
    user_turn = _prompt(LLAMA31, [ASK], [WEATHER]).split("<|start_header_id|>user<|end_header_id|>")[1]
    assert "please respond with a JSON for a function call" in user_turn and "get_weather" in user_turn


def test_the_fitted_llama_3_template_keeps_them_in_the_system_message_and_leaves_the_users_words_alone():
    fitted = mt.fitted_template(LLAMA31, BOT, EOT)
    assert fitted != LLAMA31
    system, user_turn = _prompt(fitted, [ASK], [WEATHER]).split("<|start_header_id|>user<|end_header_id|>")
    assert "get_weather" in system and "You have access to the following functions" in system
    assert user_turn == "\n\nWhat's the weather in Paris?<|eot_id|><|start_header_id|>assistant<|end_header_id|>\n\n"


def test_a_request_without_tools_renders_the_same_with_the_fitted_template():
    fitted = mt.fitted_template(LLAMA31, BOT, EOT)
    for messages in ([ASK], [{"role": "system", "content": "Be brief."}, ASK],
                     [ASK, {"role": "assistant", "content": "Mild."}, {"role": "user", "content": "And London?"}]):
        assert _prompt(fitted, messages) == _prompt(LLAMA31, messages)


def test_the_tool_instructions_stay_in_the_system_message_on_the_turn_after_a_result():
    history = [ASK, {"role": "assistant", "content": "", "tool_calls": [_call("a", city="Paris")]},
               {"role": "tool", "tool_call_id": "a", "content": '{"temp_c": 17}'}]
    prompt = _prompt(mt.fitted_template(LLAMA31, BOT, EOT), history, [WEATHER])
    system, rest = prompt.split("<|start_header_id|>user<|end_header_id|>")
    assert "get_weather" in system
    assert "respond with" not in rest and rest.startswith("\n\nWhat's the weather in Paris?<|eot_id|>")
    assert rest.endswith("<|start_header_id|>assistant<|end_header_id|>\n\n")


def test_a_template_without_the_switch_is_used_as_it_is():
    assert mt.fitted_template(QWEN_TOOLS) == QWEN_TOOLS
    assert mt.fitted_template(GEMMA_LIKE) == GEMMA_LIKE


def test_the_move_is_dropped_when_it_would_change_a_request_without_tools():
    # Mentions the switch, but prints something different once it is set: not the template we know.
    odd = "{%- if tools_in_user_message is defined %}X{%- endif %}{%- for m in messages %}{{ m.content }}{%- endfor %}"
    assert mt.fitted_template(odd) == odd


def test_the_move_is_dropped_when_the_tools_do_not_end_up_ahead_of_the_users_turn():
    # The switch is there and requests without tools are unchanged, but tools still follow the user's words.
    odd = ("{%- if not tools_in_user_message is defined %}{%- endif %}"
           "{%- for m in messages %}{{ m.content }}|{%- endfor %}{%- if tools %}{{ tools | tojson }}{%- endif %}")
    assert mt.fitted_template(odd) == odd


def test_the_llama_3_template_takes_one_call_per_message_and_the_traits_say_so():
    assert mt.traits_of_template(LLAMA31, BOT, EOT) == TemplateTraits(supports_tools=True, single_tool_call=True)
    assert mt.traits_of_template(QWEN_TOOLS).single_tool_call is False


def test_a_template_that_refuses_tool_history_altogether_is_not_called_single_call():
    refuses = QWEN_TOOLS.replace("{%- for message in messages %}",
                                 "{%- for message in messages %}{%- if message.role == 'tool' %}{{ raise_exception('no') }}{%- endif %}")
    assert mt.traits_of_template(refuses).supports_tools is True
    assert mt.traits_of_template(refuses).single_tool_call is False


TWO = {"role": "assistant", "content": "Looking both up.", "tool_calls": [_call("a", city="Paris"), _call("b", "get_time", zone="Europe/Paris")]}
RESULT_A = {"role": "tool", "tool_call_id": "a", "content": "17"}
RESULT_B = {"role": "tool", "tool_call_id": "b", "content": "14:32"}


def test_two_calls_in_one_message_become_one_call_and_its_result_at_a_time():
    out = mt.split_tool_calls([ASK, TWO, RESULT_B, RESULT_A, {"role": "user", "content": "Thanks"}])
    assert [m["role"] for m in out] == ["user", "assistant", "tool", "assistant", "tool", "user"]
    assert out[1]["tool_calls"] == [TWO["tool_calls"][0]] and out[1]["content"] == "Looking both up."
    assert out[2] is RESULT_A  # matched by id, not by the order they were sent in
    assert out[3]["tool_calls"] == [TWO["tool_calls"][1]] and out[3]["content"] == ""
    assert out[4] is RESULT_B


def test_results_without_ids_are_matched_in_order():
    first, second = {"role": "tool", "content": "17"}, {"role": "tool", "content": "14:32"}
    out = mt.split_tool_calls([ASK, TWO, first, second])
    assert out[2] is first and out[4] is second


def test_a_missing_result_leaves_the_calls_in_order_and_an_extra_one_is_kept():
    assert [m["role"] for m in mt.split_tool_calls([ASK, TWO, RESULT_A])] == ["user", "assistant", "tool", "assistant"]
    extra = {"role": "tool", "tool_call_id": "zzz", "content": "?"}
    out = mt.split_tool_calls([ASK, {**TWO, "tool_calls": [dict(c) for c in TWO["tool_calls"]]}, RESULT_A, RESULT_B, extra])
    assert out[-1] is extra and len(out) == 6


def test_messages_with_one_call_or_none_are_passed_through_untouched():
    one = {"role": "assistant", "content": "", "tool_calls": [_call("a", city="Paris")]}
    messages = [ASK, one, RESULT_A, {"role": "assistant", "content": "17 degrees."}]
    out = mt.split_tool_calls(messages)
    assert out == messages and all(a is b for a, b in zip(out, messages))


def test_the_split_history_renders_where_the_template_refused_the_original():
    history = [ASK, {**TWO, "tool_calls": [_call("a", city="Paris"), _call("b", city="London")]}, RESULT_A, RESULT_B]
    with pytest.raises(ValueError, match="single tool-calls"):
        _prompt(LLAMA31, history, [WEATHER])
    prompt = _prompt(mt.fitted_template(LLAMA31, BOT, EOT), mt.split_tool_calls(history), [WEATHER])
    calls = prompt.split("<|start_header_id|>user<|end_header_id|>")[1]
    assert calls.index('"city": "Paris"') < calls.index("ipython<|end_header_id|>\n\n\"17\"") < calls.index('"city": "London"')


class LoadedModel:
    """The parts of a llama-cpp-python model that fitting reads."""

    def __init__(self, template, chat_format="chat_template.default", chat_handler=None):
        self.metadata = {"tokenizer.chat_template": template}
        self.chat_format = chat_format
        self.chat_handler = chat_handler
        self._model = self

    def token_bos(self):
        return 1

    def token_eos(self):
        return 2

    def token_get_text(self, token):
        return {1: BOT, 2: EOT}[token]


def test_a_loaded_llama_3_model_gets_a_fitted_handler_and_others_keep_their_own():
    llama = LoadedModel(LLAMA31)
    assert mt.fit_model(llama) is True and callable(llama.chat_handler)
    qwen = LoadedModel(QWEN_TOOLS)
    assert mt.fit_model(qwen) is False and qwen.chat_handler is None


def test_fitting_leaves_a_builtin_format_and_a_handler_already_set_alone():
    builtin = LoadedModel(LLAMA31, chat_format="llama-3")
    assert mt.fit_model(builtin) is False and builtin.chat_handler is None
    mine = object()
    kept = LoadedModel(LLAMA31, chat_handler=mine)
    assert mt.fit_model(kept) is False and kept.chat_handler is mine


def test_fitting_never_raises():
    assert mt.fit_model(None) is False
    assert mt.fit_model(object()) is False
    broken = LoadedModel(LLAMA31)
    broken.token_eos = lambda: (_ for _ in ()).throw(RuntimeError("x"))
    assert mt.fit_model(broken) is False and broken.chat_handler is None


def test_the_fitted_handler_splits_two_call_messages_before_the_template_sees_them():
    seen = {}

    def inner(*, messages, **kwargs):
        seen.update(messages=messages, kwargs=kwargs)
        return "reply"

    handler = mt._splitting(inner)  # noqa: SLF001
    assert handler(llama="m", messages=[ASK, TWO, RESULT_A, RESULT_B], tools=[WEATHER], stream=False) == "reply"
    assert [m["role"] for m in seen["messages"]] == ["user", "assistant", "tool", "assistant", "tool"]
    assert seen["kwargs"] == {"llama": "m", "tools": [WEATHER], "stream": False}
