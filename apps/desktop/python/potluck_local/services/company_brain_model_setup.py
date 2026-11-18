"""Explicit local artifact import and session-only Company Brain preparation."""
from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
import platform
import secrets
import shutil
import stat

import psutil

from . import investigation_profiles, local_server
from .model_identity import artifact_identity, file_stamp
from .model_lifecycle import get_model_lifecycle
from .runtime import get_runtime
from ..utils.paths import models_dir

VERSION = 'company-brain-model-setup-v1'
MODEL_ID = 'qwen3.8-27b-iq4-xs'
MODEL_BYTES = 15321491168
CONTEXT = 8192
PROFILE = investigation_profiles.QWEN38_27B_NATIVE_NON_THINKING
# Count every layer as attention and reserve additional runtime/recurrent state.
ESTIMATED_RAM = MODEL_BYTES + CONTEXT * 65 * 4 * (256 + 256) * 2 + 4 * 1024 ** 3
DISK_MARGIN = 256 * 1024 ** 2
_verified_stamp = None


class PilotSetupError(ValueError):
    """A public, static recovery message without a local path or native output."""


def _profile():
    return investigation_profiles.resolve_profile(PROFILE)


def _path() -> Path:
    base = models_dir().resolve()
    directory = base / MODEL_ID
    if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
        raise PilotSetupError('Pilot model storage is occupied by an incompatible path. No file was overwritten.')
    target = directory / 'model.gguf'
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise PilotSetupError('Pilot model storage is occupied by an incompatible file. No file was overwritten.')
    if directory.resolve().parent != base or target.resolve().parent != directory.resolve():
        raise PilotSetupError('Pilot model storage must stay inside the local model directory.')
    return target


def _verify(path: Path) -> None:
    global _verified_stamp
    digest, stamp = artifact_identity(path)
    if stamp[2] != MODEL_BYTES or digest != _profile().model_sha256:
        raise PilotSetupError('This file does not match the supplied pilot model. The existing model was not overwritten.')
    _verified_stamp = stamp


def _preflight(*, verify: bool = False) -> dict:
    issues = []
    def issue(code, message, *blocks):
        issues.append({'code': code, 'message': message, 'blocks': list(blocks)})
    supported = (platform.system(), platform.machine()) in {
        ('Linux', 'x86_64'), ('Darwin', 'arm64'),
    }
    if not supported:
        issue('platform_unsupported', 'This pilot requires an Apple Silicon macOS or x86-64 Linux build with the native engine. Source and saved-answer review remain available.', 'import', 'prepare')
    try:
        _, engine = local_server.bundle()
        if engine['commit'] != _profile().engine_commit:
            raise ValueError()
    except (local_server.SplitError, OSError, ValueError, KeyError):
        issue('engine_missing', 'This build is missing the compatible native engine. Use a Company Brain pilot build before preparing the model.', 'prepare')
    try:
        available = psutil.virtual_memory().available
        if type(available) is not int or available < 0:
            raise ValueError()
    except (OSError, RuntimeError, ValueError, AttributeError):
        available = None
    if available is None:
        issue('ram_unknown', 'Available memory could not be checked. Close other workloads and check setup again.', 'prepare')
    elif available < ESTIMATED_RAM:
        issue('ram_insufficient', 'Not enough available memory for this pilot model. Source and saved-answer review still work without it.', 'prepare')
    installed = verified = False
    required_disk = MODEL_BYTES + DISK_MARGIN
    free = None
    try:
        target = _path()
        installed = target.is_file()
        if installed:
            required_disk = 0
            if target.stat().st_size != MODEL_BYTES:
                issue('artifact_mismatch', 'A different file occupies pilot model storage. It will not be overwritten.', 'import', 'prepare')
            else:
                if verify:
                    _verify(target)
                verified = _verified_stamp is not None and file_stamp(target) == _verified_stamp
        else:
            issue('artifact_missing', 'Choose the supplied pilot GGUF file, then import it into Potluck.', 'prepare')
        existing = target.parent
        while not existing.exists():
            existing = existing.parent
        free = shutil.disk_usage(existing).free
        if free < required_disk:
            issue('disk_insufficient', 'Not enough local disk space to copy the pilot model.', 'import')
    except PilotSetupError as exc:
        issue('artifact_mismatch', str(exc), 'import', 'prepare')
    except (OSError, ValueError):
        issue('artifact_unreadable', 'Pilot model storage could not be checked safely. Check file access and try again.', 'import', 'prepare')
    return {
        'version': VERSION,
        'model': {'id': MODEL_ID, 'name': 'Company Brain pilot · Qwen3.8 27B IQ4_XS',
                  'sha256': _profile().model_sha256, 'size_bytes': MODEL_BYTES,
                  'context_tokens': CONTEXT, 'backend': 'llama-server'},
        'platform_supported': supported, 'installed': installed, 'artifact_verified': verified,
        'ready': False, 'can_import': not any('import' in x['blocks'] for x in issues),
        'can_prepare': not any('prepare' in x['blocks'] for x in issues),
        'can_unload': False, 'active_model': None, 'issues': issues,
        'resources': {'estimated_ram_bytes': ESTIMATED_RAM, 'available_ram_bytes': available,
                      'required_disk_bytes': required_disk, 'disk_free_bytes': free},
        'warnings': ['The supplied model is separate from the desktop installer. No model is downloaded automatically.',
                     'Memory requirements are conservative estimates, not speed or answer-quality guarantees. Preparation uses CPU inference for this session.'],
    }


