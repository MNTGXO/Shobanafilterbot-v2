#![allow(deprecated)] // grammers 0.8 TlSession provides portable session bytes.

mod search;
mod title;

use anyhow::{Context, Result, anyhow, bail, ensure};
use axum::{Router, routing::get};
use base64::{Engine as _, engine::general_purpose::STANDARD};
use grammers_client::{
    Client, Update, UpdatesConfiguration,
    types::{Media, Message, Peer},
};
use grammers_mtsender::SenderPool;
use grammers_session::{defs::PeerKind, storages::TlSession};
use search::Catalog;
use shobana_rust::config::{Config, channel_key};
use std::{env, fmt::Write as _, sync::Arc, time::Duration};
use title::title_phrase;
use tokio::{net::TcpListener, time::timeout};

const INDEX_SAMPLE: usize = 100;

#[tokio::main(worker_threads = 2)]
async fn main() -> Result<()> {
    let config = Config::load()?;
    let listener = TcpListener::bind(("0.0.0.0", config.port)).await?;
    let catalog = timeout(Duration::from_secs(30), Catalog::connect(&config))
        .await
        .context("MongoDB startup timed out")??;

    let pool = SenderPool::new(Arc::new(TlSession::new()), config.api_id);
    let bot = Client::new(&pool);
    let SenderPool {
        runner,
        updates,
        handle,
    } = pool;
    let mut bot_runner = tokio::spawn(runner.run());
    timeout(
        Duration::from_secs(25),
        bot.bot_sign_in(&config.bot_token, &config.api_hash),
    )
    .await
    .context("Telegram bot login timed out")?
    .map_err(|_| anyhow!("Telegram bot login failed; check BOT_TOKEN, API_ID and API_HASH"))?;
    let indexer = load_indexer(config.api_id, !config.channels.is_empty()).await?;
    let username = timeout(Duration::from_secs(10), bot.get_me())
        .await
        .context("Telegram bot identity check timed out")?
        .map_err(|_| anyhow!("could not read the Telegram bot identity"))?
        .username()
        .map(str::to_owned);
    let mut updates = bot.stream_updates(
        updates,
        UpdatesConfiguration {
            catch_up: false,
            ..Default::default()
        },
    );
    let app = Router::new()
        .route("/", get(|| async { "Shobana Rust staging\n" }))
        .route("/healthz", get(|| async { "ok\n" }));

    eprintln!("Read-only staging bot ready on port {}", config.port);
    let result = tokio::select! {
        result = axum::serve(listener, app) => result.context("health server stopped"),
        _ = &mut bot_runner => Err(anyhow!("Telegram connection runner stopped")),
        signal = shutdown_signal() => signal,
        result = async {
            loop {
                let update = updates.next().await?;
                if let Update::NewMessage(message) = update {
                    // Process one bounded request at a time instead of accumulating tasks.
                    if timeout(Duration::from_secs(22), handle_message(&message, &config, &catalog, indexer.as_ref(), username.as_deref())).await.is_err() {
                        eprintln!("Staging command timed out");
                    }
                }
            }
            #[allow(unreachable_code)]
            Ok(())
        } => result,
    };
    handle.quit();
    result
}

async fn shutdown_signal() -> Result<()> {
    #[cfg(unix)]
    {
        let mut sigterm =
            tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?;
        tokio::select! {
            signal = tokio::signal::ctrl_c() => signal?,
            _ = sigterm.recv() => {},
        }
    }
    #[cfg(not(unix))]
    tokio::signal::ctrl_c().await?;
    Ok(())
}

async fn load_indexer(api_id: i32, has_channels: bool) -> Result<Option<Client>> {
    let encoded = match env::var("GRAMMERS_SESSION_B64") {
        Ok(encoded) if !encoded.trim().is_empty() => encoded,
        Ok(_) | Err(env::VarError::NotPresent) => return Ok(None),
        Err(_) => bail!("GRAMMERS_SESSION_B64 is not valid text"),
    };
    ensure!(
        has_channels,
        "set CHANNELS to allow an /index preview when supplying a user session"
    );
    let decoded = STANDARD
        .decode(encoded.trim())
        .context("GRAMMERS_SESSION_B64 is not valid base64")?;
    let session = TlSession::load(&decoded).map_err(|_| {
        anyhow!("invalid grammers session; regenerate it with the local session tool")
    })?;
    let pool = SenderPool::new(Arc::new(session), api_id);
    let client = Client::new(&pool);
    tokio::spawn(pool.runner.run());
    let user = timeout(Duration::from_secs(15), client.get_me())
        .await
        .context("Telegram user session check timed out")?
        .map_err(|_| anyhow!("could not authorize the grammers user session"))?;
    ensure!(
        !user.is_bot(),
        "GRAMMERS_SESSION_B64 must be a user session"
    );
    Ok(Some(client))
}

