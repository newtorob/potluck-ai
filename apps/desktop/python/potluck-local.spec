# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = [('potluck_local/data/models.json', 'potluck_local/data')]
binaries = []
hiddenimports = [
    'aiosqlite',
    'aiosqlite.core',
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
