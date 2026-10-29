"""Local-only frozen synthetic investigation evaluation; no automatic labels."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import asdict
import asyncio
import hashlib
import importlib.metadata
import json
import os
import platform
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from potluck_local.services import investigations
from potluck_local.services.evidence_lookup import EvidenceScope
from potluck_local.services.memory_db import init_memory_db
from potluck_local.services.reviewed_knowledge import approve_claim, propose_claim, supersede_claim
from potluck_local.services.runtime import get_runtime
from potluck_local.services.source_collections import register_collection, scan_collection

ENGINE = Path(__file__).resolve().parents[2]
FIXTURES = ENGINE / 'evals' / 'company_brain'


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def frozen_manifest() -> dict[str, Any]:
    for name in ('synthetic_cases', 'holdout'):
        path = FIXTURES / f'{name}.json'
        expected = (FIXTURES / f'{name}.sha256').read_text().split()[0]
        if digest(path.read_bytes()) != expected:
            raise ValueError(f'Frozen fixture hash mismatch: {name}')
    manifest = json.loads((FIXTURES / 'synthetic_cases.json').read_text())
    holdout = json.loads((FIXTURES / 'holdout.json').read_text())
    if (manifest['corpus_id'] != holdout['corpus_id'] or
            [x['id'] for x in manifest['cases']] != [x['id'] for x in holdout['cases']]):
        raise ValueError('Frozen fixture case mismatch')
    return manifest


def write_new(path: Path, obj: Any) -> None:
    with path.open('x', encoding='utf-8') as stream:
        json.dump(obj, stream, indent=2, ensure_ascii=False, default=str)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())


def identity() -> dict[str, Any]:
    files = sorted((ENGINE / 'potluck_local').rglob('*.py'))
    hashes = {str(p.relative_to(ENGINE)): digest(p.read_bytes()) for p in files}
    try:
        commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ENGINE, text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        commit = None
    try:
        wheel = importlib.metadata.version('llama-cpp-python')
    except importlib.metadata.PackageNotFoundError:
        wheel = 'unavailable'
    return {'git_head': commit, 'python_source_sha256': hashes,
            'python': sys.version, 'platform': platform.platform(), 'llama_cpp_python': wheel}


class CaptureRuntime:
    def __init__(self, runtime: Any, attempt_dir: Path, request_metadata: dict[str, Any] | None = None):
        self.runtime, self.attempt_dir = runtime, attempt_dir
        self.request_metadata = request_metadata
        self.call_count = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self.runtime, name)

    async def chat(self, messages: Any, **kwargs: Any) -> Any:
        self.call_count += 1
        call_number = self.call_count
        call_dir = self.attempt_dir / 'calls' / f'{call_number:03d}'
        call_dir.mkdir(parents=True)
        request = {'messages': messages, 'settings': kwargs}
        if self.request_metadata is not None:
            request.update(self.request_metadata)
        write_new(call_dir / 'request.json', request)
        if call_number == 1:
            write_new(self.attempt_dir / 'request.json', request)
        try:
            reply = await self.runtime.chat(messages, **kwargs)
        except BaseException as exc:
            error = {'type': type(exc).__name__, 'message': str(exc)}
            write_new(call_dir / 'runtime-error.json', error)
            if call_number == 1:
                write_new(self.attempt_dir / 'runtime-error.json', error)
            raise
        write_new(call_dir / 'raw-reply.json', reply)
        if call_number == 1:
            write_new(self.attempt_dir / 'raw-reply.json', reply)
        return reply


@contextmanager
def isolated_environment(**values: str):
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


async def setup_case(root: Path, case: dict[str, Any], arm: str) -> EvidenceScope:
    workspace = 'synthetic-eval'
    folder = root / 'allowed'
    folder.mkdir()
    (folder / 'source.md').write_text(case['allowed_source'], encoding='utf-8')
    cid = await register_collection(workspace, 'allowed', folder)
    scan = await scan_collection(workspace, cid)
    write_new(root / 'allowed-registration.json', {'workspace': workspace, 'collection_id': cid,
        'root': str(folder), 'source_sha256': digest(case['allowed_source'].encode()), 'scan': asdict(scan)})
    scope = EvidenceScope(workspace, (cid,))
    if 'excluded_source' in case:
        excluded = root / 'excluded'
        excluded.mkdir()
        (excluded / 'source.md').write_text(case['excluded_source'], encoding='utf-8')
        other_workspace = 'synthetic-other-workspace'
        other = await register_collection(other_workspace, 'excluded', excluded)
        scan = await scan_collection(other_workspace, other)
        write_new(root / 'excluded-registration.json', {'workspace': other_workspace,
            'collection_id': other, 'root': str(excluded),
            'source_sha256': digest(case['excluded_source'].encode()), 'scan': asdict(scan)})
    # Only source assertions with explicit support are approved; the injection and
    # unsupported cases stay raw-evidence controls in both arms.
    if arm == 'candidate' and case['id'] in ('factual-grounding', 'superseded-claim'):
        async with aiosqlite.connect(os.environ['POTLUCK_MEMORY_DB_PATH']) as db:
            row = await (await db.execute(
                'SELECT d.id,d.revision_id,k.id FROM source_documents d '
                'JOIN source_chunks k ON k.document_id=d.id WHERE d.collection_id=?', (cid,))).fetchone()
        if not row:
            raise ValueError('Synthetic source produced no chunk')
        ref = dict(zip(('document_id', 'revision_id', 'chunk_id'), row))
        ref['collection_id'] = cid
        if case['id'] == 'superseded-claim':
            old = await propose_claim(workspace, (cid,), 'rollout', 'Rollout status was previously pending.', [ref])
            await approve_claim(workspace, (cid,), old, 'synthetic-reviewer', 'Synthetic lifecycle setup')
        current = await propose_claim(workspace, (cid,), 'rollout' if case['id'] == 'superseded-claim' else 'fact',
                                      case['allowed_source'], [ref])
        if case['id'] == 'superseded-claim':
            await supersede_claim(workspace, (cid,), old, current, 'synthetic-reviewer', 'Synthetic lifecycle setup')
        else:
            await approve_claim(workspace, (cid,), current, 'synthetic-reviewer', 'Synthetic lifecycle setup')
    if case.get('condition') == 'indexed snapshot stale':
        async with aiosqlite.connect(os.environ['POTLUCK_MEMORY_DB_PATH']) as db:
            await db.execute("UPDATE source_collections SET last_scan_at='2000-01-01T00:00:00+00:00' WHERE id=?", (cid,))
            await db.commit()
    write_new(root / 'scope.json', {'workspace': workspace, 'collection_ids': list(scope.collection_ids),
              'condition': case.get('condition')})
    return scope


async def attempt(out: Path, case: dict[str, Any], arm: str, runtime: Any, model: str,
                  *, template_version: str | None = None,
                  generation_profile: str | None = None, seed: int | None = None,
                  questions: list[str] | None = None,
                  reviewed_operations: list[dict[str, str]] | None = None,
                  operation_mode: str | None = None) -> dict[str, Any]:
    folder = out / 'attempts' / case['id'] / arm
    folder.mkdir(parents=True)
    write_new(folder / 'started.json', {'case_id': case['id'], 'arm': arm,
              'started_at': datetime.now(timezone.utc).isoformat()})
    started = time.monotonic()
    investigation_started: float | None = None
    output: dict[str, Any] = {k: None for k in ('citation_validity', 'semantic_support',
                                                'unsupported_answer', 'correction', 'time_to_accepted_seconds')}
    with isolated_environment(POTLUCK_MEMORY_DB_PATH=str(folder / 'isolated.sqlite')):
        try:
            await init_memory_db()
            scope = await setup_case(folder, case, arm)
            original_runtime = investigations.get_runtime
            original_lookup = investigations.lookup_evidence

            async def capture_lookup(scope_arg: EvidenceScope, query: str, limit: int) -> Any:
                try:
                    result = await original_lookup(scope_arg, query, limit)
                except Exception as exc:
                    write_new(folder / 'retrieval-error.json', {'type': type(exc).__name__,
                                                               'message': str(exc)})
                    raise
                write_new(folder / 'retrieval.json', {'workspace': scope_arg.workspace,
                    'collection_ids': list(scope_arg.collection_ids), 'query': query,
                    'limit': limit, 'result': result})
                return result

            request_metadata = ({'operation_mode': operation_mode,
                                 'plan_origin': 'fixture_supplied' if reviewed_operations is not None else 'planned_unreviewed'}
                                if operation_mode is not None else None)
            investigations.get_runtime = lambda: CaptureRuntime(runtime, folder, request_metadata)
            investigations.lookup_evidence = capture_lookup
            try:
                investigation_started = time.monotonic()
                kwargs = {} if template_version is None else {'template_version': template_version}
                if generation_profile is not None:
                    kwargs['generation_profile'] = generation_profile
                if seed is not None:
                    kwargs['seed'] = seed
                if questions is not None:
                    kwargs['questions'] = questions
                if reviewed_operations is not None:
                    kwargs['reviewed_operations'] = reviewed_operations
                    kwargs['reviewed_plan_origin'] = 'fixture_supplied'
                item = await investigations.create_investigation(
                    scope, case['question'], case['question'], model, **kwargs)
            finally:
                investigations.get_runtime = original_runtime
                investigations.lookup_evidence = original_lookup
            output.update(status='success', result=item)
        except Exception as exc:
            output.update(status='error', error={'type': type(exc).__name__, 'message': str(exc)},
                          failure_stage=('pre_inference' if not (folder / 'request.json').exists() else 'inference_or_validation'))
        ended = time.monotonic()
        output['latency_seconds'] = ended - investigation_started if investigation_started is not None else None
        output['setup_seconds'] = (investigation_started or ended) - started
        output['attempt_total_seconds'] = ended - started
        write_new(folder / 'result.json', output)
    return output


async def run(out: Path, model_path: Path, expected_sha: str, n_ctx: int, threads: int) -> None:
    manifest = frozen_manifest()
    if not model_path.is_file() or model_path.is_symlink():
        raise ValueError('An existing local model file is required')
    # Hash before creating output or loading; never fetch a model.
    with model_path.open('rb') as stream:
        h = hashlib.file_digest(stream, 'sha256').hexdigest()
    if h != expected_sha.lower():
        raise ValueError('Model file hash mismatch')
    out = out.expanduser().absolute()
    out = out.parent.resolve(strict=True) / out.name
    out.mkdir(mode=0o700, parents=False, exist_ok=False)
    (out / 'data').mkdir(mode=0o700)
    canonical = json.dumps(manifest, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
    config = {'model_sha256': h, 'runtime_version': 'potluck-llama-cpp-python',
              'settings': {'n_ctx': n_ctx, 'threads': threads, 'cpu_only': True, 'n_gpu_layers': 0,
                           'temperature': 0, 'max_tokens': 1600, 'timeout_seconds': 240,
                           'rpc_servers': None}}
    write_new(out / 'manifest.json', manifest)
    write_new(out / 'run-metadata.json', {'manifest_sha256': digest(canonical),
        'fixture_sha256': digest((FIXTURES / 'synthetic_cases.json').read_bytes()),
        'holdout_sha256': digest((FIXTURES / 'holdout.json').read_bytes()),
        'model_path': str(model_path), 'model_config': config, 'code_identity': identity(),
        'comparison': 'raw scoped evidence vs reviewed evidence where supported',
        'started_at': datetime.now(timezone.utc).isoformat()})
    with isolated_environment(POTLUCK_DATA_DIR=str(out / 'data')):
        await _run_loaded(out, model_path, n_ctx, threads, manifest, canonical, config, h)


async def _run_loaded(out: Path, model_path: Path, n_ctx: int, threads: int,
                      manifest: dict[str, Any], canonical: bytes, config: dict[str, Any], h: str) -> None:
    runtime = get_runtime()
    try:
        load_report = runtime.load_model(str(model_path), n_ctx=n_ctx, n_threads=threads,
                                         n_gpu_layers=0, cpu_only=True, rpc_servers=None)
        write_new(out / 'model-load.json', load_report)
        if runtime.current_model_sha256() != h:
            raise ValueError('Loaded model identity mismatch')
        model = runtime.current_model_id()
        outputs: dict[str, dict[str, Any]] = {'baseline': {}, 'candidate': {}}
        ids = [x['id'] for x in manifest['cases']]
        for case in manifest['cases']:
            for arm in ('baseline', 'candidate'):
                outputs[arm][case['id']] = await attempt(out, case, arm, runtime, model)
        for arm in outputs:
            write_new(out / f'{arm}.json', {'corpus_id': manifest['corpus_id'],
                'manifest_sha256': digest(canonical), 'case_ids': ids, 'model_config': config,
                'assessment': None, 'outputs': outputs[arm]})
    except Exception as exc:
        write_new(out / 'run-error.json', {'type': type(exc).__name__, 'message': str(exc)})
        raise
    finally:
        runtime.unload_model()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', required=True, type=Path, help='New directory; parent must exist')
    parser.add_argument('--model-path', required=True, type=Path, help='Existing local GGUF')
    parser.add_argument('--model-sha256', required=True, help='Expected SHA-256 of local GGUF')
    parser.add_argument('--context-size', type=int, required=True)
    parser.add_argument('--threads', type=int, required=True)
    args = parser.parse_args()
    if args.context_size <= 0 or args.threads <= 0:
        parser.error('context size and threads must be positive')
    asyncio.run(run(args.output_dir, args.model_path.expanduser().absolute(), args.model_sha256,
                    args.context_size, args.threads))


if __name__ == '__main__':
    main()
