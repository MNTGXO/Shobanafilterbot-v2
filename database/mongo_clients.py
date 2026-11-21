"""Reuse Motor connection pools across the bot's Python database modules."""

from functools import lru_cache

from motor.motor_asyncio import AsyncIOMotorClient


def mongo_client(uri):
    return _open_client(uri.strip())


@lru_cache(maxsize=None)
def _open_client(uri):
    # Only called with configured URIs (at most five), never user input.
    return AsyncIOMotorClient(uri)
