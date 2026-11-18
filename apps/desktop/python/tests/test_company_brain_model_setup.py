"""Local-only setup guards with tiny fixtures; no model loading or downloads."""
import asyncio
import hashlib
import os
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from fastapi import HTTPException

from potluck_local.services import company_brain_model_setup as setup
from potluck_local.routers import company_brain_pilot as router

ARTIFACT = b'GGUF tiny pilot model fixture'

@pytest.fixture
def host(monkeypatch, tmp_path):
    model_dir = tmp_path / 'models'
    state = {'ram': 10000, 'disk': 10000, 'system': 'Linux', 'machine': 'x86_64', 'engine': True}
    profile = SimpleNamespace(model_sha256=hashlib.sha256(ARTIFACT).hexdigest(), engine_commit='engine')
    monkeypatch.setattr(setup, 'models_dir', lambda: model_dir)
    monkeypatch.setattr(setup, 'MODEL_BYTES', len(ARTIFACT))
    monkeypatch.setattr(setup, 'ESTIMATED_RAM', 100)
    monkeypatch.setattr(setup, 'DISK_MARGIN', 10)
    monkeypatch.setattr(setup, '_profile', lambda: profile)
    monkeypatch.setattr(setup, '_verified_stamp', None)
    monkeypatch.setattr(setup.platform, 'system', lambda: state['system'])
    monkeypatch.setattr(setup.platform, 'machine', lambda: state['machine'])
    monkeypatch.setattr(setup.psutil, 'virtual_memory', lambda: SimpleNamespace(available=state['ram']))
    monkeypatch.setattr(setup.shutil, 'disk_usage', lambda _: SimpleNamespace(free=state['disk']))
    def bundle():
        if not state['engine']:
            raise OSError('missing')
        return tmp_path, {'commit': 'engine'}
    monkeypatch.setattr(setup.local_server, 'bundle', bundle)
    return tmp_path, state, profile


def codes(report):
    return {i['code'] for i in report['issues']}


def source(tmp, contents=ARTIFACT):
    path = tmp / 'input.gguf'
    path.write_bytes(contents)
    return path


def test_preflight_checks_platform_engine_memory_and_disk(host):
    _, state, _ = host
    assert 'artifact_missing' in codes(setup._preflight())
    state.update(system='Windows', engine=False, ram=1, disk=1)
    report = setup._preflight()
    assert {'platform_unsupported', 'engine_missing', 'ram_insufficient', 'disk_insufficient'} <= codes(report)
    assert not report['can_import'] and not report['can_prepare']


def test_import_copies_exact_file_and_never_overwrites_existing_mismatch(host):
    tmp, _, _ = host
    path = source(tmp)
    setup._copy_import(str(path))
    target = setup._path()
    assert target.read_bytes() == ARTIFACT
    assert setup._preflight()['artifact_verified']
    target.write_bytes(b'other')
    with pytest.raises(setup.PilotSetupError):
        setup._copy_import(str(path))
    assert target.read_bytes() == b'other'
    assert not list(target.parent.glob('.pilot-import-*.partial'))


@pytest.mark.parametrize('contents', [b'GGUF incorrect model fixture', b'NOPE tiny pilot model fixture', b'GGUF short'])
def test_invalid_import_does_not_leave_target_or_temp(host, contents):
    tmp, _, _ = host
    with pytest.raises(setup.PilotSetupError):
        setup._copy_import(str(source(tmp, contents)))
    target = setup._path()
    assert not target.exists()
    assert not list(target.parent.glob('.pilot-import-*.partial'))


def test_rejects_symlink_and_nonregular_input(host):
    tmp, _, _ = host
    src = source(tmp)
    link = tmp / 'link.gguf'
    link.symlink_to(src)
    with pytest.raises(setup.PilotSetupError):
        setup._copy_import(str(link))
    fifo = tmp / 'pipe.gguf'
    os.mkfifo(fifo)
    with pytest.raises(setup.PilotSetupError):
        setup._copy_import(str(fifo))
    assert not setup._path().exists()


