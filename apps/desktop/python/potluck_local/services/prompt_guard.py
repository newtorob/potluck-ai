"""Keep control-token text in request content from becoming control tokens.

llama-cpp-python renders the chat template to one string and tokenizes the whole
string with ``special=True``; Hugging Face tokenizers likewise match special
tokens inside any text they are given. So a tool result, file, web page or user
message containing ``<|im_end|>\\n<|im_start|>system`` opens a real system turn.

The fix is in three steps. Before the template runs, every occurrence of a
control token's text inside request data is swapped for a placeholder that
carries a random per-request nonce. The template then renders exactly as it did
before. When the rendered string is tokenized, text between placeholders is
tokenized as it always was (special tokens parsed, so the template's own turn
markers still work) and each placeholder is tokenized as the original text with
special-token parsing off, so it becomes ordinary text tokens.

A request with no control-token text in it takes the old path unchanged: the
messages object is returned as is, the tokenizer wrapper hands the call straight
through, and the token ids are the same ones the old path produced.

Placeholders are plain ASCII (letters and digits) on purpose. Chat templates
render tool definitions and call arguments with ``tojson``, which escapes
non-ASCII and HTML-sensitive characters and would mangle a private-use or
angle-bracket marker. The 128-bit nonce is unknown to whoever wrote the content,
so request text cannot forge a placeholder, and a placeholder with an unknown
nonce is left as the plain text it is.

CONTROL tokens (and end-of-generation tokens) are protected, and so are the
tool-call markers (tool_calls.MARKER_TEXTS) wherever the vocab has them as a token.
``</think>`` is a user-defined token in the Qwen vocabularies, not a control token,
and templates (Qwen3 splits assistant content on ``</think>``) rely on it, so it is
left alone. ``<tool_call>`` is user-defined too, and no flag stops a tokenizer
from matching it, so a marker is protected by cutting its text in two and
tokenizing the halves apart. Without that, a file the model is asked to repeat
would put the real marker token in the prompt, and the model would sample the real
token back: the engine's check that a call marker was sampled as the marker token
(tool_markers.py) would then pass for text the user never meant as a call.

Importable without torch or llama_cpp; both are only touched through the
objects passed in.
"""
from __future__ import annotations

import os
import re
import secrets
import struct
import threading
from collections import OrderedDict
from typing import Any, Iterable

from potluck_local.services.tool_calls import MARKER_TEXTS

MARK = "PLKGRD"
_PLACEHOLDER = re.compile(MARK + r"([0-9a-f]{32})T([0-9]+)E")
_MAX_OPEN_TICKETS = 256


class Ticket:
    """The placeholders issued for one request. Close it when the request ends."""

    def __init__(self, guard: "PromptGuard | None" = None):
        self._guard = guard
        self.nonce = secrets.token_hex(16) if guard is not None else ""
        self.texts: list[str] = []

    def placeholder(self, text: str) -> str:
        try:
            index = self.texts.index(text)
        except ValueError:
            index = len(self.texts)
            self.texts.append(text)
        return f"{MARK}{self.nonce}T{index}E"

    @property
    def active(self) -> bool:
        return bool(self.texts)

    def close(self) -> None:
        if self._guard is not None:
            self._guard._forget(self.nonce)

    def __enter__(self) -> "Ticket":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


NO_TICKET = Ticket()


