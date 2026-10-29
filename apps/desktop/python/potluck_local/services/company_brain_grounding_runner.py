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
from potluck_local.services import investigation_answers, investigation_profiles
from potluck_local.services.runtime import get_runtime

FIXTURES = base.FIXTURES / 'grounding_v2'


def read_suite(suite: str, fixture_dir: Path | None = None) -> tuple[dict[str, Any], dict[str, Any], dict[str, bytes]]:
    if suite not in ('development', 'fresh'):
        raise ValueError('Invalid suite')
    fixture_dir = fixture_dir if fixture_dir is not None else FIXTURES
    files = {}
    for name in (suite, 'rubric'):
        data = (fixture_dir / f'{name}.json').read_bytes()
        checksum = (fixture_dir / f'{name}.sha256').read_bytes()
        expected = checksum.decode('utf-8').split()[0]
        if base.digest(data) != expected:
            raise ValueError(f'Frozen fixture hash mismatch: {name}')
        files[f'{name}.json'] = data
        files[f'{name}.sha256'] = checksum
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
              n_ctx: int, threads: int, fixture_dir: Path | None = None,
              template_version: str = 'investigation-v4', *,
              generation_profile: str | None = None, seed: int | None = None,
              question_mode: str = 'auto') -> None:
    if template_version not in ('investigation-v4', 'investigation-v5', 'investigation-v6'):
        raise ValueError('Invalid investigation template version')
    if question_mode not in ('auto', 'explicit'):
        raise ValueError('Invalid question mode')
    if question_mode == 'explicit' and template_version != 'investigation-v6':
        raise ValueError('Explicit questions require investigation-v6')
    if generation_profile is not None and template_version != 'investigation-v6':
        raise ValueError('Generation profiles require investigation-v6')
    profile = (investigation_profiles.resolve_profile(generation_profile).metadata()
               if generation_profile is not None else None)
    if seed is not None and (type(seed) is not int or not 0 <= seed <= 4294967295):
        raise ValueError('Seed must be an integer between 0 and 4294967295')
    manifest, _, files = read_suite(suite, fixture_dir)
    if question_mode == 'explicit':
        for case in manifest['cases']:
            try:
                investigation_answers.questions_from_texts(case.get('questions'))
            except ValueError as exc:
                raise ValueError('Invalid explicit fixture questions') from exc
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
    (out / '.gitignore').write_text('/data/\n**/*.sqlite*\n', encoding='utf-8')
    (out / 'data').mkdir(mode=0o700)
    for name, data in files.items():
        with (out / name).open('xb') as stream:
            stream.write(data)
    settings = {'n_ctx': n_ctx, 'n_threads': threads, 'n_gpu_layers': 0,
                'cpu_only': True, 'rpc_servers': None}
    base.write_new(out / 'run-metadata.json', {
        'suite': suite, 'corpus_id': manifest['corpus_id'],
        'template_version': template_version,
        'generation_profile': profile, 'seed': seed, 'question_mode': question_mode,
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
                options = {'template_version': template_version}
                if generation_profile is not None:
                    options['generation_profile'] = generation_profile
                if seed is not None:
                    options['seed'] = seed
                if question_mode == 'explicit':
                    options['questions'] = case['questions']
                outputs[case['id']] = await base.attempt(
                    out, case, 'raw', runtime, model, **options)
            base.write_new(out / 'raw.json', {'corpus_id': manifest['corpus_id'],
                'suite_sha256': base.digest(files[f'{suite}.json']),
                'case_ids': [case['id'] for case in manifest['cases']],
                'template_version': template_version, 'generation_profile': profile,
                'seed': seed, 'question_mode': question_mode, 'assessment': None, 'outputs': outputs})
        except Exception as exc:
            base.write_new(out / 'run-error.json', {'type': type(exc).__name__, 'message': str(exc)})
            raise
        finally:
            runtime.unload_model()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite', choices=('development', 'fresh'), required=True)
    parser.add_argument('--fixture-dir', type=Path, default=None,
                        help='Frozen fixture directory (defaults to grounding_v2)')
    parser.add_argument('--template-version', choices=('investigation-v4', 'investigation-v5', 'investigation-v6'),
                        default='investigation-v4')
    parser.add_argument('--generation-profile', default=None)
    parser.add_argument('--seed', type=int, default=None)
    parser.add_argument('--question-mode', choices=('auto', 'explicit'), default='auto')
    parser.add_argument('--model-path', type=Path, required=True)
    parser.add_argument('--model-sha256', required=True)
    parser.add_argument('--context-size', type=int, required=True)
    parser.add_argument('--threads', type=int, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(run(args.output_dir, args.suite, args.model_path.expanduser().absolute(),
                    args.model_sha256, args.context_size, args.threads,
                    args.fixture_dir.expanduser().absolute() if args.fixture_dir else None,
                    args.template_version, generation_profile=args.generation_profile,
                    seed=args.seed, question_mode=args.question_mode))


if __name__ == '__main__':
    main()
