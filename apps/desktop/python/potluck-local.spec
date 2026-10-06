# -*- mode: python ; coding: utf-8 -*-
import importlib.util
from pathlib import Path

from PyInstaller.utils.hooks import collect_all
from pathlib import Path

datas = [('potluck_local/data/models.json', 'potluck_local/data')]
datas += [('potluck_local/data/split-engine.json', 'potluck_local/data')]
binaries = []
split_bundle = Path(SPECPATH) / 'native' / 'split'
if split_bundle.is_dir():
    datas += [(str(split_bundle / 'manifest.json'), 'split-engine'),
              (str(split_bundle / 'LICENSE'), 'split-engine')]
    binaries += [(str(split_bundle / name), 'split-engine') for name in ('llama-server', 'rpc-server')]
hiddenimports = [
    'aiosqlite',
    'aiosqlite.core',
    # SciPy 1.17 Cython extensions import this from native code; static analysis
    # misses it, breaking sentence-transformers in an otherwise healthy bundle.
    'scipy._cyutility',
    'uvicorn.logging',
    'uvicorn.loops.auto',
    'uvicorn.protocols.http.auto',
    'uvicorn.protocols.http.h11_impl',
    'uvicorn.protocols.websockets.auto',
    'uvicorn.lifespan.on',
    'fastapi',
    'pydantic',
    'pydantic.v1',
    'aiofiles',
]

# llama_cpp ships compiled C++ extensions + Metal/CUDA dylibs — collect-all is required.
tmp_ret = collect_all('llama_cpp')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]

# Extra backend sets installed by scripts/vendor_gpu_backend.py: on Windows
# and Linux, llama_cpp/lib-vulkan beside the CPU-only llama_cpp/lib. The app picks one at
# runtime via LLAMA_CPP_LIB_PATH (see services/llama_backend.py).
#
# These are copied verbatim as datas rather than left to binary analysis, for
# three reasons: ggml-base.dll and llama.dll differ between the two sets and
# must not be deduplicated by basename; ggml-vulkan.dll imports vulkan-1.dll,
# which belongs to the user's graphics driver and must never be vendored out
# of System32; and PyInstaller has no reason to rewrite libraries that are
# loaded by explicit path at runtime. Linux preserves the wheel's nested
# layout and hashed auditwheel dependencies so its relative RPATH still works.
def _is_extra_backend(entry):
    dest = str(entry[1]).replace('\\', '/')
    return dest.startswith('llama_cpp/lib-')


datas = [d for d in datas if not _is_extra_backend(d)]
binaries = [b for b in binaries if not _is_extra_backend(b)]

_llama_spec = importlib.util.find_spec('llama_cpp')
if _llama_spec and _llama_spec.origin:
    _llama_pkg = Path(_llama_spec.origin).parent
    for _backend_dir in sorted(_llama_pkg.glob('lib-*')):
        if not _backend_dir.is_dir():
            continue
        for _f in sorted(_backend_dir.rglob('*')):
            if _f.is_file():
                _relative_parent = _f.parent.relative_to(_llama_pkg).as_posix()
                datas.append((str(_f), f'llama_cpp/{_relative_parent}'))

# sentence_transformers pulls in transformers, tokenizers, and model data.
tmp_ret = collect_all('sentence_transformers')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]

# transformers pulls in tokenizers + model data; collect_all('sentence_transformers')
# does not recurse into this separate dependency's own package data.
tmp_ret = collect_all('transformers')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]

# umap has numba JIT artefacts; collect-all ensures the compiled cache is present.
tmp_ret = collect_all('umap')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]

# setuptools vendors jaraco.text / jaraco.context / backports.tarfile internally.
# pkg_resources (used by setuptools runtime hook pyi_rth_pkgres) imports jaraco.text
# at module level (line 90 of pkg_resources/__init__.py).  Without collect-all
# setuptools the frozen binary crashes with:
#   ModuleNotFoundError: No module named 'backports'
# This is a well-known PyInstaller + setuptools>=70 issue.
tmp_ret = collect_all('setuptools')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


# transformers' models/__init__.py builds its lazy-import structure by reading
# its own .py source files at runtime (AST introspection), not just importing
# them (see transformers.utils.import_utils). Under the default archive mode,
# pure-Python modules get compiled into the zipped PYZ, so those source files
# don't exist as loose files on disk -- this only actually bites on Windows
# (Linux/macOS onedir layouts tolerate it), where the frozen binary fails with:
#   [WinError 3] The system cannot find the path specified: '...transformers\
#   models\__init__.pyc' - semantic search degraded
# collect_all() alone doesn't change *where* a module's bytecode lives, only
# what data/hiddenimports get pulled in -- module_collection_mode is
# PyInstaller's documented mechanism for forcing a package to be collected as
# loose source files instead: https://pyinstaller.org/en/stable/spec-files.html#collect-a-package-as-source-py-files
a = Analysis(
    ['potluck_local/app.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
    module_collection_mode={'transformers': 'py'},
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='potluck-local',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='potluck-local',
)
