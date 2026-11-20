#![allow(deprecated)] // Match the bot's grammers 0.8 session format.

use anyhow::{Context, Result, anyhow, ensure};
use base64::{Engine as _, engine::general_purpose::STANDARD};
use grammers_client::{Client, SignInError};
use grammers_mtsender::SenderPool;
use grammers_session::storages::TlSession;
use shobana_rust::config::{api_id, required};
use std::{
    env,
    fs::OpenOptions,
    io::{IsTerminal, Write},
    path::PathBuf,
    sync::Arc,
};

#[tokio::main(flavor = "current_thread")]
async fn main() -> Result<()> {
    ensure!(
        std::io::stdin().is_terminal(),
        "run this tool locally in a private terminal"
    );
    let mut args = env::args_os().skip(1);
    let output = PathBuf::from(
        args.next()
            .context("usage: grammers-session <output.session>")?,
    );
    ensure!(
        args.next().is_none() && output.extension().is_some_and(|ext| ext == "session"),
        "usage: grammers-session <output.session>"
    );
    ensure!(
        !output.exists(),
        "refusing to overwrite an existing session file"
    );
    let (api_id, api_hash) = (api_id()?, required("API_HASH")?);
    let session = Arc::new(TlSession::new());
    let pool = SenderPool::new(Arc::clone(&session), api_id);
    let client = Client::new(&pool);
    let SenderPool { runner, handle, .. } = pool;
    tokio::spawn(runner.run());

    let phone = rpassword::prompt_password("Telegram phone number (international format): ")?;
    let token = client
        .request_login_code(phone.trim(), &api_hash)
        .await
        .map_err(|_| anyhow!("Telegram did not accept the login code request"))?;
    let code = rpassword::prompt_password("Telegram login code: ")?;
    match client.sign_in(&token, code.trim()).await {
        Ok(_) => {}
        Err(SignInError::PasswordRequired(token)) => {
            let password = rpassword::prompt_password("Telegram two-step verification password: ")?;
            client
                .check_password(token, password)
                .await
                .map_err(|_| anyhow!("Telegram rejected the two-step login"))?;
        }
        Err(_) => return Err(anyhow!("Telegram sign-in failed")),
    }
    let write_session = || -> Result<()> {
        let mut options = OpenOptions::new();
        options.write(true).create_new(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.mode(0o600);
        }
        let mut file = options.open(&output)?;
        writeln!(file, "{}", STANDARD.encode(session.save()))?;
        file.sync_all()?;
        Ok(())
    };
    if let Err(error) = write_session() {
        let _ = client.sign_out().await;
        return Err(error.context("could not save the new session"));
    }
    handle.quit();
    eprintln!(
        "Session saved. Store its base64 contents as the GRAMMERS_SESSION_B64 secret. Do not share the file."
    );
    Ok(())
}