def test_rejects_destination_symlink(host):
    tmp, _, _ = host
    outside = tmp / 'outside'; outside.mkdir()
    root = tmp / 'models'; root.mkdir()
    (root / setup.MODEL_ID).symlink_to(outside, target_is_directory=True)
    with pytest.raises(setup.PilotSetupError):
        setup._copy_import(str(source(tmp)))
    assert not list(outside.iterdir())


class Lifecycle:
    @asynccontextmanager
    async def command(self):
        yield

class Runtime:
    def __init__(self, active):
        self.active = active
        self.load_model_async = AsyncMock(side_effect=self._load)
        self.unload_model_async = AsyncMock(side_effect=self._unload)
        self.validate_investigation_profile = AsyncMock()
    def current_model_id(self): return self.active
    def current_model_sha256(self): return setup._profile().model_sha256
    def loaded_n_ctx(self): return setup.CONTEXT
    def is_loaded(self): return self.active is not None
    async def _load(self, *args, **kwargs): self.active = setup.MODEL_ID
    async def _unload(self): self.active = None


def test_prepare_pins_runtime_and_profile_and_unloads_only_pilot(host, monkeypatch):
    tmp, _, profile = host
    setup._copy_import(str(source(tmp)))
    runtime = Runtime('old')
    monkeypatch.setattr(setup, 'get_runtime', lambda: runtime)
    monkeypatch.setattr(setup, 'get_model_lifecycle', lambda: Lifecycle())
    result = asyncio.run(setup.prepare('old'))
    assert result['ready']
    runtime.load_model_async.assert_awaited_once_with(str(setup._path()), n_ctx=8192, n_gpu_layers=0, n_threads=8, backend='llama-server', native_cache_mib=0, expected_model_sha256=profile.model_sha256, expected_engine_acceleration='cpu')
    assert runtime.validate_investigation_profile.await_count >= 1
    runtime.validate_investigation_profile.assert_any_await(setup.PROFILE, expected_model=setup.MODEL_ID, expected_sha256=profile.model_sha256)
    asyncio.run(setup.unload(setup.MODEL_ID))
    runtime.unload_model_async.assert_awaited_once()


def test_active_model_guard_and_wrong_unload_preserve_model(host, monkeypatch):
    runtime = Runtime('other')
    monkeypatch.setattr(setup, 'get_runtime', lambda: runtime)
    monkeypatch.setattr(setup, 'get_model_lifecycle', lambda: Lifecycle())
    with pytest.raises(setup.PilotSetupError): asyncio.run(setup.prepare('stale'))
    with pytest.raises(setup.PilotSetupError): asyncio.run(setup.unload('other'))
    runtime.load_model_async.assert_not_awaited()
    runtime.unload_model_async.assert_not_awaited()
    assert runtime.active == 'other'


def test_failed_profile_validation_stops_attempted_model(host, monkeypatch):
    tmp, _, _ = host
    setup._copy_import(str(source(tmp)))
    runtime = Runtime(None)
    runtime.validate_investigation_profile.side_effect = ValueError('bad renderer')
    monkeypatch.setattr(setup, 'get_runtime', lambda: runtime)
    monkeypatch.setattr(setup, 'get_model_lifecycle', lambda: Lifecycle())
    with pytest.raises(setup.PilotSetupError): asyncio.run(setup.prepare(None))
    runtime.unload_model_async.assert_awaited_once()
    assert runtime.active is None


