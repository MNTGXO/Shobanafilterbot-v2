# MongoDB storage and indexed title search

## What the bot stores

With `DATABASE_URI` set, the primary database stores bot state and participates in the media catalog. Up to four additional connections (`DATABASE_URI2`–`DATABASE_URI5`) hold media records only. When `DATABASE_URI` is unset and `POSTGRES_URI` is configured, the bot uses PostgreSQL instead.

| Collection | What it contains | Location |
| --- | --- | --- |
| `COLLECTION_NAME` (`mn_files` by default) | Telegram file ID (`_id`), normalized file name, file size, Telegram media type (`file_type`), MIME type (`mime_type`), optional HTML caption, and when it was indexed (`created_at`). The actual audio/video/document bytes stay on Telegram. | Primary plus any configured media shards; the bot distributes new records by file ID. |
| `users` | Telegram user ID, name, and ban status/reason. | Primary |
| `groups` | Telegram chat ID/title, enable/disable status/reason, and per-group settings. | Primary |
| `CONNECTION` | The groups a user has linked to their private chat with the bot and their currently selected group. | Primary |
| Collection named for a group's ID | Manual filter trigger text, reply text, buttons, alert text, and optional Telegram file reference. | Primary |
| `config` | Force-subscribe channel IDs, update channel IDs and on/off state, announcement deduplication keys, daily added titles, and the last daily summary marker. | Primary |
| `invite_links`, `join_users` | Saved invite links by chat/purpose, and tracked join requests by user/chat (including the recorded name and update time). | Primary |

`file_type` distinguishes Telegram documents, videos, and audio for inline result labels and type filters. `mime_type` (such as `video/mp4`) is used by the admin `/delete` fallback when there is no exact file ID match, to distinguish files with the same name and size. The bot no longer writes a separate `file_ref`: delivery already uses the existing catalog ID. Old MongoDB records and PostgreSQL columns are left in place; there is no automatic bulk cleanup or ID migration.

The bot does not persist a general search history to MongoDB. Its short-lived search-result cache is in memory. Do not share database dumps publicly: they can contain user IDs, invite links, and captions.

## Optional: enable MongoDB Search for the media catalog

This is optional. The default remains the existing MongoDB regex search (using the Rust worker when available), and PostgreSQL is unchanged. No migration or bot-managed index creation runs on startup.

1. Check that every configured MongoDB media shard supports **MongoDB Search** (`$search` and `$listSearchIndexes`). In the Atlas **Search Indexes** screen (not ordinary collection indexes), create an index named `media_titles` on the configured media collection in **each** shard. Paste the [index definition](media_titles.search-index.json) into the JSON editor. A MongoDB `{file_name: 1}` or `{file_name: "text"}` index is not a MongoDB Search index.
2. Wait for every index to be ready/queryable. Allow time and storage for the initial indexing of large catalogs. The bot needs permission to list Search indexes and read the media collection, but it does not need index-management permission for this feature.
3. In a test deployment, set `MONGO_INDEXED_SEARCH=true`. The optional `MONGO_SEARCH_INDEX` defaults to `media_titles`; if you change the name, use the same one on every shard. Leave `USE_CAPTION_FILTER` **unset** for title-only testing. If caption matching is enabled, the bot intentionally retains the existing regex path rather than dropping caption matches.
4. Search for common and rare titles, Malayalam titles, and a title with no matches. Compare the first page and pagination against the default mode using the same data. Check for `Indexed title search verified on … MongoDB media shard(s)` in the service logs. Measure uncached search time separately from cache hits; this feature targets the work MongoDB does to find matches, not Telegram delivery latency.
5. To return to the old behavior, set `MONGO_INDEXED_SEARCH=false` or remove it and redeploy. You do not need to drop the index to roll back.

Indexed mode searches **analyzed filename tokens** with all query terms required, then sorts by newest catalog entry. It is not a substring/regex search, so punctuation, token order, and partial titles can produce different results. Search indexes also update asynchronously; recently indexed files can take time to appear. Unsupported requests (including caption searches and very deep pages), index readiness failures, and query errors fall back to the existing path across all shards. A successful indexed query with **zero matches** does not fall back to a costly regex scan.

## Where more RAM helps (and where it doesn't)

The bot doesn't keep the media catalog in memory. Its Python modules reuse one MongoDB connection pool per configured URI, even if that URI serves multiple databases. The Rust worker still has its own connections. In PostgreSQL mode, exact catalog totals run as a database-side `COUNT(*)`, and filtered admin previews stream results in batches off the Telegram event loop. The database can still need to scan rows; the bot no longer needs RAM for the full catalog to count them.

For **cold title searches**, measure the database path before resizing the bot. Compare p50 and p95 `search_time` for the same common, rare, and no-match queries, both at low load and during a representative burst. Measure warm repeats separately. Check bot memory/CPU and database latency side by side. A larger bot instance helps if the bot itself is saturated; it won't fix a remote MongoDB collection scan. For PostgreSQL substring search, investigate a `pg_trgm` GIN index in staging if the query plan shows sequential scans; index build time, storage, and write overhead should be measured before applying it to production.
