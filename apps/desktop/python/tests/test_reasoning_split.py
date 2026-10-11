"""Splitting a model's reasoning from its answer: every shape, every chunk boundary.

The recorded shapes come from qwen3-8b-q4: with /no_think it sends an empty block
and then the answer; with thinking on, the block holds several sentences.
"""
from __future__ import annotations

import itertools
import random

import pytest

from potluck_local.services.reasoning_split import (
    CLOSE, CONTENT, MAX_HELD, OPEN, REASONING, ReasoningSplitter, split_reasoning,
)

ANSWER = "A mesh network is a network where every node relays data for the others."
THINKING_ON = ("<think>\nOkay, the user wants to know what a mesh network is. Let me think. "
               "Every node relays for the others, so one failure does not break it.\n</think>\n\n" + ANSWER)

# (name, text, starts_in_reasoning, reasoning, content)
SHAPES = [
    ("no_think_empty_block", "<think>\n\n</think>\n\n" + ANSWER, False, "", ANSWER),
    ("thinking_on", THINKING_ON, False,
     "Okay, the user wants to know what a mesh network is. Let me think. "
     "Every node relays for the others, so one failure does not break it.", ANSWER),
    ("template_opened_block", "Okay, so the sum is 391.\n</think>\n\nThe answer is 391.", True,
     "Okay, so the sum is 391.", "The answer is 391."),
    ("template_opened_and_model_opens_again", "<think>\nHmm.\n</think>\n\n391", True, "Hmm.", "391"),
    ("cut_off_inside_block", "<think>\nOkay, so the user asks about", False, "Okay, so the user asks about", ""),
    ("cut_off_inside_opened_block", "Okay, so the user asks about", True, "Okay, so the user asks about", ""),
    ("only_the_opener", "<think>", False, "", ""),
    ("leading_whitespace_before_opener", "\n  <think>\nthinking\n</think>\nanswer", False, "thinking", "answer"),
    ("no_block_at_all", ANSWER, False, "", ANSWER),
    ("literal_tag_later_in_answer", "Use the <think> tag to open a block, and </think> to close it.", False, "",
     "Use the <think> tag to open a block, and </think> to close it."),
    ("second_block_after_the_first_is_text", "<think>a</think>b <think>c</think> d", False, "a", "b <think>c</think> d"),
    ("close_tag_without_opener_when_template_does_not_open", "</think>answer", False, "", "</think>answer"),
    ("tag_prefix_that_is_not_a_tag", "<thanks> for asking", False, "", "<thanks> for asking"),
    ("reasoning_with_angle_brackets", "<think>if a < b and b > c then</think>ok", False, "if a < b and b > c then", "ok"),
    ("code_indentation_after_block", "<think>x</think>\n\n    indented()", False, "x", "indented()"),
    ("empty_reply", "", False, "", ""),
    ("whitespace_reply", "  \n", False, "", "  \n"),
]


def stream(text: str, cuts: list[int], *, starts_in_reasoning: bool | None = False) -> tuple[str, str]:
    splitter = ReasoningSplitter(starts_in_reasoning=starts_in_reasoning)
    pieces: list[tuple[str, str]] = []
    last = 0
    for cut in [*cuts, len(text)]:
        pieces += splitter.feed(text[last:cut])
        last = cut
    pieces += splitter.finish()
    return (
        "".join(t for kind, t in pieces if kind == REASONING),
        "".join(t for kind, t in pieces if kind == CONTENT),
    )


@pytest.mark.parametrize("name,text,opened,reasoning,content", SHAPES, ids=[s[0] for s in SHAPES])
def test_whole_reply(name, text, opened, reasoning, content):
    assert split_reasoning(text, starts_in_reasoning=opened) == (reasoning, content)


@pytest.mark.parametrize("name,text,opened,reasoning,content", SHAPES, ids=[s[0] for s in SHAPES])
def test_stream_cut_at_every_position_gives_the_same_split(name, text, opened, reasoning, content):
    for i in range(len(text) + 1):
        assert stream(text, [i], starts_in_reasoning=opened) == (reasoning, content), f"one cut at {i}"
    for i, j in itertools.combinations(range(len(text) + 1), 2):
        assert stream(text, [i, j], starts_in_reasoning=opened) == (reasoning, content), f"cuts at {i}, {j}"
    assert stream(text, list(range(1, len(text))), starts_in_reasoning=opened) == (reasoning, content), "one character at a time"