class PromptGuard:
    def __init__(self, protected: Iterable[str]):
        # Longest first so a token that contains another is matched whole.
        texts = sorted({t for t in protected if t}, key=len, reverse=True)
        self.protected = tuple(texts)
        # Tokenized in two halves, never as one piece (a marker that is a user-defined token).
        self.split = frozenset(t for t in texts if t in MARKER_TEXTS)
        self._pattern = re.compile("|".join(re.escape(t) for t in texts)) if texts else None
        self._open: "OrderedDict[str, Ticket]" = OrderedDict()
        self._lock = threading.Lock()

    def _register(self, ticket: Ticket) -> None:
        with self._lock:
            self._open[ticket.nonce] = ticket
            # Backstop for a caller that never closes; real requests close theirs.
            while len(self._open) > _MAX_OPEN_TICKETS:
                self._open.popitem(last=False)

    def _forget(self, nonce: str) -> None:
        with self._lock:
            self._open.pop(nonce, None)

    def sanitize(self, data: Any) -> tuple[Any, Ticket]:
        """``data`` with protected token text replaced, and the ticket for it.

        Walks strings in dicts and lists, keys included (MCP tool schemas put
        request-controlled text in property names). Returns ``data`` itself and
        an inactive ticket when nothing matched.
        """
        if self._pattern is None:
            return data, NO_TICKET
        ticket = Ticket(self)

        def swap(match: "re.Match[str]") -> str:
            return ticket.placeholder(match.group(0))

        def walk(value: Any) -> Any:
            if isinstance(value, str):
                return self._pattern.sub(swap, value) if self._pattern.search(value) else value
            if isinstance(value, dict):
                items = [(walk(k), walk(v)) for k, v in value.items()]
                if all(nk is k and nv is v for (nk, nv), (k, v) in zip(items, value.items())):
                    return value
                return dict(items)
            if isinstance(value, (list, tuple)):
                items = [walk(v) for v in value]
                if all(n is o for n, o in zip(items, value)):
                    return value
                return items if isinstance(value, list) else tuple(items)
            return value

        out = walk(data)
        if not ticket.active:
            return data, NO_TICKET
        self._register(ticket)
        return out, ticket

    def segments(self, text: str) -> list[tuple[str, bool]] | None:
        """``text`` as (piece, protected) pairs, or None when it holds no live placeholder.

        A protected piece is the original control-token text. A placeholder
        whose nonce is not open stays inside the surrounding plain piece.
        """
        if MARK not in text:
            return None
        with self._lock:
            live = dict(self._open)
        pieces: list[tuple[str, bool]] = []
        last = 0
        for match in _PLACEHOLDER.finditer(text):
            ticket = live.get(match.group(1))
            index = int(match.group(2))
            if ticket is None or index >= len(ticket.texts):
                continue
            if match.start() > last:
                pieces.append((text[last:match.start()], False))
            pieces.append((ticket.texts[index], True))
            last = match.end()
        if not pieces:
            return None
        if last < len(text):
            pieces.append((text[last:], False))
        return pieces


# ---- llama-cpp-python -------------------------------------------------------

def llama_protected_tokens(model: Any) -> list[str]:
    """Texts the loaded vocab would turn into a CONTROL or end-of-generation token.

    Each candidate is checked by tokenizing it with ``special=True``: only text
    that really becomes that one token is a risk, so only that is protected.
    """
    import llama_cpp

    from potluck_local.services import tool_markers

    inner = model._model
    control = llama_cpp.LLAMA_TOKEN_ATTR_CONTROL
    vocab = inner.vocab
    found: list[str] = []
    for token in range(model.n_vocab()):
        if not (inner.token_get_attr(token) & control or llama_cpp.llama_vocab_is_eog(vocab, token)):
            continue
        raw = inner.token_get_text(token)
        text = raw.decode("utf-8", "ignore") if isinstance(raw, bytes) else raw
        if not text.strip():
            continue
        if model.tokenize(text.encode("utf-8"), add_bos=False, special=True) == [token]:
            found.append(text)
    # A marker the tokenizer matches in any text (a user-defined token) still has to stay text.
    return found + [text for text in tool_markers.marker_tokens(model).values() if text not in found]


def llama_guard(model: Any) -> PromptGuard | None:
    """The guard installed on ``model``, installing it on first use.

    Wraps ``model.tokenize`` once. llama-cpp-python's chat handler and
    ``create_completion`` both reach the tokenizer through that instance
    attribute, so one wrapper covers chat, tools and raw completions. Returns
    None for objects with no in-process vocab: the native split adapter (a
    llama-server child; split_native.py reads the header with
    ``gguf_protected_tokens`` instead) and test fakes.
    """
    existing = getattr(model, "_potluck_prompt_guard", None)
    if isinstance(existing, PromptGuard):
        return existing
    inner_model = getattr(model, "_model", None)
    if inner_model is None or not hasattr(inner_model, "token_get_attr") or not hasattr(model, "tokenize"):
        return None
    return install_tokenize_guard(model, PromptGuard(llama_protected_tokens(model)))


