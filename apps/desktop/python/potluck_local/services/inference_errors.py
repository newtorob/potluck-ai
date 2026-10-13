"""Stable categories for inference failures, and the words that go with them.

Two audiences read an inference error and they get different text:

* The machine's own user sees a sentence they can act on. For most failures
  that is still the engine's own message. For a GPU that ran out of memory it
  is a fixed sentence, because the engine's message on Windows is
  ``[WinError -529697949] Windows Error 0xe06d7363``, which says nothing.
* Another machine (a household peer, a circle member, the coordinator) sees
  only fixed text chosen by category. An exception string can hold paths, model
  output or driver details, and it comes from this machine to someone else's.
  Nothing in this module returns an exception's text for that audience.

How a GPU out-of-memory failure is recognised, and what is deliberately left out
-------------------------------------------------------------------------------
On Windows the Vulkan backend reports a failed device allocation by throwing a
C++ exception (``vk::OutOfDeviceMemoryError``) that nothing in llama.cpp
catches. ctypes turns the structured exception into
``OSError(winerror=-529697949)``; 0xE06D7363 is the Microsoft C++ exception
code. The code alone does not prove the cause (any uncaught C++ exception
carries it), so it counts as out-of-memory only when the model is actually
offloaded to a GPU, since a CPU-only load cannot throw a device allocation
error. Plain ``OSError`` for anything else (a closed file, a failed socket) is
never labelled out-of-memory, and neither is Python's own ``MemoryError``, which
is host RAM.

The other signals are phrases the GPU runtimes themselves emit (Vulkan's
``ErrorOutOfDeviceMemory``, CUDA's ``out of memory``, PyTorch's
``OutOfMemoryError``). They are matched as whole phrases, never on the bare
word "memory".
"""

from __future__ import annotations

import re
from dataclasses import dataclass

GPU_OUT_OF_MEMORY = "gpu_out_of_memory"
INFERENCE_ERROR = "inference_error"
MODEL_NOT_VERIFIED = "model_not_verified"

# Microsoft C++ exception code, as ctypes reports it for an uncaught C++ throw.
MSVC_CPP_EXCEPTION = 0xE06D7363

# For the machine's own user.
# "Most likely", here and in the text for other machines: on Windows the signal is
# an uncaught C++ exception on a GPU load, which a lost device also raises.
GPU_OUT_OF_MEMORY_TEXT = (
    "The GPU stopped this reply, most likely because it ran out of memory. "
    "Close other programs that use the GPU, or load a smaller model."
)

# Refusing to serve because the loaded model has not passed its answer check
# (services/acceptance.py). Fixed text, part of the HTTP contract.
MODEL_NOT_VERIFIED_TEXT = "The serving machine has not verified this model."

# For any other machine. Chosen by category, never built from an exception.
_OTHER_MACHINE_TEXT: dict[str, str] = {
    INFERENCE_ERROR: "Inference failed",
    GPU_OUT_OF_MEMORY: "The serving machine's GPU most likely ran out of memory.",
    "model_mismatch": "Model identity changed",
    MODEL_NOT_VERIFIED: MODEL_NOT_VERIFIED_TEXT,
}

_GPU_OOM_PHRASES = (
    "erroroutofdevicememory",  # vk::OutOfDeviceMemoryError what()
    "cuda out of memory",
    "cuda error: out of memory",
    "cudamalloc failed: out of memory",
    "hipmalloc failed: out of memory",
    "mps backend out of memory",
)


class InferenceFailure(RuntimeError):
    """A classified inference failure. ``str(exc)`` is the text for the machine's own user."""

    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category
        self.message = message


@dataclass(frozen=True)
class FailureDescription:
    category: str
    message: str  # for the machine's own user


def is_native_cpp_exception(exc: BaseException) -> bool:
    """True for the OSError ctypes raises when native code throws a C++ exception."""
    if not isinstance(exc, OSError):
        return False
    code = getattr(exc, "winerror", None)
    if isinstance(code, int) and not isinstance(code, bool):
        return (code & 0xFFFFFFFF) == MSVC_CPP_EXCEPTION
    # A re-wrapped error keeps the code in its text: "[WinError -529697949] Windows Error 0xe06d7363".
    return f"0x{MSVC_CPP_EXCEPTION:08x}" in str(exc).lower()


def classify_inference_error(exc: BaseException, *, gpu_offload: bool) -> str:
    """The stable category for *exc*. ``gpu_offload`` is whether the loaded model runs on a GPU."""
    if isinstance(exc, InferenceFailure):
        return exc.category
    if isinstance(exc, MemoryError):
        return INFERENCE_ERROR  # host RAM, not the GPU
    if is_native_cpp_exception(exc):
        return GPU_OUT_OF_MEMORY if gpu_offload else INFERENCE_ERROR
    if isinstance(exc, OSError):
        return INFERENCE_ERROR
    if type(exc).__name__ == "OutOfMemoryError":  # torch.OutOfMemoryError / torch.cuda.OutOfMemoryError
        return GPU_OUT_OF_MEMORY
    text = str(exc).lower()
    if any(phrase in text for phrase in _GPU_OOM_PHRASES):
        return GPU_OUT_OF_MEMORY
    return INFERENCE_ERROR


def describe_failure(exc: BaseException, *, gpu_offload: bool) -> FailureDescription:
    """Category plus the text for the machine's own user.

    Only the GPU out-of-memory failure gets a fixed sentence; every other
    failure keeps the engine's own message.
    """
    category = classify_inference_error(exc, gpu_offload=gpu_offload)
    if category == GPU_OUT_OF_MEMORY:
        return FailureDescription(category, GPU_OUT_OF_MEMORY_TEXT)
    if isinstance(exc, InferenceFailure):
        return FailureDescription(category, exc.message)
    return FailureDescription(category, str(exc))


# What llama-cpp-python raises when the prompt does not fit the context window:
# "Requested tokens (N) exceed context window of M". It raises when N is M or more.
_PROMPT_OVERFLOW = re.compile(r"Requested tokens \((\d+)\) exceed context window of (\d+)")


def prompt_overflow(text: object) -> tuple[int, int] | None:
    """``(prompt tokens, context window)`` when ``text`` is the engine's message for a
    prompt that does not fit, else None. Takes the exception or its text."""
    match = _PROMPT_OVERFLOW.search(str(text)) if text is not None else None
    return (int(match.group(1)), int(match.group(2))) if match else None


def fixed_category(value: object) -> str:
    """A category this module knows. Anything else, whatever it is, is inference_error."""
    return value if isinstance(value, str) and value in _OTHER_MACHINE_TEXT else INFERENCE_ERROR


def other_machine_text(category: object) -> str:
    """The fixed sentence for another machine, selected by category alone."""
    return _OTHER_MACHINE_TEXT[fixed_category(category)]


def other_machine_error(category: object) -> dict[str, str]:
    """``{"category", "message"}`` for an error event sent to another machine."""
    chosen = fixed_category(category)
    return {"category": chosen, "message": _OTHER_MACHINE_TEXT[chosen]}