async def status() -> dict:
    report = await asyncio.to_thread(_preflight)
    runtime = get_runtime()
    report['active_model'] = runtime.current_model_id()
    report['can_unload'] = runtime.is_loaded() and report['active_model'] == MODEL_ID
    if (report['can_unload'] and runtime.current_model_sha256() == _profile().model_sha256
            and runtime.loaded_n_ctx() == CONTEXT):
        try:
            await runtime.validate_investigation_profile(
                PROFILE, expected_model=MODEL_ID, expected_sha256=_profile().model_sha256)
        except (ValueError, RuntimeError, OSError):
            report['warnings'].append('The loaded model has not passed the pilot profile check. Prepare it again before asking.')
        else:
            report['ready'] = True
            report['can_prepare'] = False
            report['artifact_verified'] = True
            # Its allocation already occupies RAM; free memory is a new-load
            # prerequisite, not evidence that the running verified model failed.
            report['issues'] = [x for x in report['issues'] if x['code'] not in ('ram_unknown', 'ram_insufficient')]
    return report


def _require(report, action):
    if not report['can_' + action]:
        raise PilotSetupError(' '.join(x['message'] for x in report['issues'] if action in x['blocks']))


async def _settle(operation):
    # A disconnected caller cannot release the lifecycle gate over a live copy/load.
    task = asyncio.ensure_future(operation)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled():
            task.exception()
        raise


