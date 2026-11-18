"""Build-time guard for opt-in, self-contained Company Brain pilot bundles."""
import hashlib
import json
import os
from pathlib import Path
import platform
import struct
import subprocess


def validate(root: Path, pin: dict) -> dict:
    system, machine = platform.system(), platform.machine()
    if (system, machine) not in {('Linux', 'x86_64'), ('Darwin', 'arm64')}:
        raise ValueError('Pilot bundles require native Linux x86-64 or macOS arm64 builds.')
    manifest = json.loads((root / 'manifest.json').read_text())
    if any(manifest.get(k) != pin[k] for k in ('schemaVersion', 'protocol', 'rpcProtocol', 'commit')):
        raise ValueError('Pilot native engine does not match the packaged pin.')
    expected = 'cpu' if system == 'Linux' else 'metal'
    if manifest.get('backend') != expected:
        raise ValueError(f'Pilot bundle requires the {expected} engine; no external GPU runtime dependency.')
    hashes = {}
    for name in ('manifest.json', 'LICENSE', 'llama-server', 'rpc-server'):
        path = root / name
        if path.is_symlink() or not path.is_file() or not path.stat().st_size:
            raise ValueError(f'Missing regular pilot resource: {name}')
        if name.endswith('server'):
            if not os.access(path, os.X_OK):
                raise ValueError(f'Pilot engine is not executable: {name}')
            header = path.read_bytes()[:32]
            if system == 'Linux':
                if len(header) < 20 or header[:6] != b'\x7fELF\x02\x01' or struct.unpack('<H', header[18:20])[0] != 62:
                    raise ValueError('Pilot engine must be a native x86-64 ELF binary.')
                linked = subprocess.run(['ldd', str(path)], capture_output=True, text=True, timeout=15)
                if 'not found' in linked.stdout + linked.stderr or (linked.returncode and 'not a dynamic executable' not in linked.stderr + linked.stdout):
                    raise ValueError('Pilot native engine has unresolved dependencies.')
            else:
                arches = subprocess.check_output(['lipo', '-archs', str(path)], text=True, timeout=15).split()
                if 'arm64' not in arches:
                    raise ValueError('Pilot engine must support arm64.')
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    # Run only the harmless version command, never a model or inference probe.
    env = {k: v for k, v in os.environ.items() if not k.startswith(('LLAMA_', 'GGML_', 'LD_', 'DYLD_'))}
    subprocess.run([str(root / 'llama-server'), '--version'], env=env, check=True,
                   capture_output=True, timeout=15)
    subprocess.run([str(root / 'rpc-server'), '--help'], env=env, check=True,
                   capture_output=True, timeout=15)
    return {'version': 'company-brain-pilot-build-v1', 'platform': system, 'machine': machine,
            'backend': expected, 'engine_commit': pin['commit'], 'files': hashes,
            'model_weights_included': False}
