"""A failed job's log line names the job and the kind of error, nothing more.

An exception raised while serving can quote the prompt or the output of the
member whose job it is, so neither its message nor a traceback may reach the
log of the machine that served it.
"""
from __future__ import annotations

import asyncio
import json
import logging
import traceback
import uuid

from potluck_local.contributor import client, inference
from potluck_local.contributor.config import Settings
from potluck_local.contributor.schemas import JobAssignedMessage

# Stands for prompt or output text that an error can quote.
CONTENT_MARKER = "marker-51c9-private-content"


class FailingEngine:
    """Yields one token, then fails the way an engine can mid-generation."""

    async def stream_completion(self, **_):
        yield "x"
        raise ValueError(f"cannot sample after {CONTENT_MARKER}")

    stream_chat = stream_completion


class RecordingWS:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, text):
        self.sent.append(json.loads(text))


class FailingWS(RecordingWS):
    """Refuses token frames with an error that quotes what it was sending."""

    async def send(self, text):
        frame = json.loads(text)
        if frame["type"] == "job_token":
            raise ValueError(f"cannot frame {CONTENT_MARKER}")
        self.sent.append(frame)


def _job() -> JobAssignedMessage:
    return JobAssignedMessage(type="job_assigned", job_id=uuid.uuid4(),
                              model_slug="some-model", prompt="hi")


def _assert_redacted(caplog, event: str, job: JobAssignedMessage) -> None:
    failures = [r for r in caplog.records if event in r.getMessage()]
    assert len(failures) == 1
    assert str(job.job_id) in failures[0].getMessage()
    assert "ValueError" in failures[0].getMessage()
    assert failures[0].exc_info is None and failures[0].exc_text is None
    for record in caplog.records:
        shown = [record.getMessage(), repr(record.args), record.exc_text or ""]
        if record.exc_info:
            shown.append("".join(traceback.format_exception(*record.exc_info)))
        assert CONTENT_MARKER not in "\n".join(shown), record.getMessage()
    assert CONTENT_MARKER not in caplog.text


def test_real_job_failure_log_has_no_traceback_and_no_exception_text(caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(inference, "get_ollama_engine", lambda **_: FailingEngine())
    settings = Settings(coordinator_url="ws://x/peers/connect", binary_hash="h",
                        inference_backend="ollama")
    ws, job = RecordingWS(), _job()

    asyncio.run(client._handle_real_job(ws, job, settings))

    assert ws.sent[-1]["type"] == "job_error"
    assert ws.sent[-1]["error_message"] == "ValueError"
    _assert_redacted(caplog, "real_job_internal_error", job)


def test_stub_job_failure_log_has_no_traceback_and_no_exception_text(caplog):
    caplog.set_level(logging.DEBUG)
    ws, job = FailingWS(), _job()

    asyncio.run(client._handle_stub_job(ws, job))

    assert ws.sent[-1]["type"] == "job_error"
    _assert_redacted(caplog, "stub_job_internal_error", job)