def install_tokenize_guard(model: Any, guard: PromptGuard) -> PromptGuard:
    plain = model.tokenize

    def tokenize(text: bytes, add_bos: bool = True, special: bool = False) -> list[int]:
        if not special or MARK.encode() not in text:
            return plain(text, add_bos, special)
        pieces = guard.segments(text.decode("utf-8"))
        if pieces is None:
            return plain(text, add_bos, special)
        ids: list[int] = []
        first = True
        for piece, protected in pieces:
            if not piece:
                continue
            if protected and piece in guard.split:
                cut = len(piece) // 2
                ids += plain(piece[:cut].encode("utf-8"), add_bos=add_bos and first, special=False)
                ids += plain(piece[cut:].encode("utf-8"), add_bos=False, special=False)
            else:
                ids += plain(piece.encode("utf-8"), add_bos=add_bos and first, special=not protected)
            first = False
        return ids

    model.tokenize = tokenize
    model._potluck_prompt_guard = guard
    return guard


def protect_chat(model: Any, messages: Any, kwargs: dict | None = None) -> tuple[Any, dict, Ticket]:
    """Sanitized (messages, kwargs, ticket) for ``model.create_chat_completion``.

    ``kwargs["tools"]`` is sanitized too: tool descriptions come from MCP servers
    and are rendered into the prompt. Other keys (including a forced-call grammar
    built from the original tool schema) are untouched. The caller closes the
    ticket once the call, and for a stream the whole stream, is done.
    """
    extra = dict(kwargs or {})
    guard = llama_guard(model)
    if guard is None:
        return messages, extra, NO_TICKET
    request = {"messages": messages, "tools": extra.get("tools")}
    clean, ticket = guard.sanitize(request)
    if not ticket.active:
        return messages, extra, NO_TICKET
    if "tools" in extra:
        extra["tools"] = clean["tools"]
    return clean["messages"], extra, ticket


def protect_prompt(model: Any, prompt: str) -> tuple[str, Ticket]:
    """The same for a raw ``create_completion`` prompt string."""
    guard = llama_guard(model)
    if guard is None:
        return prompt, NO_TICKET
    clean, ticket = guard.sanitize(prompt)
    return (clean, ticket) if ticket.active else (prompt, NO_TICKET)


# ---- Native llama-server ------------------------------------------------------

_GGUF_FIXED = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1, 10: 8, 11: 8, 12: 8}
_GGUF_CONTROL = 3  # tokenizer.ggml.token_type value for a control token
_GGUF_USER_DEFINED = 4
_GGUF_END_KEYS = frozenset(f"tokenizer.ggml.{n}_token_id" for n in ("eos", "eot", "eom"))


def _gguf_vocab(path: Any) -> tuple[list[str], list[int], set[int]]:
    """``(token texts, token types, end-of-generation ids)`` from a GGUF header, or empty
    values when the file cannot be read as GGUF. Never raises."""
    texts: list[str] = []
    types: list[int] = []
    ends: set[int] = set()
    try:
        with open(path, "rb") as fh:
            if fh.read(4) != b"GGUF" or struct.unpack("<I", fh.read(4))[0] not in (2, 3):
                return [], [], set()
            fh.read(8)  # tensor_count
            (kv_count,) = struct.unpack("<Q", fh.read(8))
            if kv_count > 100_000:
                return [], [], set()

            def read_str() -> bytes:
                (n,) = struct.unpack("<Q", fh.read(8))
                if n > 1 << 20:
                    raise ValueError("implausible GGUF string length")
                return fh.read(n)

            for _ in range(kv_count):
                key = read_str().decode("utf-8", "replace")
                (vtype,) = struct.unpack("<I", fh.read(4))
                if vtype != 9:
                    if vtype == 8:
                        read_str()
                    elif vtype in _GGUF_FIXED:
                        raw = fh.read(_GGUF_FIXED[vtype])
                        if key in _GGUF_END_KEYS and vtype in (4, 5):
                            ends.add(struct.unpack("<I" if vtype == 4 else "<i", raw)[0])
                    else:
                        raise ValueError("unknown GGUF value type")
                    continue
                elem, count = struct.unpack("<IQ", fh.read(12))
                if key == "tokenizer.ggml.tokens" and elem == 8:
                    texts = [read_str().decode("utf-8", "ignore") for _ in range(count)]
                elif key == "tokenizer.ggml.token_type" and elem in (4, 5):
                    types = list(struct.unpack(f"<{count}{'I' if elem == 4 else 'i'}", fh.read(4 * count)))
                elif elem in _GGUF_FIXED:
                    fh.seek(_GGUF_FIXED[elem] * count, os.SEEK_CUR)
                elif elem == 8:
                    for _ in range(count):
                        read_str()
                else:
                    raise ValueError("unknown GGUF array element type")
    except (OSError, struct.error, ValueError):
        return [], [], set()
    return texts, types, ends