def test_router_strict_inputs_and_confirmation(monkeypatch):
    with pytest.raises(ValidationError): router.ModelImport(source_path='/a', import_confirmed='true')
    with pytest.raises(ValidationError): router.ModelPrepare(prepare_confirmed=True, expected_active_model=None, extra='x')
    with pytest.raises(ValidationError): router.ModelUnload(expected_active_model=42)
    import_mock = AsyncMock()
    prepare_mock = AsyncMock()
    monkeypatch.setattr(setup, 'import_model', import_mock)
    monkeypatch.setattr(setup, 'prepare', prepare_mock)
    with pytest.raises(HTTPException) as exc: asyncio.run(router.import_pilot_model(router.ModelImport(source_path='/a', import_confirmed=False)))
    assert exc.value.status_code == 400
    with pytest.raises(HTTPException) as exc: asyncio.run(router.prepare_pilot_model(router.ModelPrepare(prepare_confirmed=False, expected_active_model=None)))
    assert exc.value.status_code == 400
    import_mock.assert_not_awaited(); prepare_mock.assert_not_awaited()


def test_ready_running_model_not_marked_insufficient_due_to_own_ram(host, monkeypatch):
    tmp, state, _ = host
    setup._copy_import(str(source(tmp)))
    state['ram'] = 1
    runtime = Runtime(setup.MODEL_ID)
    monkeypatch.setattr(setup, 'get_runtime', lambda: runtime)
    result = asyncio.run(setup.status())
    assert result['ready'] and result['can_unload']
    assert 'ram_insufficient' not in codes(result)
    runtime.validate_investigation_profile.assert_awaited_once()


def test_early_load_failure_preserves_prior_same_id_model(host, monkeypatch):
    tmp, _, _ = host
    setup._copy_import(str(source(tmp)))
    runtime = Runtime(setup.MODEL_ID)
    runtime.load_model_async.side_effect = ValueError('preload check failed')
    monkeypatch.setattr(setup, 'get_runtime', lambda: runtime)
    monkeypatch.setattr(setup, 'get_model_lifecycle', lambda: Lifecycle())
    with pytest.raises(setup.PilotSetupError):
        asyncio.run(setup.prepare(setup.MODEL_ID))
    runtime.unload_model_async.assert_not_awaited()
    assert runtime.active == setup.MODEL_ID


def test_cancelled_prepare_holds_lifecycle_until_load_settles(host, monkeypatch):
    tmp, _, _ = host
    setup._copy_import(str(source(tmp)))
    runtime = Runtime(None)
    state = {'held': False}
    class Guard:
        @asynccontextmanager
        async def command(self):
            assert not state['held']
            state['held'] = True
            try:
                yield
            finally:
                state['held'] = False
    monkeypatch.setattr(setup, 'get_runtime', lambda: runtime)
    monkeypatch.setattr(setup, 'get_model_lifecycle', lambda: Guard())
    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow_load(*args, **kwargs):
            entered.set()
            await release.wait()
            runtime.active = setup.MODEL_ID
        runtime.load_model_async.side_effect = slow_load
        task = asyncio.create_task(setup.prepare(None))
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert state['held'] and not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not state['held']
        runtime.validate_investigation_profile.assert_awaited_once()
    asyncio.run(exercise())


def test_setup_http_auth_and_explicit_confirmation(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from potluck_local.app import create_app
    monkeypatch.setenv('POTLUCK_SIDECAR_TOKEN','setup-test-secret')
    monkeypatch.setenv('POTLUCK_DATA_DIR',str(tmp_path/'data'))
    check=AsyncMock(return_value={'ready':False})
    monkeypatch.setattr(setup,'status',check)
    with TestClient(create_app(),client=('127.0.0.1',12345)) as client:
        path='/company-brain/pilot/model-setup'
        assert client.get(path).status_code==401
        headers={'Authorization':'Bearer setup-test-secret'}
        assert client.get(path,headers=headers).json()=={'ready':False}
        assert client.post(path+'/prepare',json={'prepare_confirmed':False,'expected_active_model':None},headers=headers).status_code==400
        assert client.post(path+'/prepare',json={'prepare_confirmed':'true','expected_active_model':None},headers=headers).status_code==422
    check.assert_awaited_once()
