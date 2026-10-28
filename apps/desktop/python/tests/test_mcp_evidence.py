"""Evidence MCP tools are opt-in and bound to the owner's startup scope."""
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from potluck_local.mcp import server
from potluck_local.mcp.tools import evidence


@asynccontextmanager
async def _test_lifespan(_server):
    # In-memory transport cancels the server task on disconnect. Avoid starting
    # the production lifespan, whose session and heartbeat outlive this fixture.
    yield None


def _build_test_mcp():
    mcp = server._build_mcp()
    mcp._mcp_server.lifespan = _test_lifespan
    return mcp


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(evidence, "get_settings", lambda: SimpleNamespace(
        privacy=SimpleNamespace(memory_enabled=True), memory=SimpleNamespace(enabled=True)))


@pytest.mark.asyncio
async def test_opt_in_and_fixed_scope_over_mcp(monkeypatch, enabled):
    monkeypatch.delenv(evidence.EVIDENCE_SCOPE_ENV, raising=False)
    plain = _build_test_mcp()
    assert "evidence_search" not in {t.name for t in await plain.list_tools()}
    monkeypatch.setenv(evidence.EVIDENCE_SCOPE_ENV, json.dumps({
        "workspace": "owner", "collection_ids": ["allowed"]}))
    search = AsyncMock(return_value={"source_chunks": []})
    resolve = AsyncMock(return_value=None)
    monkeypatch.setattr(evidence, "lookup_evidence", search)
    monkeypatch.setattr(evidence, "resolve_citation", resolve)
    mcp = _build_test_mcp()
    monkeypatch.setenv(evidence.EVIDENCE_SCOPE_ENV, json.dumps({
        "workspace": "other", "collection_ids": ["other"]}))
    async with create_connected_server_and_client_session(mcp._mcp_server) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
        assert {"evidence_search", "evidence_resolve"} <= names
        result = await client.call_tool("evidence_search", {
            "query": "secret query", "workspace": "other", "collection_ids": ["other"]})
        assert not result.isError
        scope = search.await_args.args[0]
        assert scope.workspace == "owner" and scope.collection_ids == ("allowed",)
        result = await client.call_tool("evidence_resolve", {"citation_id": "outside"})
        assert not result.isError
        assert resolve.await_args.args[0] == scope


@pytest.mark.parametrize("raw", ["", "{}", '[]', '{"workspace":"x","collection_ids":[]}',
                                  '{"workspace":"x","collection_ids":["*"]}',
                                  '{"workspace":"x","collection_ids":["a","a"]}'])
def test_invalid_scope_fails_closed(monkeypatch, raw):
    monkeypatch.setenv(evidence.EVIDENCE_SCOPE_ENV, raw)
    with pytest.raises(ValueError, match="Invalid POTLUCK_EVIDENCE_SCOPE configuration"):
        server._build_mcp()


@pytest.mark.asyncio
async def test_privacy_and_generic_errors(monkeypatch, enabled):
    monkeypatch.setenv(evidence.EVIDENCE_SCOPE_ENV, '{"workspace":"x","collection_ids":["a"]}')
    search = AsyncMock(side_effect=RuntimeError("/private/path secret"))
    monkeypatch.setattr(evidence, "lookup_evidence", search)
    mcp = _build_test_mcp()
    async with create_connected_server_and_client_session(mcp._mcp_server) as client:
        result = await client.call_tool("evidence_search", {"query": "secret"})
        assert result.isError and "/private/path" not in str(result)
        monkeypatch.setattr(evidence, "get_settings", lambda: SimpleNamespace(
            privacy=SimpleNamespace(memory_enabled=False), memory=SimpleNamespace(enabled=True)))
        result = await client.call_tool("evidence_search", {"query": "secret"})
        assert result.isError and search.await_count == 1
        result = await client.call_tool("evidence_resolve", {"citation_id": "secret"})
        assert result.isError