def gguf_protected_tokens(path: Any) -> list[str]:
    """The same texts ``llama_protected_tokens`` finds, read from the GGUF header.

    The native split engine (split_native.py) keeps its vocab in a llama-server
    child, so the control tokens (token type 3), the end-of-generation ids the
    header names (eos, eot, eom) and the tool-call markers that are user-defined
    tokens (type 4) are read from the file instead. Returns [] when the file
    cannot be read as GGUF; never raises.
    """
    texts, types, ends = _gguf_vocab(path)
    found = {i for i, t in enumerate(types[:len(texts)]) if t == _GGUF_CONTROL} | {i for i in ends if 0 <= i < len(texts)}
    # Tool-call markers the vocab holds as user-defined tokens (Qwen's <tool_call>).
    found |= {i for i, t in enumerate(types[:len(texts)]) if t == _GGUF_USER_DEFINED and texts[i] in MARKER_TEXTS}
    return sorted({texts[i] for i in found if texts[i].strip()})


def gguf_marker_tokens(path: Any) -> dict[int, str]:
    """``{token id: marker text}`` for the tool-call markers the GGUF vocab holds as one
    control or user-defined token (what ``tool_markers.marker_tokens`` finds in process)."""
    texts, types, _ = _gguf_vocab(path)
    return {i: texts[i] for i, t in enumerate(types[:len(texts)])
            if t in (_GGUF_CONTROL, _GGUF_USER_DEFINED) and texts[i] in MARKER_TEXTS}


# ---- Hugging Face tokenizers ------------------------------------------------

def hf_protected_tokens(tok: Any) -> list[str]:
    names = {t for t in getattr(tok, "all_special_tokens", None) or [] if isinstance(t, str)}
    for added in (getattr(tok, "added_tokens_decoder", None) or {}).values():
        if getattr(added, "special", False):
            names.add(str(added.content))
    return [n for n in names if n.strip()]


def hf_guard(tok: Any) -> PromptGuard:
    guard = getattr(tok, "_potluck_prompt_guard", None)
    if guard is None:
        guard = PromptGuard(hf_protected_tokens(tok))
        try:
            tok._potluck_prompt_guard = guard
        except AttributeError:
            pass
    return guard


def hf_chat_inputs(tok: Any, messages: Any) -> Any:
    """``tok.apply_chat_template(messages, add_generation_prompt=True, return_tensors="pt", return_dict=True)``.

    A request with no special-token text goes through that exact call. Otherwise
    the template is rendered to text from sanitized messages and tokenized in
    pieces, with ``split_special_tokens=True`` on the pieces that came from
    request content.
    """
    guard = hf_guard(tok)
    clean, ticket = guard.sanitize(messages)
    if not ticket.active:
        return tok.apply_chat_template(
            messages, add_generation_prompt=True, return_tensors="pt", return_dict=True)
    try:
        text = tok.apply_chat_template(clean, add_generation_prompt=True, tokenize=False)
        pieces = guard.segments(text)
        if pieces is None:  # the template dropped every placeholder (Qwen3 drops old reasoning)
            pieces = [(text, False)]
        ids: list[int] = []
        for piece, protected in pieces:
            if not piece:
                continue
            kwargs = {"add_special_tokens": False}
            if protected:
                kwargs["split_special_tokens"] = True
            ids += list(tok(piece, **kwargs)["input_ids"])
    finally:
        ticket.close()
    import torch

    tensor = torch.tensor([ids], dtype=torch.long)
    return {"input_ids": tensor, "attention_mask": torch.ones_like(tensor)}
