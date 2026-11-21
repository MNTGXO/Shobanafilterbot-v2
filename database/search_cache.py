"""Short-lived catalog pages and shared reads for concurrent identical searches."""

import asyncio
from collections import OrderedDict
import time


class SearchCache:
    def __init__(self, ttl=30, capacity=256, max_pending=64):
        self._ttl = ttl
        self._capacity = capacity
        self._max_pending = max_pending
        self._values = OrderedDict()
        self._pending = {}
        self._generation = 0

    def invalidate(self):
        # Searches that started before a catalog change must not refill the cache
        # or be joined by new requests after the change.
        self._generation += 1
        self._values.clear()

    async def get_or_load(self, key, load):
        cached = self._values.get(key)
        if cached is not None:
            created_at, result = cached
            if time.monotonic() - created_at <= self._ttl:
                self._values.move_to_end(key)
                return result
            self._values.pop(key)

        pending_key = (self._generation, key)
        task = self._pending.get(pending_key)
        if task is None:
            if len(self._pending) >= self._max_pending:
                # Keep shared state bounded without refusing unrelated searches.
                return await self._load_and_save(pending_key, load)
            task = asyncio.create_task(self._load_and_save(pending_key, load))
            self._pending[pending_key] = task
            task.add_done_callback(lambda completed: self._finish(pending_key, completed))
        # A cancelled Telegram request must not cancel another caller's read.
        return await asyncio.shield(task)

    async def cancel_pending(self):
        """Drain shared tasks before shutting down the database worker."""
        self.invalidate()
        tasks = tuple(self._pending.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _load_and_save(self, pending_key, load):
        generation, key = pending_key
        result = await load()
        if generation == self._generation:
            self._values[key] = (time.monotonic(), result)
            self._values.move_to_end(key)
            while len(self._values) > self._capacity:
                self._values.popitem(last=False)
        return result

    def _finish(self, pending_key, task):
        self._pending.pop(pending_key, None)
        # Consume the exception even if every caller cancelled. Awaiters still
        # receive the original exception; the failed result is never cached.
        if not task.cancelled():
            task.exception()