async fn handle_message(
    message: &Message,
    config: &Config,
    catalog: &Catalog,
    indexer: Option<&Client>,
    username: Option<&str>,
) {
    // Never expose catalog titles to groups, other private users, or anonymous senders.
    let peer = message.peer_id();
    if message.outgoing()
        || peer.kind() != PeerKind::User
        || !config.admins.contains(&peer.bot_api_dialog_id())
    {
        return;
    }
    let text = message.text().trim();
    let (head, args) = text.split_once(char::is_whitespace).unwrap_or((text, ""));
    let command = if let Some((command, target)) = head.split_once('@') {
        if !username.is_some_and(|u| u.eq_ignore_ascii_case(target)) {
            return;
        }
        command
    } else {
        head
    };
    let response = match command {
        "/start" | "/help" => "Shobana Rust staging (read-only).\n/search <title> lists up to 10 catalog matches.\n/index <channel ID or @username> previews recent documents when a user session is configured.".to_owned(),
        "/search" => match title_phrase(args) {
            None => "Usage: /search <title>".to_owned(),
            Some(phrase) => match timeout(Duration::from_secs(8), catalog.search(&phrase)).await {
                Ok(Ok(hits)) if hits.is_empty() => "No indexed matches found.".to_owned(),
                Ok(Ok(hits)) => {
                    let mut reply = String::from("Catalog matches (read-only):\n");
                    for (i, hit) in hits.iter().enumerate() {
                        let name: String = hit.file_name.chars().filter(|c| !c.is_control()).take(150).collect();
                        let _ = writeln!(reply, "{}. {name}", i + 1);
                    }
                    reply
                }
                _ => {
                    eprintln!("Catalog lookup failed");
                    "Search temporarily unavailable.".to_owned()
                }
            },
        },
        "/index" => {
            let target = channel_key(args.trim());
            if target.is_empty() {
                "Usage: /index <allowed channel ID or @username>".to_owned()
            } else if !config.channels.contains(&target) {
                "That channel is not in the staging CHANNELS list.".to_owned()
            } else if let Some(client) = indexer {
                match timeout(Duration::from_secs(15), preview_index(client, &target)).await {
                    Ok(Ok((scanned, docs))) => format!("Read-only preview: {docs} document messages out of {scanned} recent messages. No records written."),
                    _ => "Could not read channel history. Check access and CHANNELS.".to_owned(),
                }
            } else {
                "Index preview requires a grammers user session.".to_owned()
            }
        }
        _ => return,
    };
    if message.respond(response).await.is_err() {
        eprintln!("Could not send Telegram staging reply");
    }
}

async fn preview_index(client: &Client, target: &str) -> Result<(usize, usize)> {
    // Use only channels already visible to the user session; never join chats automatically.
    let mut dialogs = client.iter_dialogs().limit(500);
    while let Some(dialog) = dialogs.next().await? {
        let peer = dialog.peer();
        if !matches!(peer, Peer::Channel(_)) {
            continue;
        }
        if peer.id().bot_api_dialog_id().to_string() != target
            && !peer
                .username()
                .is_some_and(|name| name.eq_ignore_ascii_case(target))
        {
            continue;
        }
        let mut messages = client.iter_messages(peer).limit(INDEX_SAMPLE);
        let (mut scanned, mut docs) = (0, 0);
        while let Some(message) = messages.next().await? {
            scanned += 1;
            if matches!(message.media(), Some(Media::Document(_))) {
                docs += 1;
            }
        }
        return Ok((scanned, docs));
    }
    bail!("channel not present in user-session dialogs")
}
