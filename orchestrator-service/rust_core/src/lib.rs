// vartrack_core/src/lib.rs
//
// Rust ETL core exposed to Python via PyO3.
// Three main capabilities:
//   1. BFS flattening of nested CUE-parsed JSON → flat key/value pairs
//   2. Variable map merging (env resolution: branchMap → filePathMap)
//   3. Prune / prune-last diffing against live DB state

pub mod flatten;
pub mod merge;
pub mod prune;
pub mod sync;
pub mod cue;
pub mod diff;

use pyo3::prelude::*;

/// Register all Python-visible symbols under the `vartrack_core` module.
#[pymodule]
fn vartrack_core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    // ── Flatten ──────────────────────────────────────────────────────────────
    m.add_function(wrap_pyfunction!(flatten::py_flatten_bfs, m)?)?;
    m.add_function(wrap_pyfunction!(flatten::py_flatten_dfs, m)?)?;

    // ── Merge ────────────────────────────────────────────────────────────────
    m.add_function(wrap_pyfunction!(merge::py_merge_variables, m)?)?;
    m.add_function(wrap_pyfunction!(merge::py_resolve_env, m)?)?;

    // ── Prune ────────────────────────────────────────────────────────────────
    m.add_function(wrap_pyfunction!(prune::py_diff_keys, m)?)?;
    m.add_function(wrap_pyfunction!(prune::py_prune_last, m)?)?;

    // ── Sync helpers ─────────────────────────────────────────────────────────
    m.add_function(wrap_pyfunction!(sync::py_content_hash, m)?)?;

    // ── CUE validation ──────────────────────────────────────────────────────
    cue::register(m)?;

    // ── Diff engine ──────────────────────────────────────────────────────────
    diff::register(m)?;

    Ok(())
}