@pytest.mark.asyncio
async def test_observer_receives_no_evidence_arguments(monkeypatch):
    observer = SimpleNamespace(log_tool_call=AsyncMock(), update_session=AsyncMock())
    monkeypatch.setattr(server, "get_observer", lambda: observer)
    monkeypatch.setattr(server, "get_current_session_id", lambda: "session")
    monkeypatch.setattr(server, "_client_identified", True)
    async def fn(**kwargs):
        return {}
    await server._wrap_tool_fn("evidence_search", fn)(query="sensitive", project="sensitive")
    import asyncio
    await asyncio.sleep(0)
    assert observer.log_tool_call.await_args.kwargs["args"] == {}
    observer.update_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_database_scope_and_citation_lifecycle(tmp_path, monkeypatch, enabled):
    import aiosqlite

    from potluck_local.services.evidence_lookup import EvidenceScope, lookup_evidence
    from potluck_local.services.memory_db import get_memory_db_path, init_memory_db
    from potluck_local.services.source_collections import register_collection, scan_collection

    monkeypatch.setenv("POTLUCK_MEMORY_DB_PATH", str(tmp_path / "test.db"))
    await init_memory_db()
    one = tmp_path / "one"
    two = tmp_path / "two"
    one.mkdir()
    two.mkdir()
    document = one / "a.md"
    document.write_text("# Deploy\nAttribution ownermarker is live.")
    (two / "b.md").write_text("# Deploy\nAttribution foreignmarker is live.")
    first = await register_collection("owner", "one", one)
    second = await register_collection("other", "two", two)
    await scan_collection("owner", first)
    await scan_collection("other", second)
    outsider = await lookup_evidence(EvidenceScope("other", (second,)), "attribution")
    foreign_citation = outsider["source_chunks"][0]["citation_id"]
    source = (await lookup_evidence(EvidenceScope("owner", (first,)), "attribution"))[
        "source_chunks"
    ][0]
    async with aiosqlite.connect(get_memory_db_path()) as db:
        await db.execute(
            "INSERT INTO reviewed_claims "
            "(id,workspace,claim_key,content,status,reviewer,reason,created_at,reviewed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ("claim", "owner", "key", "Attribution ownerclaim", "approved", "reviewer",
             "checked", "now", "now"),
        )
        await db.execute(
            "INSERT INTO reviewed_claim_sources VALUES (?,?,?,?,?)",
            ("claim", first, source["document_id"], source["revision_id"], source["chunk_id"]),
        )
        await db.commit()

    def config(workspace, collection):
        monkeypatch.setenv(evidence.EVIDENCE_SCOPE_ENV, json.dumps({
            "workspace": workspace, "collection_ids": [collection]}))

    config("owner", first)
    mcp = _build_test_mcp()
    config("other", second)
    async with create_connected_server_and_client_session(mcp._mcp_server) as client:
        response = await client.call_tool("evidence_search", {
            "query": "attribution", "workspace": "other", "collection_ids": [second]})
        assert not response.isError
        result = json.loads(response.content[0].text)
        assert "ownermarker" in str(result) and "ownerclaim" in str(result)
        assert "foreignmarker" not in str(result)
        citations = [item["citation_id"] for key in ("source_chunks", "reviewed_claims")
                     for item in result[key]]
        assert len(citations) == 2
        for citation in citations:
            resolved = await client.call_tool("evidence_resolve", {"citation_id": citation})
            assert not resolved.isError and "foreignmarker" not in str(resolved)
            assert json.loads(resolved.content[0].text) is not None
        outside = await client.call_tool("evidence_resolve", {"citation_id": foreign_citation})
        assert "foreignmarker" not in str(outside)
        assert not outside.content
        async with aiosqlite.connect(get_memory_db_path()) as db:
            await db.execute("UPDATE reviewed_claims SET status='superseded' WHERE id='claim'")
            await db.commit()
        claim_resolved = await client.call_tool("evidence_resolve", {"citation_id": citations[1]})
        assert not claim_resolved.content
        document.write_text("# Deploy\nAttribution changed.")
        await scan_collection("owner", first)
        source_resolved = await client.call_tool("evidence_resolve", {"citation_id": citations[0]})
        assert not source_resolved.content
        fresh = await client.call_tool("evidence_search", {"query": "attribution"})
        new_citation = json.loads(fresh.content[0].text)["source_chunks"][0]["citation_id"]
        document.unlink()
        await scan_collection("owner", first)
        removed = await client.call_tool("evidence_resolve", {"citation_id": new_citation})
        assert not removed.content

    restarted = _build_test_mcp()
    async with create_connected_server_and_client_session(restarted._mcp_server) as client:
        response = await client.call_tool("evidence_search", {"query": "attribution"})
        assert "foreignmarker" in str(response) and "ownermarker" not in str(response)


