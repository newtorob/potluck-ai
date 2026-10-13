# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = []
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

# setuptools vendors jaraco.text / jaraco.context / backports.tarfile internally.
# pkg_resources (used by setuptools runtime hook pyi_rth_pkgres) imports jaraco.text
# at module level; without collect-all setuptools the frozen binary crashes with
# ModuleNotFoundError: No module named 'backports'.
tmp_ret = collect_all('setuptools')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['potluck_local/mcp/__main__.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # numpy stays in the bundle: memory_retrieval.py and embeddings.py import it
    # at module level for cosine similarity over stored embeddings. Without
    # numpy the MCP entry fails with ModuleNotFoundError before reaching the
    # optional embedder path.
    # sentence_transformers / torch / llama_cpp / GPUtil are excluded: they
    # are lazy-loaded and the embedder gracefully degrades (embed() returns None)
    # so semantic_search falls back to [] and callers fall back to keyword search.
    excludes=['llama_cpp', 'torch', 'sentence_transformers', 'GPUtil'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='potluck-mcp',
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
    name='potluck-mcp',
)
