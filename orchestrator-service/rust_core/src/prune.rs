// vartrack_core/src/prune.rs
//
// Prune / prune-last diffing.
//
// prune:      keys present in DB but NOT in git → mark for deletion.
// prune_last: same, but deletion is deferred until ALL other writes succeed
//             (caller applies deletes only after the upsert batch commits).
//
// prune_protection: list of glob patterns – keys matching any pattern are
//                   never deleted regardless of sync state.

use std::collections::HashSet;

use pyo3::prelude::*;
use pyo3::types::{PyList, PySet};

// ── Internal ──────────────────────────────────────────────────────────────────

/// Returns the set of keys that exist in `live_keys` but not in `git_keys`.
/// Keys matching any pattern in `protected` are excluded from the result.
pub fn diff_keys(
    git_keys: &HashSet<String>,
    live_keys: &HashSet<String>,
    protected: &[String],
) -> Vec<String> {
    live_keys
        .iter()
        .filter(|k| !git_keys.contains(*k))
        .filter(|k| !is_protected(k, protected))
        .cloned()
        .collect()
}

/// Same as diff_keys but returns in deterministic sorted order.
/// `prune_last` semantics: caller must apply deletes AFTER successful upserts.
pub fn prune_last_keys(
    git_keys: &HashSet<String>,
    live_keys: &HashSet<String>,
    protected: &[String],
) -> Vec<String> {
    let mut keys = diff_keys(git_keys, live_keys, protected);
    keys.sort(); // deterministic order for idempotent re-runs
    keys
}

fn is_protected(key: &str, patterns: &[String]) -> bool {
    patterns.iter().any(|p| glob_match(p, key))
}

/// Minimal glob matching: supports `*` (any segment chars) and `**` (any path).
fn glob_match(pattern: &str, key: &str) -> bool {
    if pattern == "**" {
        return true;
    }

    let pat_parts: Vec<&str> = pattern.split('.').collect();
    let key_parts: Vec<&str> = key.split('.').collect();

    glob_parts_match(&pat_parts, &key_parts)
}

fn glob_parts_match(pattern: &[&str], key: &[&str]) -> bool {
    match (pattern.first(), key.first()) {
        (None, None) => true,
        (None, _) | (_, None) => false,
        (Some(&"**"), _) => {
            // ** can match zero or more parts.
            let rest = &pattern[1..];
            for i in 0..=key.len() {
                if glob_parts_match(rest, &key[i..]) {
                    return true;
                }
            }
            false
        }
        (Some(&"*"), _) => glob_parts_match(&pattern[1..], &key[1..]),
        (Some(p), Some(k)) => {
            if p == k {
                glob_parts_match(&pattern[1..], &key[1..])
            } else {
                false
            }
        }
    }
}

// ── PyO3 bindings ─────────────────────────────────────────────────────────────

/// Compute keys to delete: live DB keys not present in current git state.
///
/// Args:
///     git_keys:   list[str] – keys from current git checkout
///     live_keys:  list[str] – keys currently in MongoDB
///     protected:  list[str] – glob patterns; matching keys are never deleted
///
/// Returns: list[str] – keys to delete
#[pyfunction]
#[pyo3(signature = (git_keys, live_keys, protected = None))]
pub fn py_diff_keys(
    py: Python<'_>,
    git_keys: Vec<String>,
    live_keys: Vec<String>,
    protected: Option<Vec<String>>,
) -> PyResult<PyObject> {
    let git_set: HashSet<String> = git_keys.into_iter().collect();
    let live_set: HashSet<String> = live_keys.into_iter().collect();
    let prot = protected.unwrap_or_default();

    let to_delete = diff_keys(&git_set, &live_set, &prot);
    let list = PyList::new(py, &to_delete)?;
    Ok(list.into())
}

/// Compute keys to delete (prune-last, sorted for deterministic deferred apply).
///
/// Deferred semantics: caller should apply these deletions AFTER all upserts
/// have committed successfully – ensuring atomicity at the operation level.
#[pyfunction]
#[pyo3(signature = (git_keys, live_keys, protected = None))]
pub fn py_prune_last(
    py: Python<'_>,
    git_keys: Vec<String>,
    live_keys: Vec<String>,
    protected: Option<Vec<String>>,
) -> PyResult<PyObject> {
    let git_set: HashSet<String> = git_keys.into_iter().collect();
    let live_set: HashSet<String> = live_keys.into_iter().collect();
    let prot = protected.unwrap_or_default();

    let to_delete = prune_last_keys(&git_set, &live_set, &prot);
    let list = PyList::new(py, &to_delete)?;
    Ok(list.into())
}
