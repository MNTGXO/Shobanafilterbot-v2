from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock

from pymongo.errors import OperationFailure

from database.atlas_search import AtlasCatalogSearch


def doc(file_id, timestamp):
    return {'_id': file_id, 'created_at': timestamp}


def shard(rows=(), *, status='READY', count=None, fail=False):
    collection = MagicMock()

    def aggregate(pipeline, **options):
        operator = next(iter(pipeline[0]))
        if operator == '$listSearchIndexes':
            reply = [] if status is None else [{'status': status, 'queryable': status == 'READY'}]
        elif operator == '$searchMeta':
            reply = [{'count': {'total': count if count is not None else len(rows)}}]
        elif operator == '$search':
            cursor = MagicMock()
            if fail:
                cursor.to_list = AsyncMock(side_effect=OperationFailure('query failed'))
                return cursor
            offset = next((stage['$skip'] for stage in pipeline if '$skip' in stage), 0)
            limit = next(stage['$limit'] for stage in pipeline if '$limit' in stage)
            reply = list(rows[offset:offset + limit])
        else:
            raise AssertionError(f'unexpected stage {operator}')
        cursor = MagicMock()
        cursor.to_list = AsyncMock(return_value=reply)
        return cursor

    collection.aggregate.side_effect = aggregate
    return collection


class AtlasSearchTest(IsolatedAsyncioTestCase):
    async def test_fast_page_merges_five_shards_and_preserves_file_type(self):
        shards = [shard([doc('a', 9), doc('f', 3)]), shard([doc('b', 9)]),
                  shard([]), shard([doc('c', 7), doc('e', 5)]), shard([doc('d', 6)])]
        catalog = AtlasCatalogSearch(shards, 'media_titles')
        page, next_offset, total = await catalog.search('New Movie', 'video', 1, 3, True)
        self.assertEqual([row['_id'] for row in page], ['b', 'c', 'd'])
        self.assertEqual((next_offset, total), (4, 5))
        for col in shards:
            status, search = col.aggregate.call_args_list
            self.assertEqual(status.args[0], [{'$listSearchIndexes': {'name': 'media_titles'}}])
            pipeline = search.args[0]
            spec = pipeline[0]['$search']
            self.assertEqual(spec['compound']['filter'], [{'equals': {'path': 'file_type', 'value': 'video'}}])
            self.assertEqual(spec['compound']['must'][0]['text']['matchCriteria'], 'all')
            self.assertEqual(spec['sort'], {'created_at': -1, '_id': 1})
            self.assertEqual(pipeline[1], {'$limit': 5})

    async def test_single_shard_skips_on_server_and_counts_exactly(self):
        col = shard([doc('a', 9), doc('b', 8), doc('c', 7)], count=10)
        catalog = AtlasCatalogSearch([col], 'media_titles')
        self.assertEqual(await catalog.search('Movie', None, 1, 1, False), ([doc('b', 8)], 2, 10))
        _, search, count = col.aggregate.call_args_list
        self.assertEqual(search.args[0][1], {'$skip': 1})
        self.assertEqual(count.args[0][0]['$searchMeta']['count'], {'type': 'total'})

    async def test_one_missing_index_disables_indexed_search_across_all_shards(self):
        shards = [shard(), shard(status=None)]
        catalog = AtlasCatalogSearch(shards, 'media_titles')
        with self.assertLogs('database.atlas_search', level='WARNING'):
            self.assertIsNone(await catalog.search('Movie', None, 0, 10, True))
        self.assertIsNone(await catalog.search('Movie', None, 0, 10, True))
        for col in shards:
            col.aggregate.assert_called_once()

    async def test_indexed_no_match_is_not_a_failure_and_reuses_readiness(self):
        col = shard()
        catalog = AtlasCatalogSearch([col], 'media_titles')
        self.assertEqual(await catalog.search('Missing', None, 0, 10, True), ([], '', 0))
        self.assertEqual(await catalog.search('Another missing', None, 0, 10, True), ([], '', 0))
        self.assertEqual(col.aggregate.call_count, 3)

    async def test_query_error_is_fallback_with_cooldown(self):
        col = shard(fail=True)
        catalog = AtlasCatalogSearch([col], 'media_titles')
        with self.assertLogs('database.atlas_search', level='WARNING'):
            self.assertIsNone(await catalog.search('Movie', None, 0, 10, True))
        self.assertIsNone(await catalog.search('Another', None, 0, 10, True))
        self.assertEqual(col.aggregate.call_count, 2)
