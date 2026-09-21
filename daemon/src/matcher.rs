//! Cosine matching and k-of-n voting.
//!
//! This runs in the daemon, never in the worker, because it is the only
//! place templates are allowed to exist in plaintext.

#[derive(Debug, Clone, Copy)]
pub struct MatchOutcome {
    pub accepted: bool,
    pub passes: usize,
    pub considered: usize,
    /// Index into the templates of the best-scoring one, if any.
    /// Lets the caller attribute a match to a named identity without
    /// leaking the score itself.
    pub best_index: Option<usize>,
    /// Kept for the audit log and for tuning. Never returned to a
    /// caller: leaking scores lets an attacker hill-climb toward the
    /// threshold one attempt at a time.
    pub best: f32,
}

/// Cosine similarity against every template, returning the best score
/// and WHICH template produced it.
pub fn best_template(query: &[f32], templates: &[Vec<f32>]) -> (f32, Option<usize>) {
    let mut best = -1.0f32;
    let mut best_ix = None;
    for (ix, t) in templates.iter().enumerate() {
        if t.len() != query.len() {
            continue;
        }
        let dot: f32 = t.iter().zip(query).map(|(a, b)| a * b).sum();
        if dot > best {
            best = dot;
            best_ix = Some(ix);
        }
    }
    (best, best_ix)
}

pub fn cosine_max(query: &[f32], templates: &[Vec<f32>]) -> f32 {
    best_template(query, templates).0
}

/// At least `k` of the last `n` query embeddings must clear `tau`.
pub fn vote(
    queries: &[Vec<f32>],
    templates: &[Vec<f32>],
    tau: f32,
    k: usize,
    n: usize,
) -> MatchOutcome {
    let window: &[Vec<f32>] = if queries.len() > n {
        &queries[queries.len() - n..]
    } else {
        queries
    };
    let mut passes = 0usize;
    let mut best = -1.0f32;
    let mut best_ix = None;
    for q in window {
        let (s, ix) = best_template(q, templates);
        if s > best {
            best = s;
            best_ix = ix;
        }
        if s >= tau {
            passes += 1;
        }
    }
    MatchOutcome {
        accepted: passes >= k,
        passes,
        considered: window.len(),
        best_index: best_ix,
        best,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn unit(v: Vec<f32>) -> Vec<f32> {
        let n: f32 = v.iter().map(|x| x * x).sum::<f32>().sqrt();
        v.into_iter().map(|x| x / n).collect()
    }

    #[test]
    fn cosine_picks_the_best_template() {
        let q = unit(vec![1.0, 0.0, 0.0]);
        let t = vec![unit(vec![0.0, 1.0, 0.0]), unit(vec![0.9, 0.4, 0.0])];
        assert!((cosine_max(&q, &t) - 0.913).abs() < 0.01);
    }

    #[test]
    fn mismatched_dimensions_never_match() {
        // A model swap must fail closed, not compare garbage.
        let q = vec![1.0, 0.0];
        let t = vec![vec![1.0, 0.0, 0.0]];
        assert_eq!(cosine_max(&q, &t), -1.0);
    }

    #[test]
    fn voting_requires_k_passes() {
        let q = unit(vec![1.0, 0.0]);
        let far = unit(vec![0.0, 1.0]);
        let t = vec![q.clone()];
        let queries = vec![q.clone(), far.clone(), q.clone()];
        assert!(!vote(&queries, &t, 0.9, 3, 5).accepted);
        let queries = vec![q.clone(), q.clone(), far, q.clone()];
        assert!(vote(&queries, &t, 0.9, 3, 5).accepted);
    }

    #[test]
    fn empty_queries_are_rejected() {
        assert!(!vote(&[], &[vec![1.0]], 0.5, 1, 5).accepted);
    }
}
