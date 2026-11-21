mod merge;

use anyhow::{Context, Result, anyhow, ensure};
use futures_util::{TryStreamExt, future::try_join_all};
use mongodb::{
    Client, Collection,
    bson::{self, Bson, Document, doc},
    options::ClientOptions,
};
use serde::Deserialize;
use std::{
    io::{self, Read, Write},
    time::Duration,
};

const MAX_FRAME: usize = 4 * 1024 * 1024;
const MAX_OFFSET: i64 = 2_500;
const MAX_PAGE: i64 = 100;
const QUERY_TIMEOUT: Duration = Duration::from_secs(3);

#[derive(Deserialize)]
struct Settings {
    shards: Vec<Shard>,
    collection: String,
}

#[derive(Deserialize)]
struct Shard {
    uri: String,
    database: String,
}

#[derive(Deserialize)]
struct Search {
    filter: Document,
    offset: i64,
    limit: i64,
    fast: bool,
}

#[tokio::main(worker_threads = 2)]
async fn main() -> Result<()> {
    let mut input = io::stdin().lock();
    let mut output = io::stdout().lock();
    let Some(frame) = read_frame(&mut input)? else {
        return Ok(());
    };
    // This private pipe is the only configuration channel. Don't print driver errors
    // along this path; they could include credentials or query content.
    let settings: Settings =
        bson::from_slice(&frame).map_err(|_| anyhow!("invalid worker configuration"))?;
    let collections = open_catalog(settings)
        .await
        .map_err(|_| anyhow!("catalog initialization failed"))?;
    write_frame(&mut output, &doc! { "ok": true })?;

    while let Some(frame) = read_frame(&mut input)? {
        let response = match bson::from_slice::<Search>(&frame) {
            Ok(search) => run_search(&collections, search).await,
            Err(_) => Err(anyhow!("invalid search request")),
        };
        let doc = response.unwrap_or_else(|_| doc! { "ok": false });
        write_frame(&mut output, &doc)?;
    }
    Ok(())
}

async fn open_catalog(settings: Settings) -> Result<Vec<Collection<Document>>> {
    ensure!(
        (1..=5).contains(&settings.shards.len()),
        "expected one to five shards"
    );
    let mut collections = Vec::new();
    for shard in settings.shards {
        let mut opts = ClientOptions::parse(&shard.uri).await?;
        opts.server_selection_timeout = Some(QUERY_TIMEOUT);
        let collection = Client::with_options(opts)?
            .database(&shard.database)
            .collection(&settings.collection);
        collections.push(collection);
    }
    Ok(collections)
}

async fn run_search(collections: &[Collection<Document>], search: Search) -> Result<Document> {
    ensure!(
        (0..=MAX_OFFSET).contains(&search.offset) && (1..=MAX_PAGE).contains(&search.limit),
        "page out of bounds"
    );
    let single = collections.len() == 1;
    let page_size = search.limit + i64::from(search.fast);
    let fetch_limit = page_size + if single { 0 } else { search.offset };
    let search_ref = &search;
    let parts = try_join_all(collections.iter().map(|col| async move {
        let docs = async {
            col.find(search_ref.filter.clone())
                .sort(doc! { "created_at": -1 })
                .projection(doc! {
                    "file_name": 1, "file_size": 1, "file_type": 1,
                    "caption": 1, "created_at": 1,
                })
                .skip(if single { search_ref.offset as u64 } else { 0 })
                .limit(fetch_limit)
                .max_time(QUERY_TIMEOUT)
                .await?
                .try_collect::<Vec<Document>>()
                .await
        };
        if search_ref.fast {
            docs.await.map(|docs| (docs, 0))
        } else {
            let count = async {
                col.count_documents(search_ref.filter.clone())
                    .max_time(QUERY_TIMEOUT)
                    .await
            };
            tokio::try_join!(docs, count)
        }
    }))
    .await?;
    Ok(make_page(parts, &search, single))
}

