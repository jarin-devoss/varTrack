// vartrack_core/src/flatten.rs
//
// Non-recursive BFS and DFS flattening of nested JSON objects.
//
// Design:
//   - BFS uses a VecDeque (breadth-first, level-order).
//   - DFS uses an explicit stack (iterative, no recursion depth risk).
//   - Both produce identical output for flat JSON; BFS is preferred for
//     readability of key ordering, DFS is slightly faster for deep trees.
//
// Key format: "parent.child.leaf"  (separator configurable, default ".")
// Arrays are indexed: "items.0.name", "items.1.name"

use std::collections::VecDeque;

use indexmap::IndexMap;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use serde_json::Value;

// ── Internal ──────────────────────────────────────────────────────────────────

/// Work item for iterative traversal.
struct WorkItem {
    prefix: String,
    value: Value,
}

/// BFS flatten: returns an ordered map of dot-path → scalar string value.
pub fn flatten_bfs(root: Value, separator: &str) -> IndexMap<String, String> {
    let mut result = IndexMap::new();
    let mut queue: VecDeque<WorkItem> = VecDeque::new();

    queue.push_back(WorkItem {
        prefix: String::new(),
        value: root,
    });

    while let Some(WorkItem { prefix, value }) = queue.pop_front() {
        match value {
            Value::Object(map) => {
                for (k, v) in map {
                    let new_prefix = if prefix.is_empty() {
                        k
                    } else {
                        format!("{prefix}{separator}{k}")
                    };
                    queue.push_back(WorkItem { prefix: new_prefix, value: v });
                }
            }
            Value::Array(arr) => {
                for (i, v) in arr.into_iter().enumerate() {
                    let new_prefix = if prefix.is_empty() {
                        i.to_string()
                    } else {
                        format!("{prefix}{separator}{i}")
                    };
                    queue.push_back(WorkItem { prefix: new_prefix, value: v });
                }
            }
            Value::Null => {
                result.insert(prefix, String::new());
            }
            other => {
                result.insert(prefix, scalar_to_string(other));
            }
        }
    }

    result
}

/// DFS flatten: produces same key/value pairs but in depth-first order.
pub fn flatten_dfs(root: Value, separator: &str) -> IndexMap<String, String> {
    let mut result = IndexMap::new();
    let mut stack: Vec<WorkItem> = vec![WorkItem {
        prefix: String::new(),
        value: root,
    }];

    while let Some(WorkItem { prefix, value }) = stack.pop() {
        match value {
            Value::Object(map) => {
                // Reverse so that first key ends up on top of stack.
                let items: Vec<_> = map.into_iter().collect();
                for (k, v) in items.into_iter().rev() {
                    let new_prefix = if prefix.is_empty() {
                        k
                    } else {
                        format!("{prefix}{separator}{k}")
                    };
                    stack.push(WorkItem { prefix: new_prefix, value: v });
                }
            }
            Value::Array(arr) => {
                // Collect in forward order first, then push in reverse so
                // the stack's LIFO ordering produces correct forward iteration.
                let pairs: Vec<(usize, Value)> = arr.into_iter().enumerate().collect();
                for (i, v) in pairs.into_iter().rev() {
                    let new_prefix = if prefix.is_empty() {
                        i.to_string()
                    } else {
                        format!("{prefix}{separator}{i}")
                    };
                    stack.push(WorkItem { prefix: new_prefix, value: v });
                }
            }
            Value::Null => {
                result.insert(prefix, String::new());
            }
            other => {
                result.insert(prefix, scalar_to_string(other));
            }
        }
    }

    result
}

fn scalar_to_string(v: Value) -> String {
    match v {
        Value::String(s) => s,
        Value::Bool(b) => b.to_string(),
        Value::Number(n) => n.to_string(),
        _ => String::new(),
    }
}

// ── PyO3 bindings ─────────────────────────────────────────────────────────────

/// Flatten a nested Python dict/list into a flat {str: str} dict using BFS.
///
/// Args:
///     data:      Python dict or JSON string
///     separator: key separator (default ".")
///
/// Returns: dict[str, str]
#[pyfunction]
#[pyo3(signature = (data, separator = "."))]
pub fn py_flatten_bfs(
    py: Python<'_>,
    data: &Bound<'_, PyAny>,
    separator: &str,
) -> PyResult<PyObject> {
    let value = pyany_to_value(data)?;
    let flat = flatten_bfs(value, separator);
    value_map_to_pydict(py, flat)
}

/// Flatten a nested Python dict/list into a flat {str: str} dict using DFS.
#[pyfunction]
#[pyo3(signature = (data, separator = "."))]
pub fn py_flatten_dfs(
    py: Python<'_>,
    data: &Bound<'_, PyAny>,
    separator: &str,
) -> PyResult<PyObject> {
    let value = pyany_to_value(data)?;
    let flat = flatten_dfs(value, separator);
    value_map_to_pydict(py, flat)
}

// ── Helpers ───────────────────────────────────────────────────────────────────

fn pyany_to_value(obj: &Bound<'_, PyAny>) -> PyResult<Value> {
    // Accept either a Python dict/list OR a raw JSON string.
    if let Ok(s) = obj.extract::<String>() {
        serde_json::from_str(&s).map_err(|e| {
            pyo3::exceptions::PyValueError::new_err(format!("invalid JSON: {e}"))
        })
    } else {
        // Convert via JSON round-trip through Python's json module.
        let json_mod = obj.py().import("json")?;
        let json_str: String = json_mod.call_method1("dumps", (obj,))?.extract()?;
        serde_json::from_str(&json_str).map_err(|e| {
            pyo3::exceptions::PyValueError::new_err(format!("serialisation error: {e}"))
        })
    }
}

fn value_map_to_pydict(py: Python<'_>, map: IndexMap<String, String>) -> PyResult<PyObject> {
    let dict = PyDict::new(py);
    for (k, v) in map {
        dict.set_item(k, v)?;
    }
    Ok(dict.into())
}
