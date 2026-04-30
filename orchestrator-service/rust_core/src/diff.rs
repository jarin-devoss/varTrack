// etl-core/src/diff.rs
//
// Diff engine for flattened key-value maps.
//
// Used by GIT_SMART_REPAIR sync mode to compute the minimal set of changes
// needed to bring a MongoDB collection in sync with the current Git state.
//
// Produces three buckets:
//   added    – keys present in `new` but not in `old`
//   removed  – keys present in `old` but not in `new`
//   changed  – keys present in both but with different values

use ahash::AHashMap;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use serde_json::Value;

/// A single changed entry with both the old and new values.
#[derive(Debug, Clone)]
pub struct DiffChange {
    pub old: Value,
    pub new: Value,
}

#[derive(Debug, Clone)]
pub struct DiffResult {
    pub added: AHashMap<String, Value>,
    pub removed: Vec<String>,
    pub changed: AHashMap<String, DiffChange>,
}

impl DiffResult {
    pub fn is_empty(&self) -> bool {
        self.added.is_empty() && self.removed.is_empty() && self.changed.is_empty()
    }

    pub fn total_changes(&self) -> usize {
        self.added.len() + self.removed.len() + self.changed.len()
    }
}

/// Compute a diff between two flat maps.
/// `old` = current state (e.g., what is in MongoDB)
/// `new` = desired state (e.g., freshly flattened from Git)
pub fn diff(
    old: &AHashMap<String, Value>,
    new: &AHashMap<String, Value>,
) -> DiffResult {
    let mut added: AHashMap<String, Value> = AHashMap::new();
    let mut removed: Vec<String> = Vec::new();
    let mut changed: AHashMap<String, DiffChange> = AHashMap::new();

    // Keys in new but not old → added; in both but different → changed
    for (k, v) in new {
        match old.get(k) {
            None => {
                added.insert(k.clone(), v.clone());
            }
            Some(old_v) if old_v != v => {
                changed.insert(k.clone(), DiffChange {
                    old: old_v.clone(),
                    new: v.clone(),
                });
            }
            _ => {} // unchanged
        }
    }

    // Keys in old but not in new → removed
    for k in old.keys() {
        if !new.contains_key(k) {
            removed.push(k.clone());
        }
    }

    DiffResult { added, removed, changed }
}

// ─── PyO3 bindings ────────────────────────────────────────────────────────────

fn dict_to_map(py: Python<'_>, d: &Bound<'_, PyDict>) -> PyResult<AHashMap<String, Value>> {
    let json_mod = py.import("json")?;
    let s: String = json_mod.call_method1("dumps", (d,))?.extract()?;
    let raw: serde_json::Map<String, Value> = serde_json::from_str(&s)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;
    Ok(raw.into_iter().collect())
}

/// Convert a serde_json Value to its natural string representation.
fn value_to_str(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        Value::Bool(b) => b.to_string(),
        Value::Number(n) => n.to_string(),
        Value::Null => String::new(),
        // For complex types (arrays/objects inside a flat map) fall back to JSON.
        other => serde_json::to_string(other).unwrap_or_default(),
    }
}

fn map_to_dict(py: Python<'_>, map: &AHashMap<String, Value>) -> PyResult<Py<PyDict>> {
    let out = PyDict::new(py);
    for (k, v) in map {
        out.set_item(k, value_to_str(v))?;
    }
    Ok(out.into())
}

fn change_map_to_dict(py: Python<'_>, map: &AHashMap<String, DiffChange>) -> PyResult<Py<PyDict>> {
    let out = PyDict::new(py);
    for (k, change) in map {
        let entry = PyDict::new(py);
        entry.set_item("old", value_to_str(&change.old))?;
        entry.set_item("new", value_to_str(&change.new))?;
        out.set_item(k, entry)?;
    }
    Ok(out.into())
}

/// Python: diff(old: dict, new: dict) -> dict
///
/// Returns:
///   {
///     "added":   {key: new_value, ...},
///     "removed": [key, ...],
///     "changed": {key: new_value, ...},
///     "total_changes": int,
///     "is_empty": bool,
///   }
#[pyfunction]
fn py_diff(
    py: Python<'_>,
    old: &Bound<'_, PyDict>,
    new: &Bound<'_, PyDict>,
) -> PyResult<PyObject> {
    let old_map = dict_to_map(py, old)?;
    let new_map = dict_to_map(py, new)?;

    let result = diff(&old_map, &new_map);

    let out = PyDict::new(py);
    out.set_item("added", map_to_dict(py, &result.added)?)?;

    let removed_list = PyList::new(py, &result.removed)?;
    out.set_item("removed", removed_list)?;

    out.set_item("changed", change_map_to_dict(py, &result.changed)?)?;
    out.set_item("total_changes", result.total_changes())?;
    out.set_item("is_empty", result.is_empty())?;

    Ok(out.into())
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(py_diff, m)?)?;
    Ok(())
}

// ─── Tests ────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn map(pairs: &[(&str, Value)]) -> AHashMap<String, Value> {
        pairs.iter().map(|(k, v)| (k.to_string(), v.clone())).collect()
    }

    #[test]
    fn detects_added() {
        let old = map(&[("a", json!(1))]);
        let new = map(&[("a", json!(1)), ("b", json!(2))]);
        let d = diff(&old, &new);
        assert_eq!(d.added.len(), 1);
        assert!(d.added.contains_key("b"));
        assert!(d.removed.is_empty());
        assert!(d.changed.is_empty());
    }

    #[test]
    fn detects_removed() {
        let old = map(&[("a", json!(1)), ("b", json!(2))]);
        let new = map(&[("a", json!(1))]);
        let d = diff(&old, &new);
        assert!(d.added.is_empty());
        assert_eq!(d.removed, vec!["b"]);
        assert!(d.changed.is_empty());
    }

    #[test]
    fn detects_changed() {
        let old = map(&[("a", json!(1))]);
        let new = map(&[("a", json!(99))]);
        let d = diff(&old, &new);
        assert!(d.added.is_empty());
        assert!(d.removed.is_empty());
        assert_eq!(d.changed["a"].old, json!(1));
        assert_eq!(d.changed["a"].new, json!(99));
    }

    #[test]
    fn empty_diff_when_identical() {
        let m = map(&[("x", json!("same")), ("y", json!(true))]);
        let d = diff(&m, &m);
        assert!(d.is_empty());
    }
}
