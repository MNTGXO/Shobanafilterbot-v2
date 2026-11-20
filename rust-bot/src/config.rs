use anyhow::{Context, Result, ensure};
use std::{collections::HashSet, env};

pub struct Config {
    pub api_id: i32,
    pub api_hash: String,
    pub bot_token: String,
    pub databases: Vec<(String, String)>,
    pub collection: String,
    pub admins: HashSet<i64>,
    pub channels: Vec<String>,
    pub port: u16,
}

pub fn required(name: &str) -> Result<String> {
    let value = env::var(name).unwrap_or_default();
    ensure!(!value.trim().is_empty(), "{name} must be set");
    Ok(value.trim().to_owned())
}

pub fn api_id() -> Result<i32> {
    let id = required("API_ID")?.parse().context("invalid API_ID")?;
    ensure!(id > 0, "API_ID must be positive");
    Ok(id)
}

impl Config {
    pub fn load() -> Result<Self> {
        ensure!(
            required("RUST_STAGING")? == "1",
            "set RUST_STAGING=1 for the read-only staging bot"
        );
        let name = setting("DATABASE_NAME", "Cluster0");
        let mut databases = vec![(required("DATABASE_URI")?, name.clone())];
        for i in 2..=5 {
            let uri = setting(&format!("DATABASE_URI{i}"), "");
            if !uri.is_empty() {
                let entry = (uri, setting(&format!("DATABASE_NAME{i}"), &name));
                if !databases.contains(&entry) {
                    databases.push(entry);
                }
            }
        }
        let admins = required("ADMINS")?
            .split_whitespace()
            .map(str::parse::<i64>)
            .collect::<Result<HashSet<_>, _>>()
            .context("ADMINS must contain numeric Telegram user IDs in staging")?;
        ensure!(
            !admins.is_empty() && admins.iter().all(|id| *id > 0),
            "ADMINS must contain positive Telegram user IDs"
        );
        let port = setting("PORT", "8000").parse().context("invalid PORT")?;
        ensure!(port > 0, "PORT must be nonzero");
        Ok(Self {
            api_id: api_id()?,
            api_hash: required("API_HASH")?,
            bot_token: required("BOT_TOKEN")?,
            databases,
            collection: setting("COLLECTION_NAME", "mn_files"),
            admins,
            channels: setting("CHANNELS", "")
                .split_whitespace()
                .map(channel_key)
                .collect(),
            port,
        })
    }
}

pub fn channel_key(channel: &str) -> String {
    channel.trim_start_matches('@').to_ascii_lowercase()
}

fn setting(name: &str, default: &str) -> String {
    env::var(name)
        .ok()
        .map(|s| s.trim().to_owned())
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| default.to_owned())
}
