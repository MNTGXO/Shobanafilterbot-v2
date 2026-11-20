FROM rust:1.88-bookworm AS build
WORKDIR /src
COPY Cargo.toml Cargo.lock ./
COPY src ./src
RUN cargo test --locked --all-targets \
    && cargo build --locked --release --bin shobana-rust

FROM debian:bookworm-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /src/target/release/shobana-rust /usr/local/bin/shobana-rust
USER 65532:65532
ENV PORT=8000
EXPOSE 8000
CMD ["/usr/local/bin/shobana-rust"]
