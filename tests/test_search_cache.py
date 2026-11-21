import asyncio
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock

from database.search_cache import SearchCache


class SearchCacheTest(IsolatedAsyncioTestCase):
    async def test_concurrent_identical_searches_load_once(self):
        cache = SearchCache()
        entered, release = asyncio.Event(), asyncio.Event()

        async def run_search():
            entered.set()
            await release.wait()
            return 'page'

        load = AsyncMock(side_effect=run_search)
        tasks = [asyncio.create_task(cache.get_or_load('title', load)) for _ in range(10)]
        await entered.wait()
        release.set()
        self.assertEqual(await asyncio.gather(*tasks), ['page'] * 10)
        self.assertEqual(await cache.get_or_load('title', load), 'page')
        load.assert_awaited_once()

    async def test_invalidation_does_not_join_or_cache_old_search(self):
        cache = SearchCache()
        entered, release = asyncio.Event(), asyncio.Event()

        async def stale_load():
            entered.set()
            await release.wait()
            return 'old page'

        old = asyncio.create_task(cache.get_or_load('title', stale_load))
        await entered.wait()
        cache.invalidate()
        load = AsyncMock(return_value='new page')
        self.assertEqual(await cache.get_or_load('title', load), 'new page')
        release.set()
        self.assertEqual(await old, 'old page')
        self.assertEqual(await cache.get_or_load('title', load), 'new page')
        load.assert_awaited_once()

    async def test_cancelled_waiter_does_not_cancel_shared_read(self):
        cache = SearchCache()
        entered, release = asyncio.Event(), asyncio.Event()

        async def run_search():
            entered.set()
            await release.wait()
            return 'page'

        load = AsyncMock(side_effect=run_search)
        first = asyncio.create_task(cache.get_or_load('title', load))
        await entered.wait()
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        release.set()
        self.assertEqual(await cache.get_or_load('title', load), 'page')
        load.assert_awaited_once()

    async def test_shutdown_cancels_shared_reads(self):
        cache = SearchCache()
        entered = asyncio.Event()

        async def run_search():
            entered.set()
            await asyncio.Future()

        waiter = asyncio.create_task(cache.get_or_load('title', run_search))
        await entered.wait()
        await cache.cancel_pending()
        with self.assertRaises(asyncio.CancelledError):
            await waiter

    async def test_failure_is_not_cached(self):
        cache = SearchCache()
        load = AsyncMock(side_effect=[RuntimeError('database unavailable'), 'page'])
        with self.assertRaisesRegex(RuntimeError, 'database unavailable'):
            await cache.get_or_load('title', load)
        self.assertEqual(await cache.get_or_load('title', load), 'page')
        self.assertEqual(load.await_count, 2)
