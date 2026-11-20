use anyhow::{Result, anyhow, ensure};
use futures_util::{TryStreamExt, future::try_join_all};
use mongodb::{
    Client, Collection,
    bson::{Document, doc},
    options::ClientOptions,
};
use serde::Deserialize;
use shobana_rust::config::Config;
use std::{sync::Arc, time::Duration};

const LIMIT: i64 = 10;

#[derive(Deserialize)]
pub struct Hit {
    #[serde(rename = "_id")]
    id: String,
    pub file_name: String,
    score: f64,
}

#[derive(Clone)]
pub struct Catalog(Arc<Vec<Collection<Hit>>>);

impl Catalog {
    pub async fn connect(config: &Config) -> Result<Self> {
        let mut collections = Vec::new();
        for (index, (uri, name)) in config.databases.iter().enumerate() {
            let mut opts = ClientOptions::parse(uri).await.map_err(|_| {
                anyhow!(
                    "could not parse or resolve MongoDB URI for catalog {}",
                    index + 1
                )
            })?;
            opts.max_pool_size = Some(4);
            opts.server_selection_timeout = Some(Duration::from_secs(5));
            let col = Client::with_options(opts)?
                .database(name)
                .collection::<Hit>(&config.collection);
            // MongoDB read users can inspect indexes. Don't let a missing or wrong
            // collection appear healthy just because a query against it returned no rows.
            let mut indexes = col
                .list_indexes()
                .await
                .map_err(|_| anyhow!("could not list MongoDB indexes for catalog {}", index + 1))?;
            let mut has_title_index = false;
            while let Some(model) = indexes.try_next().await.map_err(|_| {
                anyhow!(
                    "could not read MongoDB index metadata for catalog {}",
                    index + 1
                )
            })? {
                if model
                    .options
                    .and_then(|options| options.weights)
                    .is_some_and(|weights| weights.contains_key("file_name"))
                {
                    has_title_index = true;
                    break;
                }
            }
            ensure!(
                has_title_index,
                "MongoDB catalog {} needs a text index on file_name",
                index + 1
            );
            // Check the index also supports this query (e.g. no required compound prefix).
            col.find(search_filter("\"shobana rust readiness probe\""))
                .limit(1).max_time(Duration::from_secs(3)).await
                .map_err(|_| anyhow!("MongoDB catalog {} is not searchable; verify connectivity, read access and the text index", index + 1))?;
            collections.push(col);
        }
        Ok(Self(Arc::new(collections)))
    }

    pub async fn search(&self, phrase: &str) -> Result<Vec<Hit>> {
        let parts = try_join_all(self.0.iter().map(|col| async move {
            col.find(search_filter(phrase))
                .sort(doc! { "score": { "$meta": "textScore" }, "_id": 1 })
                .projection(doc! { "_id": 1, "file_name": 1, "score": { "$meta": "textScore" } })
                .limit(LIMIT)
                .batch_size(LIMIT as u32)
                .max_time(Duration::from_secs(3))
                .await?
                .try_collect::<Vec<Hit>>()
                .await
        }))
        .await?;
        let mut hits: Vec<_> = parts.into_iter().flatten().collect();
        hits.sort_by(|a, b| b.score.total_cmp(&a.score).then_with(|| a.id.cmp(&b.id)));
        hits.truncate(LIMIT as usize);
        Ok(hits)
    }
}

fn search_filter(phrase: &str) -> Document {
    doc! { "$text": { "$search": phrase, "$language": "none" } }
}
