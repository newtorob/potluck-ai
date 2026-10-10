"""Answer checks: prove a loaded model gives right answers on this machine.

Why this exists
---------------
On a Windows machine with an old AMD driver the Vulkan backend generated at full
speed and returned wrong text (adjacent tokens repeated in every reply). The CPU
on the same machine was right, and nothing in the engine noticed: the model test
passed on any non-empty text and the machine went on serving other machines. A
configuration is trusted here only after checks that look at the answer.

What the checks are
-------------------
A few fixed prompts with known answers, run at temperature 0 through the loaded
model. They are easy on purpose: the point is to catch a broken computation, not
a weak model, so the smallest model in the catalog passes on a healthy machine.

  required     copy        "Reply with only this number: 58213" contains 58213
               arithmetic  2 + 2 contains a standalone 4
               repeats     a free two-sentence answer is not degenerate
  recorded     fact        the capital of France
               stops       an answer ended by itself (finish_reason "stop")
               long_prompt a code planted in a long prompt is recalled

Only the required checks decide the result. Changing any prompt or rule means
bumping SUITE_VERSION, which makes every stored proof stale.

The repeats rule
----------------
A broken kernel that doubles tokens ("A A mesh mesh network network") makes about
half the adjacent word pairs identical. Ordinary text has almost none: "the the"
once, "ha ha" in a list, "1, 1" (digits do not count). The rule fails a reply
with at least MIN_REPEATS adjacent identical words (or identical token pieces)
and at least REPEAT_RATIO of all adjacent pairs. Four pairs is more than natural
text produces in a 48 token answer, and 15% leaves room under the 50% a doubling
fault gives. See the calibration notes in the tests.

Proofs
------
A result is kept as a proof, tied to everything that could change the answer
(see build_key). A passing proof is reused so a load does not repeat the checks;
a proof that the GPU gave wrong answers is reused so a known-bad GPU is not
loaded again; a proof that the model itself failed is reused for
MODEL_FAILED_DAYS, so a model that fails does not load three times at every
start. The store is a small JSON file, replaced atomically.

Which loads are checked, and what lending does about the rest
-------------------------------------------------------------
The check covers one llama.cpp model loaded on this machine. Three kinds of load
are not covered: a managed split (the weights are on several machines), a
pipeline or whole-model (transformers) load, and a load with rpc_servers. For
those the status stays "not_run", answers_verified() is None (null in
/peer/capabilities), and other people's requests are served as they were before
the check existed. A load that is covered but has no passing check is not
lent: the owner turned the check off ("skipped"), it failed ("failed"), it is
still running, or a fault stopped it ("not_run"). lending_allowed() is the one
place that rule is written down.

Model output is untrusted data. It is judged here, kept as short excerpts in the
local proof for the owner's diagnostics, and never logged at info level or sent
to another machine.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from potluck_local.services import inference_errors

logger = logging.getLogger(__name__)

# Bump when a prompt, a judging rule or the set of required checks changes.
SUITE_VERSION = 1
SCHEMA_VERSION = 1
LEVELS = ("quick", "full")

PASSED, FAILED, RUNNING, NOT_RUN, SKIPPED = "passed", "failed", "running", "not_run", "skipped"
OK, GPU_WRONG_OUTPUT, MODEL_FAILED = "ok", "gpu_wrong_output", "model_failed"

# For this machine's own user. These sentences are part of the HTTP contract.
GPU_WRONG_TEXT = (
    "The GPU gave wrong answers for this model, so it is running on the CPU, which is slower. "
    "Updating the graphics driver usually fixes this."
)
MODEL_FAILED_TEXT = (
    "This model did not pass its answer check on this machine. You can still use it yourself. "
    "It is not offered to other people."
)

# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------

MIN_REPEATS = 4
REPEAT_RATIO = 0.15
# Two sentences are longer than this; fewer characters means the model did not answer.
MIN_CHARS = 30

# A reply that ends inside its reasoning block (a thinking model with a small
# budget) is asked again once with room to finish.
THINK_RETRY_TOKENS = 384

QUICK_PROMPT_TOKENS = 600
FULL_CONTEXT_FRACTION = 0.85
# "full" fills 85% of the loaded context, which is minutes of prompt evaluation
# at the 128k contexts the catalog allows. Past this size the check stops
# adding information about the device and only adds waiting.
FULL_PROMPT_CAP = 32768
_CODE = "58213"
_VAULT_CODE = "73941"

_FILLER = (
    "The harbor town keeps its ledgers in a stone hall beside the old clock tower.",
    "Every spring the fishing boats are painted again, usually in blue or white.",
    "A narrow road climbs from the market square to the lighthouse on the ridge.",
    "The baker on the corner opens before dawn and sells out of rye by nine.",
    "Visitors are asked to leave their bicycles by the fountain, not on the steps.",
    "Winter storms close the ferry for a few days, and the post comes late.",
    "The library lends umbrellas, and most of them come back within the week.",
    "Children learn to swim in the sheltered cove behind the western pier.",
    "The council meets on the first Tuesday and the minutes are pinned to the door.",
    "A row of pear trees along the canal blossoms a week before the rest of the town.",
    "The tide table is printed on the back of every ticket sold at the kiosk.",
    "Old nets are mended in the shed by the slipway, usually by the same three people.",
)

_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.S)
_THINK_TAGS = re.compile(r"</?think>")
_EDGE_PUNCTUATION = re.compile(r"^\W+|\W+$")


def visible_text(raw: str) -> tuple[str, bool]:
    """(the answer without its reasoning, whether the reply ended inside the reasoning)."""
    text = _THINK_BLOCK.sub("", raw)
    if "</think>" in text:  # the chat template opened the block and the model closed it
        text = text.rsplit("</think>", 1)[1]
    cut = "<think>" in text
    if cut:
        text = text.split("<think>", 1)[0]
    text = text.strip()
    return text, cut and not text


def without_think_tags(raw: str) -> str:
    """The reply with the <think> and </think> markers removed and the reasoning
    inside kept. An empty block ("<think>\n\n</think>", which Qwen3 sends with
    /no_think) leaves only whitespace, so it adds neither words nor length."""
    return _THINK_TAGS.sub(" ", raw)


def adjacent_repeats(items: list[str]) -> int:
    """Adjacent equal items. An empty item is a break in the text and never repeats."""
    return sum(1 for a, b in zip(items, items[1:]) if a and a == b)


def _words(text: str) -> list[str]:
    """Space-separated words, lowercased, punctuation trimmed. A word with no
    letter (a number, a dash) becomes "" so it breaks a run and never counts:
    "1, 1" is not a repeat. Text without spaces (Chinese, say) is one word per
    clause, and a phrase repeated between commas is a weak model looping, not a
    broken device."""
    trimmed = (_EDGE_PUNCTUATION.sub("", w).lower() for w in text.split())
    return [w if any(c.isalpha() for c in w) else "" for w in trimmed]


def is_degenerate(text: str, pieces: list[str] | None = None) -> bool:
    """Whether adjacent identical words (or token pieces) pass the threshold.

    Pieces are the model's own token strings for the same text; they catch a
    doubling that left no space between the copies ("TheThe sky sky").
    """
    sequences = [_words(text)]
    if pieces:
        sequences.append([p.strip().lower() if any(c.isalpha() for c in p) else "" for p in pieces])
    for sequence in sequences:
        repeats = adjacent_repeats(sequence)
        if repeats >= MIN_REPEATS and repeats >= REPEAT_RATIO * (len(sequence) - 1):
            return True
    return False


def _standalone_four(text: str) -> bool:
    return re.search(r"(?<![\d.,])4(?!\d|[.,]\d)", text) is not None


@dataclass(frozen=True)
class Reply:
    raw: str
    visible: str
    finish_reason: str | None
    tokens: int


@dataclass
class Outcome:
    id: str
    required: bool
    passed: bool
    finish_reason: str | None = None
    tokens: int = 0
    excerpt: str = ""
    ms: int = 0
    error: str | None = None

    def api(self) -> dict:
        return {"id": self.id, "passed": self.passed, "required": self.required}

    def record(self) -> dict:
        out = {"id": self.id, "passed": self.passed, "required": self.required, "finish_reason": self.finish_reason,
               "tokens": self.tokens, "excerpt": self.excerpt, "ms": self.ms}
        if self.error:
            out["error"] = self.error
        return out


@dataclass
class CheckRun:
    level: str
    outcomes: list[Outcome] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    duration_ms: int = 0

    @property
    def passed(self) -> bool:
        required = [o for o in self.outcomes if o.required]
        return bool(required) and all(o.passed for o in required)

    @property
    def output_tokens(self) -> int:
        return sum(o.tokens for o in self.outcomes)

    def api(self) -> list[dict]:
        return [o.api() for o in self.outcomes]

    def records(self) -> list[dict]:
        return [o.record() for o in self.outcomes]


def _excerpt(text: str) -> str:
    return " ".join(text.split())[:80]


def _ask(model: Any, prompt: str, max_tokens: int, think_off: bool) -> tuple[Reply, bool]:
    content = f"{prompt} /no_think" if think_off else prompt
    result = model.create_chat_completion(
        messages=[{"role": "user", "content": content}],
        temperature=0.0,
        max_tokens=max_tokens,
        stream=False,
    )
    choice = (result.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    raw = message.get("content")
    raw = raw if isinstance(raw, str) else ""
    usage = result.get("usage") or {}
    tokens = usage.get("completion_tokens")
    text, cut = visible_text(raw)
    return Reply(raw, text, choice.get("finish_reason"), tokens if isinstance(tokens, int) else 0), cut


def _ask_checked(model: Any, prompt: str, max_tokens: int, think_off: bool) -> Reply:
    reply, cut = _ask(model, prompt, max_tokens, think_off)
    if cut and reply.finish_reason == "length":
        reply, _ = _ask(model, prompt, max(max_tokens, THINK_RETRY_TOKENS), think_off)
    return reply


def _pieces(model: Any, text: str) -> list[str] | None:
    try:
        ids = model.tokenize(text.encode("utf-8"), add_bos=False, special=False)
        return [bytes(model.detokenize([i])).decode("utf-8", "replace") for i in ids]
    except Exception:  # noqa: BLE001 - the word rule still applies
        return None


def has_no_think_switch(model: Any) -> bool:
    """Whether the chat template has the Qwen3 switch that turns reasoning off
    ("/no_think" at the end of a message). Other models would read it as text."""
    try:
        template = (getattr(model, "metadata", None) or {}).get("tokenizer.chat_template") or ""
    except Exception:  # noqa: BLE001
        return False
    return "enable_thinking" in template


def long_prompt_text(count_tokens: Callable[[str], int], target_tokens: int) -> str:
    """Numbered filler sentences with a code planted at 40%, ending in the question."""

    def entry(i: int) -> str:
        return f"Note {i}: {_FILLER[i % len(_FILLER)]}"

    per_entry = max(1, count_tokens(entry(10)))
    count = max(4, target_tokens // per_entry)
    lines = [entry(i) for i in range(count)]
    lines.insert(int(count * 0.4), f"The vault code is {_VAULT_CODE}.")
    return "\n".join(lines) + "\n\nWhat is the vault code? Reply with only the number."


def run_checks(model: Any, level: str, *, n_ctx: int | None, on_cpu: bool) -> CheckRun:
    """Run the suite against a loaded llama.cpp model. A model that misbehaves
    does not raise: a check that throws has failed. The one exception is a GPU
    that ran out of memory, which raises InferenceFailure because it says
    nothing about the answers. The caller holds exclusive access to the model."""
    run = CheckRun(level=level)
    started = time.monotonic()
    think_off = has_no_think_switch(model)
    replies: dict[str, Reply] = {}

    def check(check_id: str, required: bool, prompt: str, max_tokens: int, judge: Callable[[Reply], bool]) -> None:
        t0 = time.monotonic()
        outcome = Outcome(check_id, required, False)
        try:
            reply = _ask_checked(model, prompt, max_tokens, think_off)
            replies[check_id] = reply
            outcome.finish_reason, outcome.tokens = reply.finish_reason, reply.tokens
            outcome.excerpt = _excerpt(reply.visible or reply.raw)
            outcome.passed = bool(judge(reply))
        except Exception as exc:  # noqa: BLE001 - a throwing model has failed the check
            failure = inference_errors.describe_failure(exc, gpu_offload=not on_cpu)
            if failure.category == inference_errors.GPU_OUT_OF_MEMORY:
                # Not an answer at all: the card ran out of room. Say that, not "wrong".
                raise inference_errors.InferenceFailure(failure.category, failure.message) from exc
            outcome.error = type(exc).__name__
        outcome.ms = round((time.monotonic() - t0) * 1000)
        run.outcomes.append(outcome)

    def repeats(reply: Reply) -> bool:
        # The reasoning counts too (a broken device repeats there as well), the markers do not.
        text = without_think_tags(reply.raw)
        return len(text.strip()) >= MIN_CHARS and not is_degenerate(text, _pieces(model, text))

    check("copy", True, f"Reply with only this number: {_CODE}", 24, lambda r: _CODE in r.visible)
    check("arithmetic", True, "What is 2 + 2? Reply with only the number.", 24, lambda r: _standalone_four(r.visible))
    check("repeats", True, "Write two short sentences about the ocean.", 48, repeats)
    check("fact", False, "What is the capital of France? Reply with one word.", 24, lambda r: "paris" in r.visible.lower())
    stop = replies.get("arithmetic")
    run.outcomes.append(Outcome("stops", False, bool(stop and stop.visible and stop.finish_reason == "stop"),
                                finish_reason=stop.finish_reason if stop else None))

    if level == "full" or not on_cpu:
        target = QUICK_PROMPT_TOKENS
        if level == "full" and n_ctx:
            target = min(int(n_ctx * FULL_CONTEXT_FRACTION) - 64, FULL_PROMPT_CAP)
        if n_ctx is None or target + 64 <= n_ctx:
            try:
                prompt = long_prompt_text(lambda s: len(model.tokenize(s.encode("utf-8"), add_bos=False)), max(target, 200))
            except Exception:  # noqa: BLE001
                prompt = None
            if prompt is not None:
                check("long_prompt", False, prompt, 24, lambda r: _VAULT_CODE in r.visible)

    try:
        model.reset()  # leave no checked prompt in the cache for the next request
    except Exception:  # noqa: BLE001
        pass
    run.duration_ms = round((time.monotonic() - started) * 1000)
    return run


# ---------------------------------------------------------------------------
# The key a proof is tied to
# ---------------------------------------------------------------------------


def wrapper_version() -> str:
    """The llama-cpp-python version, read without loading its native library."""
    try:
        from importlib.metadata import version

        return version("llama-cpp-python")
    except Exception:  # noqa: BLE001
        import sys

        module = sys.modules.get("llama_cpp")
        return str(getattr(module, "__version__", "unknown"))


def build_key(
    *,
    sha256: str,
    backend: str,
    device: str,
    device_kind: str,
    n_gpu_layers: int,
    total_layers: int | None,
    cpu_only: bool,
    driver: str | None,
    library: str,
    level: str,
) -> dict:
    """Everything that could change the answer. A proof applies to exactly this."""
    return {
        "model_sha256": sha256,
        "backend": backend,
        "device": device,
        "device_kind": device_kind,
        "n_gpu_layers": n_gpu_layers,
        "total_layers": total_layers,
        "cpu_only": cpu_only,
        "driver": driver,
        "llama_cpp": wrapper_version(),
        "library": library,
        "suite": SUITE_VERSION,
        "level": level,
    }


def key_id(key: dict) -> str:
    return hashlib.sha256(json.dumps(key, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:32]


# ---------------------------------------------------------------------------
# Proofs
# ---------------------------------------------------------------------------

MAX_PROOFS = 64
_MAX_FILE_BYTES = 1024 * 1024
MAX_AGE_DAYS = 90
MAX_AGE_DAYS_UNKNOWN_DRIVER = 30
# Long enough that a model that fails does not load three times at every start,
# short enough that the owner is not left with an old verdict for months.
MODEL_FAILED_DAYS = 7


def _max_age_seconds(key: dict, verdict: str | None = None) -> float:
    days = MAX_AGE_DAYS if key.get("driver") else MAX_AGE_DAYS_UNKNOWN_DRIVER
    if verdict == MODEL_FAILED:
        days = min(days, MODEL_FAILED_DAYS)
    return days * 86400.0


def _proofs_path() -> Path:
    from potluck_local.utils.paths import data_dir

    return data_dir() / "data" / "acceptance-proofs.json"


class ProofStore:
    """Results of the answer check, by key, in one small JSON file.

    Bounded to MAX_PROOFS (oldest dropped first), written atomically, and
    tolerant of a missing, damaged or oversized file, which reads as empty.
    A failure to write is logged and ignored: the check result is still used
    for this load.
    """

    def __init__(self, path: Path | Callable[[], Path] | None = None) -> None:
        self._path = path
        self._lock = threading.Lock()

    def path(self) -> Path:
        if self._path is None:
            return _proofs_path()
        return self._path() if callable(self._path) else self._path

    def _read(self) -> dict[str, dict]:
        try:
            path = self.path()
            if path.stat().st_size > _MAX_FILE_BYTES:
                return {}
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        proofs = payload.get("proofs") if isinstance(payload, dict) else None
        if not isinstance(proofs, dict):
            return {}
        return {k: v for k, v in proofs.items() if isinstance(v, dict) and isinstance(v.get("key"), dict)}

    def _write(self, proofs: dict[str, dict]) -> None:
        path = self.path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".acceptance-", dir=path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump({"schema_version": SCHEMA_VERSION, "proofs": proofs}, stream, separators=(",", ":"))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
            finally:
                Path(temporary).unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("acceptance_proofs_not_saved error=%s", type(exc).__name__)

    def get(self, key: dict, *, now: float | None = None) -> dict | None:
        """The proof for exactly this key, unless it is too old."""
        with self._lock:
            proof = self._read().get(key_id(key))
        if proof is None or proof.get("key") != key:
            return None
        checked = proof.get("checked_at_epoch")
        if not isinstance(checked, (int, float)):
            return None
        if (time.time() if now is None else now) - checked > _max_age_seconds(key, proof.get("verdict")):
            return None
        return proof

    def put(self, key: dict, record: dict) -> None:
        stamp = time.time()
        entry = {**record, "key": key, "checked_at_epoch": stamp, "checked_at": iso(stamp)}
        with self._lock:
            proofs = self._read()
            proofs[key_id(key)] = entry
            proofs = {k: v for k, v in proofs.items()
                      if stamp - float(v.get("checked_at_epoch") or 0) <= _max_age_seconds(v["key"], v.get("verdict"))}
            if len(proofs) > MAX_PROOFS:
                newest = sorted(proofs.items(), key=lambda kv: float(kv[1].get("checked_at_epoch") or 0), reverse=True)
                proofs = dict(newest[:MAX_PROOFS])
            self._write(proofs)

    def drop(self, key: dict) -> None:
        with self._lock:
            proofs = self._read()
            if proofs.pop(key_id(key), None) is not None:
                self._write(proofs)

    def holds_gpu_wrong_output(self) -> bool:
        """Whether any proof says a GPU was wrong. A load asks this first, so
        the usual machine pays nothing for the lookup."""
        with self._lock:
            return any(p.get("verdict") == GPU_WRONG_OUTPUT for p in self._read().values())


_store = ProofStore()


def get_store() -> ProofStore:
    return _store


def iso(epoch: float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if epoch is None else epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# What /health/inference reports (the HTTP contract)
# ---------------------------------------------------------------------------


def summary(
    *,
    status: str = NOT_RUN,
    model_id: str | None = None,
    level: str | None = None,
    checked_at: str | None = None,
    from_proof: bool = False,
    ran_on: dict | None = None,
    checks: list[dict] | None = None,
    verdict: str | None = None,
    fallback: dict | None = None,
    message: str | None = None,
) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "model_id": model_id,
        "level": level,
        "checked_at": checked_at,
        "from_proof": from_proof,
        "ran_on": ran_on,
        "checks": list(checks or []),
        "verdict": verdict,
        "fallback": fallback,
        "message": message,
    }


FALLBACK_TO_CPU = {"from": "gpu", "to": "cpu", "reason": GPU_WRONG_OUTPUT}


def lending_allowed(runtime: Any) -> bool:
    """Whether to serve another person's request with the loaded model.

    True when its answers are verified, and also when the check does not cover
    this kind of load (answers_verified() is None; see the module docstring).
    False for a covered load that is unverified, and for a runtime that cannot
    say. The callers have already turned away "nothing is loaded".
    """
    try:
        verified = runtime.answers_verified()
    except Exception:  # noqa: BLE001
        return False
    return verified is True or verified is None