fn make_page(parts: Vec<(Vec<Document>, u64)>, search: &Search, single: bool) -> Document {
    let (parts, counts): (Vec<_>, Vec<_>) = parts.into_iter().unzip();
    let mut count = counts.into_iter().sum::<u64>();
    let page_size = search.limit as usize + usize::from(search.fast);
    let mut page = if single {
        parts.into_iter().flatten().take(page_size).collect()
    } else {
        merge::merge_page(parts, search.offset as usize, page_size, |a, b| {
            timestamp(b).total_cmp(&timestamp(a))
        })
    };
    let has_more = if search.fast {
        let more = page.len() > search.limit as usize;
        page.truncate(search.limit as usize);
        count = search.offset as u64 + page.len() as u64 + u64::from(more);
        more
    } else {
        ((search.offset + search.limit) as u64) < count
    };
    doc! { "ok": true, "docs": page, "count": count as i64, "has_more": has_more }
}

fn timestamp(doc: &Document) -> f64 {
    match doc.get("created_at") {
        Some(Bson::Double(v)) => *v,
        Some(Bson::Int32(v)) => f64::from(*v),
        Some(Bson::Int64(v)) => *v as f64,
        _ => 0.0,
    }
}

fn read_frame(input: &mut impl Read) -> Result<Option<Vec<u8>>> {
    let mut header = [0; 4];
    loop {
        match input.read(&mut header[..1]) {
            Ok(0) => return Ok(None),
            Ok(_) => break,
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            Err(error) => return Err(error.into()),
        }
    }
    input.read_exact(&mut header[1..])?;
    let size = i32::from_le_bytes(header);
    ensure!(
        (5..=MAX_FRAME as i32).contains(&size),
        "invalid frame length"
    );
    let mut frame = vec![0; size as usize];
    frame[..4].copy_from_slice(&header);
    input
        .read_exact(&mut frame[4..])
        .context("incomplete frame")?;
    Ok(Some(frame))
}

fn write_frame(output: &mut impl Write, document: &Document) -> Result<()> {
    let frame = bson::to_vec(document)?;
    if frame.len() > MAX_FRAME {
        output.write_all(&bson::to_vec(&doc! {"ok": false})?)?;
    } else {
        output.write_all(&frame)?;
    }
    output.flush()?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn merges_shards_preserving_ties_and_fast_pagination() {
        let search = Search {
            filter: doc! {},
            offset: 1,
            limit: 2,
            fast: true,
        };
        let output = make_page(
            vec![
                (
                    vec![
                        doc! { "_id": "first", "created_at": 10.0 },
                        doc! { "_id": "last", "created_at": 3.0 },
                    ],
                    0,
                ),
                (
                    vec![
                        doc! { "_id": "tie", "created_at": 10 },
                        doc! { "_id": "middle", "created_at": 5 },
                    ],
                    0,
                ),
            ],
            &search,
            false,
        );
        let ids: Vec<_> = output
            .get_array("docs")
            .unwrap()
            .iter()
            .map(|d| d.as_document().unwrap().get_str("_id").unwrap())
            .collect();
        assert_eq!(ids, ["tie", "middle"]);
        assert_eq!(output.get_i64("count").unwrap(), 4);
        assert!(output.get_bool("has_more").unwrap());
    }

    #[test]
    fn single_shard_keeps_db_offset_and_exact_count() {
        let search = Search {
            filter: doc! {},
            offset: 10,
            limit: 2,
            fast: false,
        };
        let output = make_page(vec![(vec![doc! {"_id": "last"}], 11)], &search, true);
        assert_eq!(output.get_array("docs").unwrap().len(), 1);
        assert_eq!(output.get_i64("count").unwrap(), 11);
        assert!(!output.get_bool("has_more").unwrap());
    }

    #[test]
    fn accepts_bson_frames_and_rejects_invalid_sizes() {
        let mut buffer = Vec::new();
        let doc = doc! { "offset": 3_i32, "limit": 10_i32, "fast": true, "filter": { "file_name": Bson::RegularExpression(bson::Regex { pattern: "foo".into(), options: "i".into() }) } };
        write_frame(&mut buffer, &doc).unwrap();
        let mut reader = buffer.as_slice();
        let frame = read_frame(&mut reader).unwrap().unwrap();
        let query: Search = bson::from_slice(&frame).unwrap();
        assert_eq!(query.offset, 3);
        assert_eq!(query.limit, 10);
        assert_eq!(query.filter, doc.get_document("filter").unwrap().clone());
        assert!(read_frame(&mut reader).unwrap().is_none());
        assert!(read_frame(&mut i32::MAX.to_le_bytes().as_slice()).is_err());
    }
}
