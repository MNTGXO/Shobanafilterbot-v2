import asyncio
from contextlib import contextmanager
import importlib.util
from pathlib import Path
import sys
import threading
from types import ModuleType
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock, patch


DB_PATH = Path(__file__).resolve().parents[1] / 'database' / 'ia_filterdb.py'


def load_catalog(*, mongo, store=None):
    settings = ModuleType('info')
    settings.DATABASE_URI = 'mongodb://127.0.0.1/?connect=false' if mongo else ''
    settings.DATABASE_NAME = 'catalog'
    settings.COLLECTION_NAME = 'files'
    settings.USE_CAPTION_FILTER = False
    for shard in range(2, 6):
        setattr(settings, f'DATABASE_URI{shard}', '')
        setattr(settings, f'DATABASE_NAME{shard}', 'catalog')
    sql = ModuleType('database.sql_store')
    sql.store = store
    spec = importlib.util.spec_from_file_location('_test_catalog', DB_PATH)
    catalog = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {'info': settings, 'database.sql_store': sql}):
        spec.loader.exec_module(catalog)
    return catalog


def shard(rows):
    col = MagicMock()
    cursor = col.find.return_value
    cursor.sort.return_value = cursor
    cursor.skip.return_value = cursor
    cursor.limit.return_value = cursor
    cursor.to_list = AsyncMock(return_value=rows)
    col.count_documents = AsyncMock(return_value=len(rows))
    return col


def media(file_id, created_at):
    return {'_id': file_id, 'file_name': 'Title', 'created_at': created_at}


class CatalogSearchTest(IsolatedAsyncioTestCase):
    async def test_single_mongo_shard_fast_fallback_reads_once(self):
        catalog = load_catalog(mongo=True)
        catalog._rust_search.search = AsyncMock(return_value=None)
        col = shard([media('a', 10), media('b', 9)])
        catalog._mongo_collections = [col]
        files, next_offset, total = await catalog.get_search_results('title', max_results=1, fast=True)
        self.assertEqual([file.file_id for file in files], ['a'])
        self.assertEqual((next_offset, total), (1, 2))
        col.find.assert_called_once()
        col.count_documents.assert_not_called()

    async def test_five_shard_fallback_keeps_order_ties_and_exact_count(self):
        catalog = load_catalog(mongo=True)
        catalog._rust_search.search = AsyncMock(return_value=None)
        catalog.MONGO_SHARD_COUNT = 5
        catalog._mongo_collections = [
            shard([media('a', 10), media('b', 6)]), shard([]), shard([media('c', 10), media('d', 9)]),
            shard([media('e', 10), media('f', 6)]), shard([media('g', 8)]),
        ]
        files, next_offset, total = await catalog.get_search_results('title', offset=1, max_results=4)
        self.assertEqual([file.file_id for file in files], ['c', 'e', 'd', 'g'])
        self.assertEqual((next_offset, total), (5, 7))
        for col in catalog._mongo_collections:
            col.count_documents.assert_awaited_once()

    async def test_sql_catalog_search_and_file_lookup_run_off_loop(self):
        caller_thread = threading.get_ident()
        calls = []
        row = ('file-id', 'ref', 'Title', 10, 'document', 'text/plain', 'caption', 7.0)
        connection = MagicMock()
        connection.execute.return_value.fetchall.return_value = [row]
        connection.execute.return_value.first.return_value = row

        @contextmanager
        def begin():
            calls.append(threading.get_ident())
            yield connection

        catalog = load_catalog(mongo=False, store=MagicMock(begin=begin))
        self.addCleanup(catalog._sql_read_pool.shutdown)
        files, next_offset, total = await asyncio.wait_for(
            catalog.get_search_results('Title', max_results=3, fast=True), 2,
        )
        self.assertEqual((next_offset, total), ('', 1))
        self.assertEqual(files[0].file_id, 'file-id')
        details = await asyncio.wait_for(catalog.get_file_details('file-id'), 2)
        self.assertEqual(details, files)
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(thread != caller_thread for thread in calls))
