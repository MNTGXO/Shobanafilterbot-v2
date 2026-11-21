import asyncio
from contextlib import contextmanager
import importlib.util
from pathlib import Path
import re
import sys
import threading
from types import ModuleType, SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock, patch


DB_PATH = Path(__file__).resolve().parents[1] / 'database' / 'ia_filterdb.py'


def load_catalog(*, mongo, store=None):
    settings = ModuleType('info')
    settings.DATABASE_URI = 'mongodb://127.0.0.1/?connect=false' if mongo else ''
    settings.DATABASE_NAME = 'catalog'
    settings.COLLECTION_NAME = 'files'
    settings.USE_CAPTION_FILTER = False
    settings.MONGO_INDEXED_SEARCH = False
    settings.MONGO_SEARCH_INDEX = "media_titles"
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


def telegram_media():
    return SimpleNamespace(
        file_id='BAACAgIAAwx0ZWxlZ3JhbS1yZWYAA7d6AAYuagAGHgQ',
        file_name='Sample.Movie.mp4',
        file_size=42,
        file_type='video',
        mime_type='video/mp4',
        caption=SimpleNamespace(html='Sample'),
    )


# Existing catalog ID for the fixture, generated using the old encoder.
LEGACY_CATALOG_ID = 'BAADAgADt3oABi5qAAYWBA'


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

    async def test_indexed_empty_result_does_not_run_regex_search(self):
        catalog = load_catalog(mongo=True)
        catalog._atlas_search = MagicMock(search=AsyncMock(return_value=([], '', 0)))
        catalog._rust_search.search = AsyncMock()
        self.assertEqual(await catalog.get_search_results('missing', fast=True), ([], '', 0))
        catalog._rust_search.search.assert_not_awaited()

    async def test_indexed_unavailability_uses_current_rust_search(self):
        catalog = load_catalog(mongo=True)
        catalog._atlas_search = MagicMock(search=AsyncMock(return_value=None))
        catalog._rust_search.search = AsyncMock(return_value=([media('a', 1)], '', 1))
        files, offset, total = await catalog.get_search_results('title', fast=True)
        self.assertEqual([file.file_id for file in files], ['a'])
        self.assertEqual((offset, total), ('', 1))
        catalog._rust_search.search.assert_awaited_once()

    async def test_mongo_save_keeps_catalog_id_and_does_not_store_separate_reference(self):
        catalog = load_catalog(mongo=True)
        col = MagicMock(insert_one=AsyncMock())
        catalog._mongo_collections = [col]
        self.assertEqual(await catalog.save_file(telegram_media()), (True, 1))
        record, = col.insert_one.await_args.args
        self.assertEqual(record['_id'], LEGACY_CATALOG_ID)
        self.assertEqual(record['file_type'], 'video')
        self.assertEqual(record['mime_type'], 'video/mp4')
        self.assertNotIn('file_ref', record)

    async def test_sql_save_uses_existing_catalog_id_without_reference_column(self):
        connection = MagicMock()
        connection.execute.return_value.first.return_value = None

        @contextmanager
        def begin():
            yield connection

        catalog = load_catalog(mongo=False, store=MagicMock(begin=begin))
        self.addCleanup(catalog._sql_read_pool.shutdown)
        self.assertEqual(await catalog.save_file(telegram_media()), (True, 1))
        statement, params = connection.execute.call_args.args
        self.assertNotIn('file_ref', str(statement))
        self.assertEqual(params['fid'], LEGACY_CATALOG_ID)
        self.assertEqual(params['ftype'], 'video')
        self.assertEqual(params['mtype'], 'video/mp4')

    async def test_sql_count_stays_off_loop_and_does_not_materialize_the_catalog(self):
        caller_thread = threading.get_ident()
        threads = []
        connection = MagicMock()
        connection.execute.return_value.scalar.return_value = 2_100_000
        stream = connection.execution_options.return_value
        stream.execute.return_value = iter([('Title',), ('Title II',), ('Another',), ('Subtitle',)])

        @contextmanager
        def begin():
            threads.append(threading.get_ident())
            yield connection

        catalog = load_catalog(mongo=False, store=MagicMock(begin=begin))
        self.addCleanup(catalog._sql_read_pool.shutdown)
        self.assertEqual(await catalog.Media.count_documents(), 2_100_000)
        self.assertEqual(str(connection.execute.call_args.args[0]), 'SELECT COUNT(*) FROM media')
        self.assertEqual(await catalog.Media.count_documents({'file_name': re.compile(r'\btitle\b', re.I)}), 2)
        connection.execution_options.assert_called_once_with(yield_per=1000)
        self.assertEqual(str(stream.execute.call_args.args[0]), 'SELECT file_name FROM media')
        self.assertEqual(len(threads), 2)
        self.assertTrue(all(thread != caller_thread for thread in threads))

    async def test_sql_catalog_search_and_file_lookup_run_off_loop(self):
        caller_thread = threading.get_ident()
        calls = []
        row = ('file-id', 'Title', 10, 'document', 'text/plain', 'caption', 7.0)
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
