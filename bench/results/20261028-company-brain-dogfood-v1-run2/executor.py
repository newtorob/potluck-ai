#!/usr/bin/env python3
"""One-off frozen internal dogfood runner. No server, retries, or external inference."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent
REPO = Path('/Users/rob/Documents/potluck')
OUT = REPO / 'bench/results/20261028-company-brain-dogfood-v1'
MODEL = Path('/Users/rob/.potluck/models/qwen-2.5-3b-instruct-q4/model.gguf')
sys.path.insert(0, str(REPO / 'apps/desktop/python'))
MODEL_SHA = '9c9f56a391a3abbd5b89d0245bf6106081bcc3173119d4229235dd9d23253f94'
EXPECTED = {
    'manifest.json': '711eb790c1d5b3376b68e21a34ea8c1c77b208003bdac6ed9ef448aae8f44183',
    'inputs/cases.json': '7a1347320a927658e2e528579d178f4fdba54c17211c2804f41b996476641aaa',
    'inputs/sources/COMPANY_BRAIN.md': 'a01b9eccec5f22a0986cda6568ae3454b957522cec37908fd3dd2f2e75ea8e79',
    'labels/rubric.md': '84b15360be0b5dc81c677c01f53f2de7b24927ad006986e1e03e8f43b0ff74fa',
}

def sha(path):
    with path.open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()

def write(path, obj):
    with path.open('x', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, default=str)
        f.write('\n')
        f.flush()
        os.fsync(f.fileno())

def verify():
    for relative, expected in EXPECTED.items():
        path = ROOT / relative
        if path.is_symlink() or not path.is_file() or sha(path) != expected:
            raise ValueError(f'Frozen input mismatch: {relative}')
    if sorted(p.name for p in (ROOT / 'inputs/sources').iterdir()) != ['COMPANY_BRAIN.md']:
        raise ValueError('Unexpected file in source scope')
    cases = json.loads((ROOT / 'inputs/cases.json').read_text())
    if [c['id'] for c in cases['cases']] != ['CB-01', 'CB-02']:
        raise ValueError('Unexpected cases')
    return cases

def identity():
    engine = REPO / 'apps/desktop/python'
    source = engine / 'potluck_local'
    hashes = {str(p.relative_to(engine)): sha(p) for p in sorted(source.rglob('*.py'))}
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=REPO, text=True).strip()
    if commit != 'ceb24b3901dc163174ace75139f3201c00fcf141':
        raise ValueError('Unexpected repository HEAD')
    import importlib.metadata
    try:
        wheel = importlib.metadata.version('llama-cpp-python')
    except importlib.metadata.PackageNotFoundError:
        wheel = 'unavailable'
    return {'git_head': commit, 'python_source_sha256': hashes, 'python': sys.version,
            'platform': platform.platform(), 'llama_cpp_python': wheel}

class CaptureRuntime:
    def __init__(self, runtime, folder):
        self.runtime, self.folder = runtime, folder
    def __getattr__(self, name):
        return getattr(self.runtime, name)
    async def chat(self, messages, **kwargs):
        write(self.folder / 'request.json', {'messages': messages, 'settings': kwargs})
        try:
            reply = await self.runtime.chat(messages, **kwargs)
        except BaseException as exc:
            write(self.folder / 'runtime-error.json', {'type': type(exc).__name__, 'message': str(exc)})
            raise
        write(self.folder / 'raw-reply.json', reply)
        return reply

async def run():
    # Reserve a new directory first so all subsequent setup failures are retained.
    if OUT.exists():
        raise FileExistsError('Output already exists; no reruns or overwrites')
    OUT.mkdir(mode=0o700, parents=False, exist_ok=False)
    try:
        cases = verify()
        if MODEL.is_symlink() or not MODEL.is_file() or sha(MODEL) != MODEL_SHA:
            raise ValueError('Pinned local model missing, symlinked, or hash mismatch')
        await run_setup_and_cases(cases)
    except BaseException as exc:
        if not (OUT / 'run-error.json').exists():
            write(OUT / 'run-error.json', {'type': type(exc).__name__, 'message': str(exc), 'stage': 'setup_or_run'})
        raise

async def run_setup_and_cases(cases):
    data = OUT / 'data'
    data.mkdir(mode=0o700)
    os.environ['POTLUCK_DATA_DIR'] = str(data)
    os.environ['POTLUCK_MEMORY_DB_PATH'] = str(data / 'memory.db')
    # Imports after isolation environment is set; use the real production service.
    from potluck_local.services import investigations
    from potluck_local.services.evidence_lookup import EvidenceScope
    from potluck_local.services.memory_db import init_memory_db
    from potluck_local.services.runtime import get_runtime
    from potluck_local.services.source_collections import register_collection, scan_collection
    import aiosqlite

    copied = OUT / 'frozen-inputs'
    (copied / 'sources').mkdir(parents=True)
    for src, dest in [('manifest.json', 'manifest.json'), ('inputs/cases.json', 'cases.json'),
                      ('inputs/sources/COMPANY_BRAIN.md', 'sources/COMPANY_BRAIN.md')]:
        shutil.copyfile(ROOT / src, copied / dest)
        if sha(copied / dest) != EXPECTED[src]:
            raise ValueError('Copied input mismatch: ' + src)
    code_identity = identity()
    write(OUT / 'run-metadata.json', {'created_at': datetime.now(timezone.utc).isoformat(),
        'purpose': 'selected internal dogfood; not untouched external holdout',
        'input_sha256': EXPECTED, 'executor_sha256': sha(Path(__file__)), 'code_identity': code_identity,
        'model_sha256': MODEL_SHA, 'model_slug': 'qwen-2.5-3b-instruct-q4',
        'runtime': {'n_ctx': 8192, 'n_gpu_layers': 0, 'n_threads': 8, 'cpu_only': True},
        'human_time_to_accepted_seconds': None, 'agent_review_seconds': None,
        'retry_policy': 'one attempt per case; all failures retained',
        'source_provenance': 'manifest.json; no private vault or personal memory input'})
    runtime = get_runtime()
    try:
        await init_memory_db()
        cid = await register_collection(cases['workspace'], 'selected-repository-source', copied / 'sources')
        await scan_collection(cases['workspace'], cid)
        scope = EvidenceScope(cases['workspace'], (cid,))
        async with aiosqlite.connect(os.environ['POTLUCK_MEMORY_DB_PATH']) as db:
            rows = await (await db.execute('SELECT id,relative_path,revision_id,content_sha FROM source_documents WHERE collection_id=?', (cid,))).fetchall()
        docs = [dict(zip(('document_id', 'relative_path', 'revision_id', 'content_sha'), row)) for row in rows]
        if len(docs) != 1 or docs[0]['content_sha'] != EXPECTED['inputs/sources/COMPANY_BRAIN.md']:
            raise ValueError('Indexed source does not match frozen source')
        write(OUT / 'source-index.json', {'workspace': cases['workspace'], 'collection_id': cid, 'documents': docs})
        report = runtime.load_model(str(MODEL), n_ctx=8192, n_gpu_layers=0, n_threads=8, cpu_only=True)
        write(OUT / 'model-load.json', report)
        if runtime.current_model_sha256() != MODEL_SHA or runtime.current_model_id() != 'qwen-2.5-3b-instruct-q4':
            raise ValueError('Loaded model identity mismatch')
        for case in cases['cases']:
            folder = OUT / 'attempts' / case['id']
            folder.mkdir(parents=True)
            write(folder / 'started.json', {'case_id': case['id'], 'started_at': datetime.now(timezone.utc).isoformat()})
            started = time.monotonic()
            original_runtime, original_lookup = investigations.get_runtime, investigations.lookup_evidence
            async def capture_lookup(*args, **kwargs):
                result = await original_lookup(*args, **kwargs)
                write(folder / 'retrieval.json', result)
                return result
            investigations.get_runtime = lambda: CaptureRuntime(runtime, folder)
            investigations.lookup_evidence = capture_lookup
            try:
                result = await investigations.create_investigation(scope, case['issue'], case['query'], runtime.current_model_id())
                output = {'status': 'success', 'saved_draft_id': result['id'], 'draft': result}
            except Exception as exc:
                output = {'status': 'error', 'saved_draft_id': None,
                          'error': {'type': type(exc).__name__, 'message': str(exc)},
                          'failure_stage': 'inference_or_validation' if (folder / 'request.json').exists() else 'pre_inference'}
            finally:
                investigations.get_runtime, investigations.lookup_evidence = original_runtime, original_lookup
            output.update({'elapsed_seconds': time.monotonic() - started, 'agent_review_seconds': None,
                           'human_time_to_accepted_seconds': None, 'review': None})
            write(folder / 'result.json', output)
    except BaseException as exc:
        write(OUT / 'run-error.json', {'type': type(exc).__name__, 'message': str(exc)})
        raise
    finally:
        runtime.unload_model()

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true', help='Actually load local model and run both cases')
    parser.add_argument('--output-dir', type=Path, default=OUT, help='Exclusive new directory under bench/results')
    parser.add_argument('--check-imports', action='store_true', help='Import production modules in a temporary isolated data directory, without loading a model')
    args = parser.parse_args()
    OUT = args.output_dir.expanduser().absolute()
    if OUT.parent.resolve() != (REPO / 'bench/results').resolve():
        parser.error('output directory must be directly under bench/results')
    if not args.execute:
        verify()
    if args.check_imports:
        import tempfile
        with tempfile.TemporaryDirectory(prefix='company-brain-import-') as tmp:
            os.environ['POTLUCK_DATA_DIR'] = tmp
            os.environ['POTLUCK_MEMORY_DB_PATH'] = str(Path(tmp) / 'memory.db')
            from potluck_local.services import investigations
            from potluck_local.services.runtime import get_runtime
            from potluck_local.services.source_collections import register_collection
        print('Production service imports passed; no model loaded.')
    if not args.execute:
        print('Frozen inputs verified; inference not started. Root review required before --execute.')
    else:
        asyncio.run(run())