def test_a_tag_split_inside_its_name_never_leaks_half_a_tag():
    """"</thi" must wait for "nk>", and "<th" for "ink>", so neither reaches the output."""
    splitter = ReasoningSplitter()
    seen: list[tuple[str, str]] = []
    for piece in ("<th", "ink>reason", "ing</th", "ink>", "ans", "wer"):
        seen += splitter.feed(piece)
    seen += splitter.finish()
    for _, text in seen:
        assert "<th" not in text and "</th" not in text and OPEN not in text and CLOSE not in text
    assert [t for k, t in seen if k == REASONING] == ["reason", "ing"]
    assert "".join(t for k, t in seen if k == CONTENT) == "answer"


def test_reasoning_streams_as_it_arrives_and_trailing_space_waits():
    splitter = ReasoningSplitter()
    assert splitter.feed("<think>") == []
    assert splitter.feed("One ") == [(REASONING, "One")]  # the space waits until more text shows it is inside
    assert splitter.feed("two") == [(REASONING, " two")]
    assert splitter.feed("\n</think>\n\nDone") == [(CONTENT, "Done")]
    assert splitter.feed("!") == [(CONTENT, "!")]


def test_a_plain_reply_is_released_at_once_and_unchanged():
    splitter = ReasoningSplitter()
    assert splitter.feed("Hello") == [(CONTENT, "Hello")]
    assert splitter.feed(" there <think>") == [(CONTENT, " there <think>")]
    assert splitter.finish() == []


def test_leading_whitespace_of_a_plain_reply_is_kept():
    splitter = ReasoningSplitter()
    assert splitter.feed("\n") == []  # could still become "<think>"
    assert splitter.feed("Hi") == [(CONTENT, "\nHi")]


def test_unknown_template_splits_a_whole_reply_with_a_lone_close_tag_but_a_stream_cannot():
    text = "Okay, working it out.\n</think>\n\n391"
    assert split_reasoning(text, starts_in_reasoning=None) == ("Okay, working it out.", "391")
    # A stream has already sent the reasoning as answer text when the tag arrives.
    assert stream(text, [5, 20], starts_in_reasoning=None) == ("", "Okay, working it out.\n</think>\n\n391")
    # With an explicit opener the unknown case is split in both modes.
    explicit = "<think>x</think>y"
    assert split_reasoning(explicit, starts_in_reasoning=None) == ("x", "y")
    assert stream(explicit, [3, 9], starts_in_reasoning=None) == ("x", "y")


def test_unknown_template_leaves_a_plain_reply_alone():
    assert split_reasoning(ANSWER, starts_in_reasoning=None) == ("", ANSWER)


def test_random_cuts_over_random_text_match_the_whole_reply():
    rng = random.Random(7)
    alphabet = ["<think>", "</think>", "<", ">", "/", "think", "a", " ", "\n", "b c", "</thi", "<thin"]
    for _ in range(400):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 14)))
        for opened in (False, True):
            want = split_reasoning(text, starts_in_reasoning=opened)
            cuts = sorted(rng.sample(range(len(text) + 1), k=min(len(text) + 1, rng.randint(0, 5))))
            assert stream(text, cuts, starts_in_reasoning=opened) == want, (text, cuts, opened)


# ---- untrusted input ---------------------------------------------------------


def test_nothing_raises_on_odd_input():
    splitter = ReasoningSplitter()
    for value in (None, 5, b"x", [], {}, object(), ""):
        assert splitter.feed(value) == []  # type: ignore[arg-type]
    assert split_reasoning(None) == ("", "")  # type: ignore[arg-type]
    assert split_reasoning(5) == ("", "")  # type: ignore[arg-type]
    assert splitter.finish() == []
    assert splitter.feed("late") == [(CONTENT, "late")]  # usable after finish, as plain text


def _held_size(splitter: ReasoningSplitter) -> int:
    return len(splitter._held)  # noqa: SLF001


def test_held_back_text_is_bounded_whatever_is_sent():
    for prefix, opened in (("", False), ("", True), ("<think>", False)):
        splitter = ReasoningSplitter(starts_in_reasoning=opened)
        splitter.feed(prefix)
        for _ in range(200):
            splitter.feed(" " * 1000)
            assert _held_size(splitter) <= MAX_HELD + len(CLOSE)
    splitter = ReasoningSplitter()
    for _ in range(300):
        splitter.feed("<think>"[: 6])  # repeated partial openers
        assert _held_size(splitter) <= MAX_HELD + len(OPEN)


