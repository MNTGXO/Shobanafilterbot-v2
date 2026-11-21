"""Opt-in indexed title search across the existing MongoDB media collections."""

import asyncio
import logging
import time

from pymongo.errors import PyMongoError


LOGGER = logging.getLogger(__name__)
_TIMEOUT = 4
_MAX_TIME_MS = 2500
_STATUS_TTL = 60
_RETRY_DELAY = 30
_MAX_OFFSET = 2500
_MAX_LIMIT = 100

MEDIA_PROJECTION = {
    'file_name': 1,
    'file_size': 1,
    'file_type': 1,
    'caption': 1,
    'created_at': 1,
}


def _search_spec(index, query, file_type):
    compound = {'must': [{'text': {'path': 'file_name', 'query': query, 'matchCriteria': 'all'}}]}
    if file_type:
        compound['filter'] = [{'equals': {'path': 'file_type', 'value': file_type}}]
    return {'index': index, 'compound': compound}


async def _all(coroutines):
    # Do not leave reads running in the background if another shard fails and
    # the caller falls back to regex search. The outer timeout bounds the batch.
    results = await asyncio.gather(*coroutines, return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            raise result
    return results


class AtlasCatalogSearch:
    def __init__(self, collections, index):
        self._collections = tuple(collections)
        self._index = index
        self._ready_until = 0
        self._retry_at = 0
        self._status_lock = asyncio.Lock()

    async def search(self, query, file_type, offset, limit, fast):
        """Return a page tuple, or None if callers should run legacy search."""
        if (not query or not any(char.isalnum() for char in query)
                or not (0 <= offset <= _MAX_OFFSET and 1 <= limit <= _MAX_LIMIT)
                or time.monotonic() < self._retry_at):
            return None
        try:
            return await asyncio.wait_for(self._query(query, file_type, offset, limit, fast), _TIMEOUT)
        except (PyMongoError, asyncio.TimeoutError, ValueError, TypeError):
            self._ready_until = 0
            if time.monotonic() >= self._retry_at:
                self._retry_at = time.monotonic() + _RETRY_DELAY
                LOGGER.warning('Indexed title search unavailable; using legacy MongoDB search')
            return None

    async def _ensure_indexes(self):
        if time.monotonic() < self._ready_until:
            return True
        async with self._status_lock:
            now = time.monotonic()
            if now < self._ready_until:
                return True
            if now < self._retry_at:
                return False
            statuses = await _all(self._list_index(col) for col in self._collections)
            if not all(rows and rows[0].get('status') == 'READY'
                       and rows[0].get('queryable') is True for rows in statuses):
                self._retry_at = time.monotonic() + _RETRY_DELAY
                LOGGER.warning('Indexed title search requires a ready index on every media shard; using legacy search')
                return False
            self._ready_until = time.monotonic() + _STATUS_TTL
            LOGGER.info('Indexed title search verified on %d MongoDB media shard(s)', len(statuses))
            return True

    async def _list_index(self, col):
        return await col.aggregate(
            [{'$listSearchIndexes': {'name': self._index}}], maxTimeMS=_MAX_TIME_MS,
        ).to_list(length=1)

    async def _query(self, query, file_type, offset, limit, fast):
        if not await self._ensure_indexes():
            return None
        spec = _search_spec(self._index, query, file_type)
        single = len(self._collections) == 1
        page_size = limit + int(bool(fast))
        per_shard_limit = page_size if single else offset + page_size
        page_pipeline = [{'$search': {**spec, 'sort': {'created_at': -1, '_id': 1}}}]
        if single and offset:
            page_pipeline.append({'$skip': offset})
        page_pipeline.extend([{'$limit': per_shard_limit}, {'$project': MEDIA_PROJECTION}])

        async def read_shard(col):
            return await col.aggregate(page_pipeline, maxTimeMS=_MAX_TIME_MS).to_list(length=per_shard_limit)

        async def count_shard(col):
            rows = await col.aggregate(
                [{'$searchMeta': {**spec, 'count': {'type': 'total'}}}], maxTimeMS=_MAX_TIME_MS,
            ).to_list(length=1)
            total = rows[0].get('count', {}).get('total') if rows else None
            if not isinstance(total, int) or isinstance(total, bool) or total < 0:
                raise ValueError('missing indexed result count')
            return total

        queries = [read_shard(col) for col in self._collections]
        if not fast:
            queries.extend(count_shard(col) for col in self._collections)
        answers = await _all(queries)
        parts = answers[:len(self._collections)]
        if single:
            page = parts[0]
        else:
            # Atlas sorts each shard by index keys; preserve recent-first order
            # globally and retain shard order for timestamp ties.
            rows = sorted((doc for part in parts for doc in part),
                          key=lambda doc: doc.get('created_at', 0), reverse=True)
            page = rows[offset:offset + page_size]
        if fast:
            has_more = len(page) > limit
            page = page[:limit]
            total = offset + len(page) + int(has_more)
        else:
            total = sum(answers[len(self._collections):])
            has_more = offset + limit < total
        return page, offset + limit if has_more else '', total
