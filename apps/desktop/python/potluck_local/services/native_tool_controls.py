"""Explicit tool concurrency on the pinned native backend, including peer retries."""
from .gateway_request import RequestError
from .reasoning_controls import supported


def require(runtime):
    if not supported(runtime):
        raise RequestError("tool_concurrency_unsupported",
                           "Explicit tool concurrency requires an unbatched native engine.")


def peer_supported(cap):
    return cap.get("parallel_tool_calls_version") == 1
