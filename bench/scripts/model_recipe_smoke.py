"""Exercise a CPU recipe with existing weights in isolated temporary storage.

No download or public listener is used. This verifies the recipe-to-runtime
contract, not coding quality, speed or GPU support.
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
    from potluck_local.services import model_recipes, runtime as runtime_module
    from potluck_local.services.memory_db import init_memory_db
    from potluck_local.services.model_lifecycle import get_model_lifecycle
    from potluck_local.services.settings_manager import get_settings
    from potluck_local.utils.paths import models_dir

    recipe = model_recipes.get_recipe(args.recipe)
    if recipe is None or recipe['engine']['acceleration'] != 'cpu':
        raise ValueError('Choose a bundled CPU recipe for this smoke check')
    model = recipe['model']
    path = models_dir() / model['id'] / 'model.gguf'
    path.parent.mkdir(parents=True)
    # A separate directory is needed for model identity; a hard link avoids
    # copying or editing the existing weights and is removed with temporary data.
    os.link(args.model.resolve(), path)
    await init_memory_db()
    rt = runtime_module.LLMRuntime()
    original = runtime_module._runtime
    runtime_module._runtime = rt
    lifecycle = get_model_lifecycle()
    lifecycle.remember('previous-model')
    settings_before = get_settings().model_dump()
    checks = {}
    result = {'kind': 'recipe_protocol_smoke_not_quality_benchmark',
              'utc': datetime.now(timezone.utc).isoformat(),
              'platform': platform.system(), 'arch': platform.machine(),
              'recipe': recipe, 'checks': checks}
    source_root = Path(runtime_module.__file__).parents[1]
    sources = ['services/runtime.py', 'services/local_server.py', 'services/model_recipes.py',
               'routers/models.py', 'data/model-recipes.json']
    result['source_files_sha256'] = {
        name: hashlib.sha256((source_root / name).read_bytes()).hexdigest() for name in sources
    }
    try:
        transport = httpx.ASGITransport(app=create_app(), client=('127.0.0.1', 50000))
        async with httpx.AsyncClient(transport=transport, base_url='http://127.0.0.1:8399', timeout=180) as client:
            response = await client.get('/models/recipes')
            checks['bundled_recipe_listed'] = response.status_code == 200 and any(
                row['id'] == args.recipe for row in response.json()['recipes'])
            response = await client.get(f'/models/recipes/{args.recipe}/preflight')
            report = response.json()
            result['preflight'] = report
            checks['preflight_can_load'] = response.status_code == 200 and report['can_load'] and report['artifact_verified']
            checks['preflight_did_not_load_or_change_settings'] = not rt.is_loaded() and settings_before == get_settings().model_dump()
            if not checks['preflight_can_load']:
                result['passed'] = False
                return result
            response = await client.post(f'/models/recipes/{args.recipe}/load')
            loaded = response.json()
            checks['recipe_loaded'] = response.status_code == 200 and loaded.get('recipe_id') == args.recipe
            checks['exact_artifact'] = rt.current_model_sha256() == model['sha256']
            checks['exact_context_and_engine'] = loaded.get('n_ctx') == recipe['runtime']['context_size'] and loaded.get('engine_commit') == recipe['engine']['commit']
            checks['session_only'] = loaded.get('autoload') is False and get_settings().runtime.startup_model_id is None
            settings_before['runtime']['startup_model_id'] = None
            checks['preferences_preserved'] = settings_before == get_settings().model_dump()
            checks['unqualified_not_lent'] = rt.answers_verified() is False
            response = await client.post('/v1/chat/completions', headers={'X-Potluck-Scope': 'local'},
                json={'model': model['id'], 'potluck_model_sha256': model['sha256'],
                      'messages': [{'role': 'user', 'content': 'What is 2 + 2?'}],
                      'max_tokens': 24, 'temperature': 0})
            body = response.json()
            checks['gateway_exact_model_response'] = response.status_code == 200 and body.get('model') == model['id'] and bool(body.get('choices'))
            native = rt._model
            response = await client.post(f'/models/{model["id"]}/unload')
            checks['unload_stops_child'] = response.status_code == 200 and not rt.is_loaded() and native.process.process.poll() is not None
        result['passed'] = all(checks.values())
        return result
    finally:
        await rt.unload_model_async()
        runtime_module._runtime = original


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', type=Path, required=True)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--recipe', default='qwen2.5-0.5b-instruct-q4-k-m-cpu-4k')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='potluck-recipe-smoke-') as data:
        os.environ['POTLUCK_DATA_DIR'] = data
        os.environ['POTLUCK_SPLIT_ENGINE_DIR'] = str(args.bundle.resolve())
        os.environ['POTLUCK_COORDINATOR_HTTP_URL'] = 'http://127.0.0.1:9'
        os.environ['POTLUCK_LOCAL_API_SOCKET'] = str(Path(data) / 'absent-mesh.sock')
        result = asyncio.run(run(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({'passed': result['passed'], 'output': str(args.output)}))
    if not result['passed']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