@pytest.mark.asyncio
@pytest.mark.parametrize("privacy,memory,fail", [(False, True, False), (True, False, False),
                                                  (True, True, True)])
async def test_privacy_gates_both_tools(monkeypatch, privacy, memory, fail):
    monkeypatch.setenv(evidence.EVIDENCE_SCOPE_ENV, '{"workspace":"x","collection_ids":["a"]}')
    def settings():
        if fail:
            raise RuntimeError("settings unavailable")
        return SimpleNamespace(privacy=SimpleNamespace(memory_enabled=privacy),
                               memory=SimpleNamespace(enabled=memory))
    monkeypatch.setattr(evidence, "get_settings", settings)
    search = AsyncMock()
    resolve = AsyncMock()
    monkeypatch.setattr(evidence, "lookup_evidence", search)
    monkeypatch.setattr(evidence, "resolve_citation", resolve)
    mcp = _build_test_mcp()
    async with create_connected_server_and_client_session(mcp._mcp_server) as client:
        assert (await client.call_tool("evidence_search", {"query": "secret"})).isError
        assert (await client.call_tool("evidence_resolve", {"citation_id": "secret"})).isError
    search.assert_not_awaited()
    resolve.assert_not_awaited()


@pytest.mark.asyncio
async def test_observer_database_never_stores_evidence_content(tmp_path, monkeypatch, enabled):
    import asyncio
    import aiosqlite

    from potluck_local.services.agent_observability import AgentObserver
    from potluck_local.services.memory_db import get_memory_db_path, init_memory_db

    monkeypatch.setenv("POTLUCK_MEMORY_DB_PATH", str(tmp_path / "test.db"))
    await init_memory_db()
    observer = AgentObserver()
    session = await observer.open_session(client_name=None, client_version=None,
                                          transport="stdio", pid=1)
    finished = asyncio.Event()
    original_log = observer.log_tool_call
    completed = 0

    async def log_and_signal(**kwargs):
        nonlocal completed
        try:
            await original_log(**kwargs)
        finally:
            completed += 1
            if completed == 4:
                finished.set()

    monkeypatch.setattr(observer, "log_tool_call", log_and_signal)
    monkeypatch.setattr(server, "get_observer", lambda: observer)
    monkeypatch.setattr(server, "get_current_session_id", lambda: session)
    monkeypatch.setattr(server, "_client_identified", True)
    monkeypatch.setenv(evidence.EVIDENCE_SCOPE_ENV, '{"workspace":"x","collection_ids":["a"]}')
    monkeypatch.setattr(evidence, "lookup_evidence", AsyncMock(
        side_effect=[{"source_chunks": [{"content": "RESULT_SECRET"}]},
                     RuntimeError("ERROR_SECRET")]))
    monkeypatch.setattr(evidence, "resolve_citation", AsyncMock(
        side_effect=[{"content": "RESULT_SECRET"}, RuntimeError("ERROR_SECRET")]))
    mcp = _build_test_mcp()
    async with create_connected_server_and_client_session(mcp._mcp_server) as client:
        for name, arg in (("evidence_search", "query"), ("evidence_resolve", "citation_id")):
            ok = await client.call_tool(name, {arg: "ARGUMENT_SECRET"})
            assert not ok.isError and "RESULT_SECRET" in str(ok)
            error = await client.call_tool(name, {arg: "ARGUMENT_SECRET"})
            assert error.isError and "ERROR_SECRET" not in str(error)
    await asyncio.wait_for(finished.wait(), timeout=5)
    async with aiosqlite.connect(get_memory_db_path()) as db:
        rows = await (await db.execute(
            "SELECT * FROM mcp_tool_calls WHERE session_id=?", (session,))).fetchall()
    assert len(rows) == 4
    assert all(secret not in str(rows) for secret in
               ("ARGUMENT_SECRET", "RESULT_SECRET", "ERROR_SECRET"))
