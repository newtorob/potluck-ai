"""Exercise the real native backend and gateway on CPU with an existing GGUF.

PYTHONPATH=apps/desktop/python python bench/scripts/native_local_smoke.py \
  --model /path/to/model.gguf --bundle /path/to/native/split --output result.json

Uses temporary data, no sidecar listener, and a private ephemeral native port.
This is a protocol smoke check, not a coding-quality or speed benchmark.
"""
import argparse
import asyncio
import hashlib
import json
import os
import platform
import tempfile
from datetime import datetime, timezone
from pathlib import Path


async def run(args):
    import httpx
    from potluck_local.app import create_app
    from potluck_local.services import runtime as runtime_module
    from potluck_local.services.local_server import bundle
    from potluck_local.services.model_identity import artifact_identity
    from potluck_local.services.memory_db import init_memory_db

    await init_memory_db()
    runtime = runtime_module.LLMRuntime()
    original = runtime_module._runtime
    runtime_module._runtime = runtime
    tool = {"type": "function", "function": {"name": "get_weather", "description": "Get current weather",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}}
    result = {"kind": "protocol_smoke_not_quality_benchmark", "utc": datetime.now(timezone.utc).isoformat(),
              "platform": platform.system(), "machine": platform.machine(), "device": "cpu",
              "threads": 2, "context": 4096, "cache_mib": 128,
              "engine_commit": bundle()[1]["commit"],
              "model_filename": args.model.name, "model_sha256": artifact_identity(args.model)[0],
              "checks": {}}
    sources = [Path(runtime_module.__file__), Path(runtime_module.__file__).with_name("local_server.py")]
    result["source_files_sha256"] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}
    try:
        await runtime.load_model_async(str(args.model.resolve()), backend="llama-server", n_ctx=4096,
                                       n_gpu_layers=0, n_threads=2, native_cache_mib=128)
        model_id = runtime.current_model_id()
        checks = result["checks"]
        checks["identity"] = runtime.current_model_sha256() == result["model_sha256"]
        checks["unqualified_not_lent"] = runtime.answers_verified() is False
        transport = httpx.ASGITransport(app=create_app(), client=("127.0.0.1", 50000))
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8399", timeout=90) as client:
            headers = {"X-Potluck-Scope": "local"}
            response = await client.get("/v1/models", headers=headers)
            checks["gateway_tools_advertised"] = response.status_code == 200 and any(
                m.get("id") == model_id and m.get("supports_tools") is True for m in response.json().get("data", []))
            user = {"role": "user", "content": "Use get_weather to get the current weather in Paris."}
            response = await client.post("/v1/chat/completions", headers=headers,
                json={"model": model_id, "messages": [user], "tools": [tool],
                      "tool_choice": {"type": "function", "function": {"name": "get_weather"}},
                      "max_tokens": 96, "temperature": 0})
            body = response.json()
            result["tool_response"] = body
            calls = body.get("choices", [{}])[0].get("message", {}).get("tool_calls", [])
            checks["tool_call"] = response.status_code == 200 and bool(calls) and calls[0]["function"]["name"] == "get_weather"
            checks["public_model_id"] = body.get("model") == model_id and str(args.model) not in json.dumps(body)
            if calls:
                history = [user, body["choices"][0]["message"],
                           {"role": "tool", "tool_call_id": calls[0]["id"], "content": '{"city":"Paris","temperature_c":17}'}]
                response = await client.post("/v1/chat/completions", headers=headers,
                    json={"model": model_id, "messages": history, "max_tokens": 64, "temperature": 0})
                result["tool_result_response"] = response.json()
                checks["tool_result_turn"] = response.status_code == 200 and "17" in json.dumps(response.json())
            # Closing a live stream must stop native work before the next turn.
            stream = runtime.stream_chat_raw([{"role": "user", "content": "Count upward, one number at a time."}], max_tokens=512)
            async for chunk in stream:
                if any(c.get("delta", {}).get("content") for c in chunk.get("choices", [])):
                    break
            await stream.aclose()
            checks["cancel_preserves_model"] = runtime.is_loaded()
            response = await client.post("/v1/chat/completions", headers=headers,
                json={"model": model_id, "messages": [{"role": "user", "content": "What is 2 + 2?"}],
                      "max_tokens": 24, "temperature": 0})
            checks["request_after_cancel"] = response.status_code == 200 and bool(response.json().get("choices"))
        native = runtime._model
        await runtime.unload_model_async()
        checks["unload_stops_process"] = not runtime.is_loaded() and native.process.process.poll() is not None
        result["passed"] = all(checks.values())
        return result
    finally:
        await runtime.unload_model_async()
        runtime_module._runtime = original


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="potluck-native-smoke-") as data:
        os.environ["POTLUCK_DATA_DIR"] = data
        os.environ["POTLUCK_SPLIT_ENGINE_DIR"] = str(args.bundle.resolve())
        os.environ["POTLUCK_COORDINATOR_HTTP_URL"] = "http://127.0.0.1:9"
        os.environ["POTLUCK_LOCAL_API_SOCKET"] = str(Path(data) / "absent-mesh.sock")
        result = asyncio.run(run(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"passed": result["passed"], "output": str(args.output)}))
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
