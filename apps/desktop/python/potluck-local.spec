# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = [('potluck_local/data/models.json', 'potluck_local/data')]
binaries = []
hiddenimports = [
    'aiosqlite',
    'aiosqlite.core',
    # Imported by SciPy's native extensions rather than Python source.
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
