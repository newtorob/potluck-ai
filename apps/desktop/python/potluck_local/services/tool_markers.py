"""Learn which tool-call markers in a reply were sampled as the marker token.

tool_calls.py reads calls out of decoded text, and decoded text cannot tell the
marker token from the same characters written with ordinary tokens (a model that
quotes a file holding ``<tool_call>{...}</tool_call>``). This module is the
engine-side half of the fix. For each marker the loaded vocab holds as a single
token (Qwen ``<tool_call>``, Llama ``<|python_tag|>``, Mistral ``[TOOL_CALLS]``,
Phi-4 ``<|tool_call|>``) it records which sampled tokens were that token, and puts
``tool_calls.AUTH`` in the reply text in front of each one. A marker whose vocab
has no token for it is not proven and is read from text, as before.

llama-cpp-python: ``Llama.generate`` yields the sampled token ids to
``_create_completion``, which turns them into text. ``install`` wraps that one
instance method (the way prompt_guard wraps ``tokenize``). While a ``Reader`` is
active on the calling thread the wrapper notes each token that is a marker and
where its text starts; with none active it hands back the original generator, so
nothing else on the model changes. Cost is one set lookup and one list append per
token. A marker costs one detokenize of the reply so far, to find its offset.
The offset places ``AUTH`` in the stream however the engine cut the text into
chunks (a multibyte hold, a stop-sequence hold), and a control token, which
llama-cpp-python decodes to nothing, gets its text written out behind ``AUTH``.

``AUTH`` in the model's own text is escaped to U+FFFD, so only this module makes it.

llama-server (the native split path): a /v1/completions request with ``logprobs: 1``
returns the sampled token's id in every streamed chunk (one token per chunk) and in
``choices[0].logprobs.content`` of a whole reply, so ``tag_chunk`` and ``tag_whole``
read the ids directly. Only the guarded path (split_native.py ``_prepare``) is
served this way; the server's own /v1/chat/completions parser reads text.

Importable without torch or llama_cpp; the model is only touched through the
object passed in.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Any

from potluck_local.services.tool_calls import AUTH, MARKER_TEXTS

_ESCAPED = "�"


def marker_tokens(model: Any) -> dict[int, str]:
    """``{token id: marker text}`` for the markers this vocab holds as one token.

    Empty for objects with no in-process vocab (the native split adapter, test
    fakes, Hugging Face engines). Cached on the model."""
    cached = getattr(model, "_potluck_tool_markers", None)
    if isinstance(cached, dict):
        return cached
    inner = getattr(model, "_model", None)
    found: dict[int, str] = {}
    if inner is not None and hasattr(inner, "token_get_text") and hasattr(model, "tokenize"):
        for text in MARKER_TEXTS:
            try:
                ids = model.tokenize(text.encode("utf-8"), add_bos=False, special=True)
                if len(ids) != 1:
                    continue
                raw = inner.token_get_text(ids[0])
                if (raw.decode("utf-8", "ignore") if isinstance(raw, bytes) else raw) == text:
                    found[ids[0]] = text
            except Exception:  # noqa: BLE001 - a vocab that cannot say proves nothing
                continue
    try:
        model._potluck_tool_markers = found
    except AttributeError:
        pass
    return found


def proven(model: Any) -> frozenset[str]:
    """The marker texts ``watch`` (or, for a split model, its guarded path) can prove on ``model``."""
    if getattr(model, "managed_split", False):
        read = getattr(model, "tool_markers", None)
        return frozenset(read().values()) if callable(read) else frozenset()
    if not callable(getattr(model, "generate", None)):
        return frozenset()
    return frozenset(marker_tokens(model).values())


def escape(text: str) -> str:
    """``text`` with any ``AUTH`` the model wrote made harmless. For a reply that no Reader or
    tag function has marked: its text must not be able to carry proof."""
    return text.replace(AUTH, _ESCAPED)


def tag_chunk(markers: dict[int, str], choice: dict) -> str:
    """The text of one streamed /v1/completions choice, with ``AUTH`` in front of the
    marker when the server says the chunk's token is one. A token the server prints is
    already in the text; one it hides gets its text written out."""
    text = choice.get("text")
    text = text.replace(AUTH, _ESCAPED) if isinstance(text, str) else ""
    try:
        for entry in (choice.get("logprobs") or {}).get("content") or ():
            marker = markers.get(entry.get("id")) if isinstance(entry, dict) else None
            if marker is None:
                continue
            if text == marker:
                return AUTH + text
            if text == "":
                return AUTH + marker
    except (AttributeError, TypeError):
        pass
    return text


def tag_whole(markers: dict[int, str], choice: dict) -> str:
    """The text of a whole /v1/completions choice, tagged from the token ids and bytes in its
    ``logprobs``. Left untagged (so no marker counts) when those bytes are not the text."""
    text = choice.get("text")
    text = text.replace(AUTH, _ESCAPED) if isinstance(text, str) else ""
    try:
        plain, tagged = [], []
        for entry in (choice.get("logprobs") or {}).get("content") or ():
            piece = bytes(entry["bytes"]).decode("utf-8", "ignore")
            marker = markers.get(entry.get("id"))
            if marker is not None:
                tagged.append(AUTH + ("" if piece == marker else marker))
            plain.append(piece)
            tagged.append(piece)
        return "".join(tagged) if "".join(plain) == text else text
    except (AttributeError, KeyError, TypeError, ValueError):
        return text


class Reader:
    """The markers sampled during one reply, and the tagging of its text."""

    def __init__(self, model: Any, markers: dict[int, str]) -> None:
        self._model = model
        self._markers = markers
        self._prompt: list[int] = []
        self._ids: list[int] = []
        self._hits: list[tuple[int, str]] = []  # (offset in the reply text, text to write there)
        self._next = 0  # the first hit not yet written into the stream
        self._sent = 0  # characters of reply text handed on so far

    # -- called by the generate wrapper, once per sampled token --------------------

    def wrap(self, gen, prompt):
        self._prompt = prompt
        ids, markers = self._ids, self._markers
        try:
            for token in gen:
                if token in markers:
                    self._hit(token, markers[token])
                ids.append(token)
                yield token
        finally:
            close = getattr(gen, "close", None)
            if close is not None:
                close()

    def _hit(self, token: int, text: str) -> None:
        try:
            before = bytes(self._model.detokenize(self._ids, prev_tokens=self._prompt)).decode("utf-8", "ignore")
            shown = bytes(self._model.detokenize([token], prev_tokens=self._prompt + self._ids)).decode("utf-8", "ignore")
        except Exception:  # noqa: BLE001 - an unplaceable marker is simply not proven
            return
        # A token the engine prints (Qwen's) is already in the text; one it hides is not.
        self._hits.append((len(before), AUTH if shown == text else AUTH + text))

    # -- called by the caller as the reply text goes out -------------------------------

    def text(self, piece: str) -> str:
        """``piece``, the next stretch of reply text, with ``AUTH`` before any marker that starts in it."""
        piece = piece.replace(AUTH, _ESCAPED)
        start, end = self._sent, self._sent + len(piece)
        self._sent = end
        out, last = [], 0
        while self._next < len(self._hits) and self._hits[self._next][0] < end:
            offset, tag = self._hits[self._next]
            self._next += 1
            at = min(max(offset - start, 0), len(piece))
            out.append(piece[last:at])
            out.append(tag)
            last = at
        if not out:
            return piece
        out.append(piece[last:])
        return "".join(out)

    def flush(self) -> str:
        """Markers at the very end of the reply, after the last text. A hidden one (a
        control token) is written out; a printed one whose text never came is dropped."""
        tail = []
        while self._next < len(self._hits):
            offset, tag = self._hits[self._next]
            self._next += 1
            if offset <= self._sent and len(tag) > 1:
                tail.append(tag)
        return "".join(tail)

    def whole(self, text: str) -> str:
        """The same for a reply that arrives in one piece."""
        return self.text(text) + self.flush()

    def chunk(self, chunk: dict) -> dict:
        """An OpenAI streaming chunk with its delta content tagged, in place.

        The finish chunk also takes the markers left at the end. Never raises."""
        try:
            choice = chunk["choices"][0]
            delta = choice.get("delta") or {}
            content = delta.get("content")
            text = content if isinstance(content, str) else ""
            new = self.text(text) if text else ""
            if choice.get("finish_reason") is not None:
                new += self.flush()
            if new != text:
                choice["delta"] = {**delta, "content": new}
        except Exception:  # noqa: BLE001 - never turn a reply into an error
            pass
        return chunk


class _Tap(threading.local):
    reader: Reader | None = None


def _install(model: Any) -> _Tap:
    tap = getattr(model, "_potluck_marker_tap", None)
    if isinstance(tap, _Tap):
        return tap
    tap = _Tap()
    plain = model.generate

    def generate(tokens, *args, **kwargs):
        gen = plain(tokens, *args, **kwargs)
        reader = tap.reader
        return gen if reader is None else reader.wrap(gen, list(tokens))

    model.generate = generate
    model._potluck_marker_tap = tap
    return tap


@contextmanager
def watch(model: Any, active: bool):
    """Yield a Reader for the reply ``model`` is about to generate on this thread, or None
    when ``active`` is false or the model has no marker this module can prove."""
    reader, tap = None, None
    if active:
        markers = marker_tokens(model) if proven(model) else {}
        if markers:
            tap = _install(model)
            reader = Reader(model, markers)
            tap.reader = reader
    try:
        yield reader
    finally:
        if tap is not None:
            tap.reader = None
