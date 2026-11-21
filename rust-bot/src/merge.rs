use std::cmp::Ordering;

/// Merge at most the requested prefix of already ordered shard results.
/// `compare` orders the newest item first; ties retain shard/input order.
pub fn merge_page<T>(
    parts: Vec<Vec<T>>,
    offset: usize,
    limit: usize,
    compare: impl Fn(&T, &T) -> Ordering,
) -> Vec<T> {
    // Legacy records (e.g. missing timestamps) can sort differently in BSON
    // than in the app. Preserve the old stable-sort behavior in that case.
    if parts.iter().any(|part| {
        part.windows(2)
            .any(|pair| compare(&pair[0], &pair[1]).is_gt())
    }) {
        let mut all: Vec<_> = parts.into_iter().flatten().collect();
        all.sort_by(compare);
        return all.into_iter().skip(offset).take(limit).collect();
    }

    // There are at most five shards, so scanning their heads has low overhead
    // and avoids globally sorting or allocating a copy of all candidates.
    let mut shards: Vec<_> = parts
        .into_iter()
        .map(|part| part.into_iter().peekable())
        .collect();
    let mut page = Vec::with_capacity(limit);
    for position in 0..offset.saturating_add(limit) {
        let mut best: Option<(usize, &T)> = None;
        for (index, part) in shards.iter_mut().enumerate() {
            if let Some(item) = part.peek() {
                if best.is_none_or(|(_, current)| compare(item, current).is_lt()) {
                    best = Some((index, item));
                }
            }
        }
        let Some((index, _)) = best else { break };
        let row = shards[index].next().expect("head was present");
        if position >= offset {
            page.push(row);
        }
    }
    page
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn combines_five_shards_with_stable_ties() {
        let parts = vec![
            vec![(10, 'a'), (6, 'b')],
            vec![],
            vec![(10, 'c'), (9, 'd')],
            vec![(10, 'e'), (6, 'f')],
            vec![(8, 'g')],
        ];
        let page = merge_page(parts, 1, 4, |a, b| b.0.cmp(&a.0));
        assert_eq!(page, [(10, 'c'), (10, 'e'), (9, 'd'), (8, 'g')]);
    }

    #[test]
    fn uses_stable_sort_for_unordered_legacy_data() {
        let parts = vec![vec![(1, 'a'), (2, 'b')], vec![(2, 'c'), (0, 'd')]];
        let page = merge_page(parts, 0, 3, |a, b| b.0.cmp(&a.0));
        assert_eq!(page, [(2, 'b'), (2, 'c'), (1, 'a')]);
    }

    #[test]
    fn offset_after_end_returns_empty_page() {
        assert!(merge_page(vec![vec![5], vec![3]], 3, 2, |a, b| b.cmp(a)).is_empty());
    }
}