def _copy_import(source_path: str) -> None:
    source = Path(source_path)
    if (not source.is_absolute() or source.suffix.lower() != '.gguf'
            or source.is_symlink() or source.resolve() != source):
        raise PilotSetupError('Choose a real local GGUF file without symbolic links.')
    target = _path()
    if target.exists():
        _verify(target)
        return
    report = _preflight()
    _require(report, 'import')
    source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    directory_fd = output_fd = None
    temporary = '.pilot-import-' + secrets.token_hex(16) + '.partial'
    try:
        initial = os.fstat(source_fd)
        if not stat.S_ISREG(initial.st_mode) or initial.st_size != MODEL_BYTES:
            raise PilotSetupError('The selected file has the wrong size or is not a regular pilot GGUF file.')
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _path()
        directory_fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        output_fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                            0o600, dir_fd=directory_fd)
        digest = hashlib.sha256()
        with os.fdopen(source_fd, 'rb') as incoming, os.fdopen(output_fd, 'wb') as outgoing:
            source_fd = output_fd = None
            first = incoming.read(4)
            if first != b'GGUF':
                raise PilotSetupError('The selected file is not a GGUF model.')
            digest.update(first); outgoing.write(first)
            copied = 4
            while chunk := incoming.read(1024 * 1024):
                copied += len(chunk)
                if copied > MODEL_BYTES:
                    raise PilotSetupError('The selected model changed while it was being copied.')
                digest.update(chunk); outgoing.write(chunk)
            final = os.fstat(incoming.fileno())
            if (copied != MODEL_BYTES or digest.hexdigest() != _profile().model_sha256
                    or (initial.st_dev, initial.st_ino, initial.st_size, initial.st_mtime_ns)
                    != (final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns)):
                raise PilotSetupError('The selected file does not match the supplied pilot model. Nothing was installed.')
            outgoing.flush(); os.fsync(outgoing.fileno())
        current = target.parent.stat(); opened = os.fstat(directory_fd)
        if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
            raise PilotSetupError('Pilot model storage changed while copying. Nothing was installed.')
        _path()
        os.link(temporary, 'model.gguf', src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd, follow_symlinks=False)
        os.fsync(directory_fd)
    finally:
        if source_fd is not None:
            os.close(source_fd)
        if output_fd is not None:
            os.close(output_fd)
        if directory_fd is not None:
            try:
                os.unlink(temporary, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
            os.close(directory_fd)
    # Removing the temporary hard link changes ctime. Cache only the final file
    # identity, after cleanup, so a successful import reports verified readiness.
    _verify(target)


async def import_model(source_path: str) -> dict:
    async with get_model_lifecycle().command():
        try:
            await _settle(asyncio.to_thread(_copy_import, source_path))
        except PilotSetupError:
            raise
        except (OSError, ValueError):
            raise PilotSetupError('The pilot model could not be copied safely. Check access and disk space; no existing model was overwritten.') from None
    return await status()


def _expect_active(runtime, expected):
    if runtime.current_model_id() != expected:
        raise PilotSetupError('The active model changed. Check setup and confirm the replacement again.')


async def prepare(expected_active_model: str | None) -> dict:
    async with get_model_lifecycle().command():
        runtime = get_runtime(); _expect_active(runtime, expected_active_model)
        report = await asyncio.to_thread(_preflight, verify=True); _require(report, 'prepare')
        _expect_active(runtime, expected_active_model)
        async def load():
            loaded = False
            try:
                await runtime.load_model_async(str(_path()), n_ctx=CONTEXT, n_gpu_layers=0,
                    n_threads=8, backend='llama-server', native_cache_mib=0,
                    expected_model_sha256=_profile().model_sha256,
                    expected_engine_acceleration='cpu')
                loaded = True
                if runtime.loaded_n_ctx() != CONTEXT:
                    raise ValueError('Unexpected context')
                await runtime.validate_investigation_profile(PROFILE,
                    expected_model=MODEL_ID, expected_sha256=_profile().model_sha256)
            except Exception:
                if loaded and runtime.current_model_id() == MODEL_ID:
                    await runtime.unload_model_async()
                    raise PilotSetupError('Pilot model preparation failed native verification. The attempted model was stopped; open Models or check setup to continue.') from None
                raise PilotSetupError('Pilot model preparation failed. The previous model may have been replaced; refresh setup and check Models before continuing.') from None
        await _settle(load())
    return await status()


async def unload(expected_active_model: str | None) -> dict:
    async with get_model_lifecycle().command():
        runtime = get_runtime(); _expect_active(runtime, expected_active_model)
        if expected_active_model != MODEL_ID:
            raise PilotSetupError('The pilot model is not active. Another model was left unchanged.')
        await _settle(runtime.unload_model_async())
    return await status()
