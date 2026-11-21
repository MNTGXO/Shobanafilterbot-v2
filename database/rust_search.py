"""Private BSON pipe to the Rust catalog worker; Python remains the only bot."""
import asyncio
import logging
import os
import struct
import time

from bson import BSON, Int64
from bson.errors import BSONError


_MAX_FRAME = 4 * 1024 * 1024
_MAX_OFFSET = 2_500
_MAX_PAGE = 100
_TIMEOUT = 4
_RETRY_DELAY = 30
logger = logging.getLogger(__name__)


class RustSearchWorker:
    def __init__(self, executable, shards, collection):
        self._executable = executable
        self._settings = {
            'shards': [{'uri': uri, 'database': database} for uri, database in shards],
            'collection': collection,
        }
        self._process = None
        self._lock = asyncio.Lock()
        self._retry_at = 0

    async def search(self, search_filter, offset, limit, fast):
        # Keep the fast path bounded. A simultaneous search can still use Motor
        # instead of queueing behind another request on the worker pipe.
        if (not self._executable or self._lock.locked() or time.monotonic() < self._retry_at
                or not (0 <= offset <= _MAX_OFFSET and 1 <= limit <= _MAX_PAGE)):
            return None
        async with self._lock:
            previous_process = self._process
            try:
                result = await asyncio.wait_for(
                    self._request({'filter': search_filter, 'offset': Int64(offset),
                                   'limit': Int64(limit), 'fast': bool(fast)}), _TIMEOUT,
                )
                if result.get('ok') is not True:
                    raise ValueError('worker query failed')
                docs, count, has_more = result['docs'], result['count'], result['has_more']
                if (not isinstance(docs, list) or len(docs) > limit
                        or not all(isinstance(doc, dict) and '_id' in doc for doc in docs)
                        or isinstance(count, bool) or not isinstance(count, int) or count < 0
                        or not isinstance(has_more, bool)):
                    raise ValueError('invalid worker reply')
                if self._process is not previous_process:
                    logger.info('Rust catalog worker served its first MongoDB search')
                return docs, offset + limit if has_more else '', count
            except (BSONError, KeyError, ValueError, OSError, asyncio.IncompleteReadError, asyncio.TimeoutError):
                self._retry_at = time.monotonic() + _RETRY_DELAY
                logger.warning('Rust catalog worker unavailable; falling back to Python MongoDB search')
                await self._stop()
                return None
            except asyncio.CancelledError:
                # Do not allow the next request to consume a cancelled request's reply.
                await self._stop()
                raise

    async def _request(self, payload):
        if self._process is None or self._process.returncode is not None:
            self._process = await asyncio.create_subprocess_exec(
                self._executable,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                # Configuration comes from the Python process over stdin. Don't
                # expose the bot token or other application secrets to the worker.
                env={name: os.environ[name] for name in ('PATH', 'SSL_CERT_FILE', 'SSL_CERT_DIR')
                     if name in os.environ},
            )
            handshake = await self._exchange(self._settings)
            if handshake.get('ok') is not True:
                raise ValueError('worker initialization failed')
        return await self._exchange(payload)

    async def _exchange(self, payload):
        wire = BSON.encode(payload)
        if len(wire) > _MAX_FRAME:
            raise ValueError('worker request exceeds size limit')
        writer, reader = self._process.stdin, self._process.stdout
        writer.write(wire)
        await writer.drain()
        header = await reader.readexactly(4)
        size = struct.unpack('<i', header)[0]
        if not 5 <= size <= _MAX_FRAME:
            raise ValueError('invalid BSON message length')
        return BSON(header + await reader.readexactly(size - 4)).decode()

    async def _stop(self):
        process, self._process = self._process, None
        if process is not None:
            process.stdin.close()
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            # Drain the pipe so a killed worker with a paused stdout reader can be reaped.
            await process.stdout.read()
            await process.wait()

    async def close(self):
        async with self._lock:
            await self._stop()
