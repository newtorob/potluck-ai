"""Run one frozen grounding suite locally; assessments remain unscored."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from potluck_local.services import company_brain_eval_runner as base
from potluck_local.services.runtime import get_runtime

FIXTURES = base.FIXTURES / 'grounding_v2'


def read_suite(suite: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, bytes]]:
    if suite not in ('development', 'fresh'):
        raise ValueError('Invalid suite')
    files = {}
    for name in (suite, 'rubric'):
        data = (FIXTURES / f'{name}.json').read_bytes()
        expected = (FIXTURES / f'{name}.sha256').read_text().split()[0]
        if base.digest(data) != expected:
            raise ValueError(f'Frozen fixture hash mismatch: {name}')
        files[f'{name}.json'] = data
        files[f'{name}.sha256'] = (FIXTURES / f'{name}.sha256').read_bytes()
    manifest = json.loads(files[f'{suite}.json'])
    rubric = json.loads(files['rubric.json'])
    ids = [case['id'] for case in manifest['cases']]
    if (not ids or len(ids) != len(set(ids)) or any(
            not isinstance(case['id'], str) or not case['id']
            or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789-' for c in case['id'])
            or not isinstance(case['allowed_source'], str) or not isinstance(case['question'], str)
            for case in manifest['cases'])):
        raise ValueError('Invalid frozen suite')
    if suite == 'fresh' and set(ids) != set(rubric['cases']):
        raise ValueError('Frozen suite and rubric mismatch')
    return manifest, rubric, files


async def run(out: Path, suite: str, model_path: Path, expected_sha: str,
              n_ctx: int, threads: int) -> None:
    manifest, _, files = read_suite(suite)
    if n_ctx != 8192 or threads != 8:
        raise ValueError('Grounding runs require context size 8192 and 8 threads')
    if not model_path.is_file() or model_path.is_symlink():
        raise ValueError('An existing local model file is required')
    with model_path.open('rb') as stream:
        model_sha = hashlib.file_digest(stream, 'sha256').hexdigest()
    if model_sha != expected_sha.lower():
        raise ValueError('Model file hash mismatch')
    out = out.expanduser().absolute()
    out = out.parent.resolve(strict=True) / out.name
    out.mkdir(mode=0o700, parents=False, exist_ok=False)
    (out / 'data').mkdir(mode=0o700)
    for name, data in files.items():
        with (out / name).open('xb') as stream:
            stream.write(data)
    settings = {'n_ctx': n_ctx, 'n_threads': threads, 'n_gpu_layers': 0,
                'cpu_only': True, 'rpc_servers': None}
    base.write_new(out / 'run-metadata.json', {
        'suite': suite, 'corpus_id': manifest['corpus_id'],
        'suite_sha256': base.digest(files[f'{suite}.json']),
        'rubric_sha256': base.digest(files['rubric.json']),
        'model_path': str(model_path), 'model_sha256': model_sha,
        'settings': settings, 'code_identity': base.identity(),
        'started_at': datetime.now(timezone.utc).isoformat(),
    })
    with base.isolated_environment(POTLUCK_DATA_DIR=str(out / 'data')):
        runtime = get_runtime()
        try:
            report = runtime.load_model(str(model_path), **settings)
            base.write_new(out / 'model-load.json', report)
            if runtime.current_model_sha256() != model_sha:
                raise ValueError('Loaded model identity mismatch')
            model = runtime.current_model_id()
            outputs = {}
            for case in manifest['cases']:
                outputs[case['id']] = await base.attempt(out, case, 'raw', runtime, model)
            base.write_new(out / 'raw.json', {'corpus_id': manifest['corpus_id'],
                'suite_sha256': base.digest(files[f'{suite}.json']),
                'case_ids': [case['id'] for case in manifest['cases']],
                'assessment': None, 'outputs': outputs})
        except Exception as exc:
            base.write_new(out / 'run-error.json', {'type': type(exc).__name__, 'message': str(exc)})
            raise
        finally:
            runtime.unload_model()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite', choices=('development', 'fresh'), required=True)
    parser.add_argument('--model-path', type=Path, required=True)
    parser.add_argument('--model-sha256', required=True)
    parser.add_argument('--context-size', type=int, required=True)
    parser.add_argument('--threads', type=int, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(run(args.output_dir, args.suite, args.model_path.expanduser().absolute(),
                    args.model_sha256, args.context_size, args.threads))


if __name__ == '__main__':
    main()
