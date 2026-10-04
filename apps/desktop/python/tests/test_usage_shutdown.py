"""Real process regression: disconnect finalizers must close SQLite workers."""
import os
import subprocess
import sys


def test_cancelled_usage_finalizer_exits(tmp_path):
    code = '''
import asyncio, anyio, sqlite3
from potluck_local.services.memory_db import init_memory_db, get_memory_db_path
from potluck_local.services.usage_store import record_usage_event
async def main():
    await init_memory_db()
    with anyio.CancelScope() as scope:
        scope.cancel()
        await record_usage_event(direction='consumed', scope='local', model_slug='test')
    # Direct asyncio cancellation must also drain a transaction already started.
    task = asyncio.create_task(record_usage_event(direction='consumed', scope='local', model_slug='test'))
    await asyncio.sleep(0)
    task.cancel()
    try: await task
    except asyncio.CancelledError: pass
    with sqlite3.connect(get_memory_db_path()) as db:
        assert db.execute('SELECT COUNT(*) FROM usage_events').fetchone()[0] == 2
asyncio.run(main())
'''
    result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True,
        env={**os.environ, 'POTLUCK_DATA_DIR': str(tmp_path),
             'POTLUCK_MEMORY_DB_PATH': str(tmp_path / 'memory.db')}, timeout=10)
    assert result.returncode == 0, result.stderr
