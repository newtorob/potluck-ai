"""What a chat template can do is found by rendering it."""
from __future__ import annotations

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
