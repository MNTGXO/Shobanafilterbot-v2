FROM rust:1.88-bookworm AS rust-worker
WORKDIR /build
COPY rust-bot/Cargo.toml rust-bot/Cargo.lock ./
COPY rust-bot/src ./src
RUN cargo test --locked && cargo build --locked --release

FROM python:3.10-bookworm
WORKDIR /app
COPY requirements.txt ./
RUN pip3 install --no-cache-dir -r requirements.txt
COPY . .
COPY --from=rust-worker /build/target/release/shobana-rust /usr/local/bin/shobana-search-worker
ENV RUST_SEARCH_WORKER=/usr/local/bin/shobana-search-worker PORT=8000
RUN python3 -m unittest discover -s tests
EXPOSE 8000
CMD ["python3", "bot.py"]
