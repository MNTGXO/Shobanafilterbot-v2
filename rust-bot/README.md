# Shobana hybrid bot

The root Dockerfile builds one image containing the **Python Telegram bot** and a **private Rust catalog worker**. Python runs all Telegram handlers, loads `info.py`, and owns the only Telegram session. Rust does not log in to Telegram, expose a port, or need a separate `ADMINS`, session, or bot token. `/index` still writes through the existing Python indexing path, and access controls, file delivery, captions, and auto-delete stay in Python. The optional `plugins/Extra` commands were removed.

## Deploy the combined image on Koyeb

1. After a push to `Test`, open **Actions → Build and deploy Shobana hybrid → Summary**. Copy the new tag under **GHCR image published**. The package name still ends in `-rust` for compatibility, but **new tags from the hybrid workflow** contain both languages. Old standalone Rust tags do not. PR runs only build and test.
2. In your Koyeb service under **Source → Docker → Image path**, paste that tag. Select a GHCR pull credential if the package is private. Leave command and entrypoint overrides unset; the image starts `python3 bot.py`.
3. Keep the existing environment variables that `info.py` reads, including `BOT_TOKEN`, `API_ID`, `API_HASH`, `ADMINS`, and your database configuration. Variables placed in Python source are not injected as Koyeb environment variables, but values that `info.py` resolves are the values the hybrid bot uses. Set credentials in Koyeb rather than committing them. You no longer need `RUST_STAGING` or `GRAMMERS_SESSION_B64`; neither is used. Use one bot token in one running service, and do not leave the old standalone Rust instance running against it.
4. Configure HTTP port `8000` (or your existing `PORT` if different) and set the health check to `/healthz`. The health endpoint is served by Python. With MongoDB, run a catalog search and look for `Rust catalog worker served its first MongoDB search` in the service logs; a worker failure is logged separately and the bot falls back to Python search.

Publishing does not require Koyeb credentials. For optional automatic updates, restrict the `rust-staging` Actions environment to `Test` and set its `KOYEB_SERVICE` to your intended test `app/service`, its `KOYEB_TOKEN` secret, and optionally `KOYEB_REGISTRY_SECRET`. Then set the repository Actions variable `RUST_AUTO_DEPLOY=true`. Otherwise deploy manually.

## How the worker is used

- Python resolves the existing catalog configuration from `info.py`, including deduplicated MongoDB shards. On the first uncached eligible search it starts the worker and sends only catalog configuration over a private BSON pipe. The authentication token and admin settings stay in Python.
- Rust executes MongoDB page and count queries, merges shard results, and returns the existing file metadata and pagination to Python. Search matching, including caption configuration, is unchanged. No new index or schema migration is required. Moving the query handling off Python does **not** make MongoDB regex scans faster by itself; benchmark on your catalog before claiming a throughput gain.
- The existing Python cache still serves repeat searches. SQL backends, large pages, and concurrent requests while the worker is busy use the existing Python path. If the worker is unavailable, times out, or returns an invalid reply, Python falls back and waits briefly before trying it again.

Install the pinned Python 3.11 dependencies in a fresh virtual environment for local testing. The Docker build checks the installed dependencies and runs a plugin startup smoke test without connecting to Telegram or MongoDB.

```sh
python -m pip install -r requirements.txt
python -m pip check
python -m unittest discover -s tests
cargo test --locked --manifest-path rust-bot/Cargo.toml
docker build -t shobana-hybrid .
```

Outside Docker, set `RUST_SEARCH_WORKER` to the absolute path to the compiled `shobana-rust` binary to enable it. The Docker image sets that path automatically. For a Python-only rollback using the same hybrid image, set `RUST_SEARCH_WORKER` to an empty value; Telegram and the legacy database paths still work.