def test_a_megabyte_of_hostile_text_is_split_in_linear_time_without_error():
    hostile = ("</thi" + "<think>" * 3 + " \n") * 60_000
    for opened in (False, True, None):
        reasoning, content = split_reasoning(hostile, starts_in_reasoning=opened)
        assert isinstance(reasoning, str) and isinstance(content, str)
    splitter = ReasoningSplitter(starts_in_reasoning=True)
    total = 0
    for i in range(0, len(hostile), 7):
        total += sum(len(t) for _, t in splitter.feed(hostile[i:i + 7]))
    total += sum(len(t) for _, t in splitter.finish())
    assert total <= len(hostile)


def test_one_character_feeds_stay_cheap():
    splitter = ReasoningSplitter()
    for ch in "<think>" + "x" * 50_000 + "</think>" + "y" * 50_000:
        splitter.feed(ch)
    assert splitter.finish() == []


# ---- a tool call ends a block that never closes -------------------------------------

from potluck_local.services.tool_calls import CallDetector  # noqa: E402

DETECT = CallDetector(["get_weather", "get_time"])
CALL = '<tool_call>\n{"name": "get_weather", "arguments": {"city": "Paris"}}\n</tool_call>'
GOOD = '<tool_call>{"name": "get_time", "arguments": {}}</tool_call>'

# (name, text, starts_in_reasoning, reasoning, content)  -- all with the detector on
CALL_SHAPES = [
    ("opened_and_straight_to_the_call", "<think>\n\n" + CALL, False, "", CALL),
    ("opened_without_a_blank_line", "<think>" + CALL, False, "", CALL),
    ("reasoning_then_the_call_without_a_close", "<think>\nThe user wants weather.\n" + CALL, False,
     "The user wants weather.", CALL),
    ("template_opened_block_then_the_call", "Let me check.\n" + CALL, True, "Let me check.", CALL),
    ("template_opened_block_straight_to_the_call", CALL, True, "", CALL),
    ("two_calls", "<think>\n\n" + CALL + "\n" + GOOD, False, "", CALL + "\n" + GOOD),
    ("mistral_marker", '<think>\n[TOOL_CALLS] [{"name": "get_time", "arguments": {}}]', False, "",
     '[TOOL_CALLS] [{"name": "get_time", "arguments": {}}]'),
    ("the_block_closes_so_the_marker_was_reasoning",
     "<think>I will write " + GOOD + " now, then think more.</think>\n\nanswer", False,
     "I will write " + GOOD + " now, then think more.", "answer"),
    ("a_marker_in_prose_is_reasoning", "<think>use the <tool_call> tag", False, "use the <tool_call> tag", ""),
    ("a_marker_that_does_not_parse_leaves_all_reasoning", '<think>try <tool_call>{"name": "get_weather", "arguments": {"c',
     False, 'try <tool_call>{"name": "get_weather", "arguments": {"c', ""),
    ("a_tool_that_was_not_offered", '<think>\n\n<tool_call>{"name": "launch", "arguments": {}}</tool_call>', False,
     '<tool_call>{"name": "launch", "arguments": {}}</tool_call>', ""),
    ("junk_after_the_call_is_not_a_call_section", "<think>\n\n" + CALL + "\nDone.", False, CALL + "\nDone.", ""),
    ("first_marker_fails_the_second_is_the_call",
     '<think>said <tool_call>{"name": "get_time", "arguments": {}} oops\n' + GOOD, False,
     'said <tool_call>{"name": "get_time", "arguments": {}} oops', GOOD),
]


@pytest.mark.parametrize("name,text,opened,reasoning,content", CALL_SHAPES, ids=[s[0] for s in CALL_SHAPES])
def test_call_shapes_whole_reply(name, text, opened, reasoning, content):
    assert split_reasoning(text, starts_in_reasoning=opened, calls=DETECT) == (reasoning, content)


@pytest.mark.parametrize("name,text,opened,reasoning,content", CALL_SHAPES, ids=[s[0] for s in CALL_SHAPES])
def test_call_shapes_stream_cut_anywhere(name, text, opened, reasoning, content):
    def run(cuts):
        splitter = ReasoningSplitter(starts_in_reasoning=opened, calls=DETECT)
        pieces, last = [], 0
        for cut in [*cuts, len(text)]:
            pieces += splitter.feed(text[last:cut])
            last = cut
        pieces += splitter.finish()
        return ("".join(t for k, t in pieces if k == REASONING), "".join(t for k, t in pieces if k == CONTENT))

    for i in range(len(text) + 1):
        assert run([i]) == (reasoning, content), f"one cut at {i}"
    for i, j in itertools.combinations(range(len(text) + 1), 2):
        assert run([i, j]) == (reasoning, content), f"cuts at {i}, {j}"
    assert run(list(range(1, len(text)))) == (reasoning, content)


