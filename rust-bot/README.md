# Rust staging migration

This is an **admin-only, read-only staging bot**, not a drop-in replacement. The root Python `Dockerfile`, bot, and deployment files are unchanged. The Rust bot uses grammers for Telegram, MongoDB `$text` for catalog search, and Axum on `0.0.0.0:$PORT` for Koyeb health checks. It does not send files, write catalog entries, or implement the Python bot's group filters, force-subscribe rules, stored users/settings, or PostgreSQL/SQLite backends.

## 1. Prepare MongoDB

Prefer a staging copy of the production catalog. If you must test queries against production, use a separate MongoDB user with only `read` access to the catalog database(s), and restrict its network access. Never hand the Rust service the Python service's write credentials. Back up first and schedule any production index build with the database owner; indexing uses disk and compute and is not part of bot startup or CI.

Create a MongoDB text index on `file_name` on each catalog collection (including every configured shard). In Atlas you can use `{ file_name: "text" }` with the option `default_language: "none"`. Alternatively, run this from the repository root with the URI of the intended cluster and an account authorized to create indexes:

```sh
DATABASE_NAME=Cluster0 COLLECTION_NAME=mn_files \
  mongosh "$MONGO_INDEX_ADMIN_URI" --file rust-bot/scripts/create-text-index.js
```

MongoDB allows only one text index per collection. Inspect any existing text index first and reuse it only if the fields and behavior are suitable. The script never drops or replaces an index. An ordinary `{file_name: 1}` index does not serve `$text`, and adding a text index alone does not speed up the Python bot's regex. Compare common titles, punctuation, Malayalam, and no-match searches on staging. Text search is token/phrase-based, not substring/regex search; results and ranking across shards may differ. Use an Atlas query plan to verify index use and measure latency on your own catalog.

## 2. Configure the staging bot

Set these in a separate Koyeb service (or export them in a private local shell). Store sensitive values as Koyeb secrets, not in a workflow file or a committed `.env`. Cargo does not automatically load `.env`.

| Setting | Value |
| --- | --- |
| `RUST_STAGING` | `1` (required explicit staging opt-in) |
| `BOT_TOKEN` | **Secondary** token from BotFather, never the production token during staging |
| `API_ID`, `API_HASH` | Existing Telegram application credentials |
| `DATABASE_URI` | Staging or read-only MongoDB connection string |
| `DATABASE_NAME`, `COLLECTION_NAME` | Existing values; defaults are `Cluster0` and `mn_files` |
| `DATABASE_URI2`…`DATABASE_URI5`, `DATABASE_NAME2`…`DATABASE_NAME5` | Optional catalog shards; database names default to `DATABASE_NAME` |
| `ADMINS` | Space-separated positive Telegram user IDs allowed to test in private chats |
| `CHANNELS` | Optional space-separated channel IDs or usernames allowlisted for `/index` |
| `GRAMMERS_SESSION_B64` | Optional grammers **user** session, only for `/index` preview |
| `PORT` | `8000` by default; configure the same HTTP port in Koyeb |

Keep your Python service settings in place. `AUTH_CHANNEL`, `AUTH_USERS`, captions (`CUSTOM_FILE_CAPTION`), and delete timers such as `FILE_AUTO_DELETE_SECONDS` do not take effect in the staging bot because it does not deliver files or service public chats. Do not copy over a Pyrogram `SESSION`, `STRING_SESSION`, or `.session` file; the session formats differ. The bot token itself does not need to be converted.

### Optional user session

On a trusted local machine, with `API_ID` and `API_HASH` exported, run:

```sh
cargo run --locked --manifest-path rust-bot/Cargo.toml --bin grammers-session -- "$HOME/.shobana-indexer.session"
```

Enter your phone number, login code, and two-step verification password if prompted. The tool writes **base64 grammers session data** to the file; copy its single line into the `GRAMMERS_SESSION_B64` Koyeb secret. Base64 is not encryption. Use a dedicated user account with access only to the channels you need. Keep one running replica when using that session and avoid starting another instance of the same session while Koyeb rolls over. If the session leaks, revoke it under Telegram **Settings → Devices** and generate a fresh session.

## 3. Build and deploy to Koyeb

```sh
cargo test --locked --manifest-path rust-bot/Cargo.toml --all-targets
docker build -t shobana-rust:staging rust-bot
```

The Actions workflow builds and tests Rust changes on push and pull request. It publishes an `amd64` image tagged with the commit SHA to `ghcr.io/<owner>/<repo>-rust` on manual dispatch or a push to the configured staging branch. Koyeb pulls the already-built image; it does not build Rust. Build time and image size depend on the dependency versions and cache. No 2–3 minute or 15 MB target is assumed.

1. Once the workflow is available for manual dispatch (it must exist on the default branch), run it to publish a bootstrap image. Leave `RUST_STAGING_BRANCH` unset for the bootstrap so it cannot deploy. Use GitHub's package settings to verify that the repository's workflow may publish it.
2. Create a **separate** Koyeb Docker-image service, initially pointing at that image. Set one replica, stage-only variables and secrets, HTTP port `8000`, and the HTTP health check path `/healthz`. If the package is private, configure Koyeb with pull-only registry credentials. Do not point the workflow at the existing Python service.
3. In the repository, create an Actions environment called `rust-staging`. Limit deployment branches and use an approval rule if available. Set its `KOYEB_SERVICE` variable to the separate staging `app/service`, its `KOYEB_TOKEN` secret to an authorized Koyeb token, and optionally its `KOYEB_REGISTRY_SECRET` variable to the name of the Koyeb registry secret. Set repository variable `RUST_STAGING_BRANCH` to the chosen staging branch. The workflow updates only the image reference (by digest) and waits for Koyeb.
4. Verify `/healthz` returns HTTP 200 once Telegram login and MongoDB text-index checks pass. It is a process health check after startup, not an ongoing MongoDB uptime guarantee. In the staging bot's private chat, try `/start`, `/search <title>`, and optionally `/index <channel-ID-or-@username>` from an admin account. The index preview counts documents in up to 100 recent messages; it does not ingest or change anything. Confirm non-admins and groups get no catalog replies.

## 4. Switchover gate and rollback

Testing with a second token can happen without interrupting the Python service. **Do not switch the production token to this staging build.** Before a production cutover, port and test file delivery using the legacy Pyrogram IDs and file references (a staging bot using a different token cannot establish that they’re reusable), the full indexing/write path, authorization/force-subscribe, captions, delete timers, and any other features/backends your deployment uses. Benchmark search and memory on realistic data. Keep the old service configuration, Telegram session, database backup, and deployable Python revision. There is no database schema migration in this phase.

For a future feature-complete build, rehearse the handoff and rollback: stop the old worker before assigning the production token to the replacement, then immediately verify delivery and authorization. If it fails, stop Rust before restarting Python. Plan for a brief handoff window; zero downtime with the same bot identity has not been verified.
