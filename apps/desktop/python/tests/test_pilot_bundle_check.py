"""Pilot build refusals happen before producing a misleading installer."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'pilot_bundle_check.py'

@pytest.fixture
def bundle(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('pilot_bundle_check', SCRIPT)
    check = importlib.util.module_from_spec(spec); spec.loader.exec_module(check)
    monkeypatch.setattr(check.platform, 'system', lambda: 'Linux')
    monkeypatch.setattr(check.platform, 'machine', lambda: 'x86_64')
    pin = dict(schemaVersion=1, protocol=1, rpcProtocol='8.0.0', commit='exact')
    (tmp_path/'manifest.json').write_text(json.dumps({**pin, 'backend':'cpu'}))
    (tmp_path/'LICENSE').write_text('license')
    elf = b'\x7fELF\x02\x01' + b'\0'*12 + b'\x3e\x00' + b'\0'*12
    for name in ['llama-server', 'rpc-server']:
        f=tmp_path/name;f.write_bytes(elf);f.chmod(0o700)
    calls=[]
    def run(argv, **kw):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout='all present', stderr='')
    monkeypatch.setattr(check.subprocess, 'run', run)
    return check,tmp_path,pin,calls


def test_valid_bundle_records_exact_resources_and_never_runs_model(bundle):
    check,root,pin,calls=bundle
    receipt=check.validate(root,pin)
    assert receipt['backend']=='cpu' and not receipt['model_weights_included']
    assert set(receipt['files'])=={'manifest.json','LICENSE','llama-server','rpc-server'}
    assert calls[-2:]==[[str(root/'llama-server'),'--version'], [str(root/'rpc-server'),'--help']]
    assert all('-m' not in call for call in calls)

@pytest.mark.parametrize('fault', ['pin','cuda','license','symlink','architecture','dependencies'])
def test_incomplete_or_host_dependent_bundle_refused(bundle,monkeypatch,fault):
    check,root,pin,_=bundle
    if fault in ('pin','cuda'):
        m=json.loads((root/'manifest.json').read_text());m['commit' if fault=='pin' else 'backend']='wrong' if fault=='pin' else 'cuda';(root/'manifest.json').write_text(json.dumps(m))
    elif fault=='license':(root/'LICENSE').unlink()
    elif fault=='symlink':
        f=root/'rpc-server';f.rename(root/'real');f.symlink_to(root/'real')
    elif fault=='architecture':(root/'llama-server').write_bytes(b'wrong architecture')
    else:monkeypatch.setattr(check.subprocess,'run',lambda *a,**k:SimpleNamespace(returncode=0,stdout='libx => not found',stderr=''))
    with pytest.raises((ValueError,OSError)):check.validate(root,pin)


def test_macos_rpc_dependency_failure_cannot_bless_bundle(bundle, monkeypatch):
    import subprocess
    check,root,pin,_=bundle
    monkeypatch.setattr(check.platform,'system',lambda:'Darwin')
    monkeypatch.setattr(check.platform,'machine',lambda:'arm64')
    (root/'manifest.json').write_text(json.dumps({**pin,'backend':'metal'}))
    monkeypatch.setattr(check.subprocess,'check_output',lambda *a,**k:'arm64')
    def probe(argv,**kw):
        if argv[0].endswith('/rpc-server'):
            raise subprocess.CalledProcessError(1,argv)
        return SimpleNamespace(returncode=0,stdout='version',stderr='')
    monkeypatch.setattr(check.subprocess,'run',probe)
    with pytest.raises(subprocess.CalledProcessError):check.validate(root,pin)


@pytest.mark.parametrize('dependency', ['/opt/homebrew/lib/libprivate.dylib', '@rpath/libprivate.dylib'])
def test_macos_build_machine_library_is_refused(bundle, monkeypatch, dependency):
    check,root,pin,_=bundle
    monkeypatch.setattr(check.platform,'system',lambda:'Darwin')
    monkeypatch.setattr(check.platform,'machine',lambda:'arm64')
    (root/'manifest.json').write_text(json.dumps({**pin,'backend':'metal'}))
    monkeypatch.setattr(check.subprocess,'check_output',lambda argv,**kw:
                        'arm64' if argv[0]=='lipo' else f'{argv[-1]}:\n\t{dependency} (compatibility version 1.0.0)\n')
    with pytest.raises(ValueError, match='build-machine'):check.validate(root,pin)


def test_macos_freeze_keeps_exact_engine_identity_even_when_replacement_passes_probes(bundle, monkeypatch):
    check,root,pin,calls=bundle
    monkeypatch.setattr(check.platform,'system',lambda:'Darwin')
    monkeypatch.setattr(check.platform,'machine',lambda:'arm64')
    (root/'manifest.json').write_text(json.dumps({**pin,'backend':'metal'}))
    monkeypatch.setattr(check.subprocess,'check_output',lambda argv,**kw:
                        'arm64' if argv[0]=='lipo' else f'{argv[-1]}:\n\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0)\n')
    original=check.validate(root,pin)
    frozen=check.finalize(root,pin,original)
    assert frozen['build_input_files']==original['files']
    assert frozen['files']==original['files']
    assert len([call for call in calls if '--version' in call])==2
    (root/'llama-server').write_bytes(b'different executable that passes mocked arm64/system-dependency/probes')
    with pytest.raises(ValueError, match='validated build input'):check.finalize(root,pin,original)
    assert len([call for call in calls if '--version' in call])==2
    (root/'llama-server').write_bytes(b'\x7fELF\x02\x01' + b'\0'*12 + b'\x3e\x00' + b'\0'*12)
    (root/'LICENSE').write_text('changed license')
    with pytest.raises(ValueError, match='validated build input'):check.finalize(root,pin,original)


def test_linux_freeze_cannot_silently_replace_a_valid_executable(bundle):
    check,root,pin,_=bundle
    original=check.validate(root,pin)
    with (root/'llama-server').open('ab') as stream:stream.write(b'changed')
    with pytest.raises(ValueError, match='validated build input'):check.finalize(root,pin,original)