def test_without_tools_a_block_that_never_closes_is_all_reasoning_even_in_front_of_a_call():
    assert split_reasoning("<think>\n\n" + CALL) == (CALL, "")


def test_a_clean_stop_does_not_change_the_rule_for_a_block_that_never_closes():
    """<think>, a blank line, then the answer and a stop. The text is reasoning, in a stream and in a whole reply."""
    text = "<think>\n\n391"
    assert split_reasoning(text, calls=DETECT) == ("391", "")
    assert stream(text, [3, 9]) == ("391", "")


def test_a_possible_call_inside_the_block_is_held_back_until_the_block_closes_or_the_reply_ends():
    splitter = ReasoningSplitter(calls=DETECT)
    assert splitter.feed("<think>\nThinking about it. ") == [(REASONING, "Thinking about it.")]
    assert splitter.feed('Maybe <tool_call>{"name": "get_t') == [(REASONING, " Maybe")]  # held from the marker on
    assert splitter.feed('ime", "arguments": {}}</tool_call> still thinking') == []
    assert splitter.feed("</think>done") == [
        (REASONING, ' <tool_call>{"name": "get_time", "arguments": {}}</tool_call> still thinking'), (CONTENT, "done")]


def test_a_marker_that_is_clearly_prose_is_not_held_back():
    splitter = ReasoningSplitter(calls=DETECT)
    pieces = splitter.feed("<think>\nuse the <tool_call> tag for calls, ")
    assert pieces == [(REASONING, "use the <tool_call> tag for calls,")]  # only the trailing space waits


def test_a_partial_marker_at_the_end_of_a_piece_waits_for_the_next_one():
    splitter = ReasoningSplitter(calls=DETECT)
    first = splitter.feed("<think>\nHmm <tool_")
    assert "".join(t for k, t in first) == "Hmm" and splitter.feed("box> is not a tag") != []


def test_random_cuts_over_text_with_markers_match_the_whole_reply():
    rng = random.Random(21)
    alphabet = ["<think>", "</think>", "<tool_call>", "</tool_call>", '{"name": "get_time", "arguments": {}}', '{"x": 1}', "[TOOL_CALLS]",
                " ", "\n", "word", "<tool_", "</thi", "{", "}"]
    for _ in range(600):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 12)))
        for opened in (False, True):
            want = split_reasoning(text, starts_in_reasoning=opened, calls=DETECT)
            cuts = sorted(rng.sample(range(len(text) + 1), k=min(len(text) + 1, rng.randint(0, 5))))
            splitter = ReasoningSplitter(starts_in_reasoning=opened, calls=DETECT)
            pieces, last = [], 0
            for cut in [*cuts, len(text)]:
                pieces += splitter.feed(text[last:cut])
                last = cut
            pieces += splitter.finish()
            got = ("".join(t for k, t in pieces if k == REASONING), "".join(t for k, t in pieces if k == CONTENT))
            assert got == want, (text, cuts, opened)


def test_what_is_held_after_a_possible_call_is_bounded_and_then_reasoning():
    from potluck_local.services.reasoning_split import MAX_TAIL

    splitter = ReasoningSplitter(calls=DETECT)
    splitter.feed('<think><tool_call>{"name": "get_time", "arguments": {"a": "')
    out = []
    for _ in range(MAX_TAIL // 50_000 + 2):
        out += splitter.feed("y" * 50_000)
    out += splitter.finish()
    assert "".join(t for k, t in out if k == REASONING).startswith("<tool_call>")
    assert not [t for k, t in out if k == CONTENT]


def test_many_markers_in_one_unclosed_block_stay_cheap():
    import time

    text = "<think>" + '<tool_call>{"name": "get_time", "arguments": {' * 20_000
    started = time.monotonic()
    reasoning, content = split_reasoning(text, calls=DETECT)
    assert content == "" and reasoning.startswith("<tool_call>") and time.monotonic() - started < 10


def test_a_long_tail_in_one_character_pieces_is_not_recopied_for_every_piece():
    import time

    splitter = ReasoningSplitter(calls=DETECT)
    started = time.monotonic()
    splitter.feed('<think><tool_call>{"name": "get_time", "arguments": {"a": "')
    for ch in "y" * 200_000:
        splitter.feed(ch)
    splitter.finish()
    assert time.monotonic() - started < 10
