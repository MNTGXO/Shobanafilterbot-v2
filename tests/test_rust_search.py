import asyncio
import os
from pathlib import Path
import re
import sys
import tempfile
import textwrap
from unittest import IsolatedAsyncioTestCase, skipUnless
from unittest.mock import patch

from database.rust_search import RustSearchWorker


FAKE_WORKER = '''
    import os
    import sys
    import time
    from bson import BSON
    from struct import unpack

    def read():
        header = sys.stdin.buffer.read(4)
        assert len(header) == 4
        return BSON(header + sys.stdin.buffer.read(unpack('<i', header)[0] - 4)).decode()

    def send(doc):
        sys.stdout.buffer.write(BSON.encode(doc))
        sys.stdout.buffer.flush()

    assert 'BOT_TOKEN' not in os.environ
    assert read() == {'shards': [{'uri': 'mongodb://localhost/test', 'database': 'test'}],
                      'collection': 'files'}
    send({'ok': True})
    while True:
        req = read()
        assert req['filter']['file_name'].pattern == 'test'
        if req['offset'] == 10:
            time.sleep(10)
        if req['offset'] == 20:
            sys.stdout.buffer.write(b'\\x01\\x00\\x00\\x00')
            sys.stdout.buffer.flush()
            continue
        send({'ok': True, 'docs': [{'_id': 'sample', 'caption': None}],
              'count': 2, 'has_more': req['fast']})
'''


class WorkerTest(IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name, 'fake-worker')
        # Preserve the test's explicit dependency path without passing application secrets.
        import bson
        site_packages = Path(bson.__file__).resolve().parent.parent
        path.write_text(f'#!{sys.executable}\nimport sys\nsys.path.insert(0, {str(site_packages)!r})\n'
                        + textwrap.dedent(FAKE_WORKER))
        path.chmod(0o700)
        self.worker = RustSearchWorker(str(path), [('mongodb://localhost/test', 'test')], 'files')
        self.addAsyncCleanup(self.worker.close)
        self.filter = {'file_name': re.compile('test', re.IGNORECASE)}

    async def test_uses_config_through_pipe_and_keeps_worker_for_next_query(self):
        with patch.dict(os.environ, {'BOT_TOKEN': 'not-for-the-worker'}):
            rows, offset, count = await self.worker.search(self.filter, 0, 1, True)
        self.assertEqual(rows, [{'_id': 'sample', 'caption': None}])
        self.assertEqual((offset, count), (1, 2))
        process = self.worker._process
        self.assertEqual(await self.worker.search(self.filter, 1, 1, False), (rows, '', 2))
        self.assertIs(self.worker._process, process)
        await self.worker.close()
        self.assertIsNotNone(process.returncode)

    async def test_invalid_reply_closes_worker_and_backs_off(self):
        with self.assertLogs('database.rust_search', level='WARNING'):
            self.assertIsNone(await self.worker.search(self.filter, 20, 1, False))
        self.assertIsNone(self.worker._process)
        self.assertIsNone(await self.worker.search(self.filter, 0, 1, True))
        self.assertIsNone(self.worker._process)

    async def test_busy_worker_does_not_block_other_search_and_times_out(self):
        await self.worker.search(self.filter, 0, 1, True)
        with patch('database.rust_search._TIMEOUT', 0.1):
            with self.assertLogs('database.rust_search', level='WARNING'):
                stalled = asyncio.create_task(self.worker.search(self.filter, 10, 1, False))
                await asyncio.sleep(0)
                self.assertFalse(stalled.done())
                self.assertIsNone(await self.worker.search(self.filter, 0, 1, True))
                self.assertIsNone(await stalled)
        self.assertIsNone(self.worker._process)

    async def test_disabled_and_out_of_bounds_queries_skip_worker(self):
        disabled = RustSearchWorker('', [], 'files')
        self.assertIsNone(await disabled.search(self.filter, 0, 1, True))
        self.assertIsNone(await self.worker.search(self.filter, 0, 101, True))
        self.assertIsNone(await self.worker.search(self.filter, 2501, 1, True))
        self.assertIsNone(self.worker._process)

    @skipUnless(os.environ.get('RUST_SEARCH_WORKER'), 'compiled worker not provided')
    async def test_compiled_worker_accepts_bson_settings(self):
        worker = RustSearchWorker(os.environ['RUST_SEARCH_WORKER'],
                                  [('mongodb://127.0.0.1:27017', 'test')], 'files')
        self.addAsyncCleanup(worker.close)
        # The handshake uses no live catalog. The intentionally invalid page is
        # rejected by Rust before it sends a query to MongoDB.
        reply = await asyncio.wait_for(worker._request({'filter': self.filter,
                                                         'offset': -1, 'limit': 1,
                                                         'fast': True}), 4)
        self.assertEqual(reply, {'ok': False})
