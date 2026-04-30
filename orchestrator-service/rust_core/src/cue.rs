// etl-core/src/cue.rs
//
// CUE schema validation helpers.
//
// CUE is invoked as an external process (cue vet / cue export).  This module
// wraps those calls with structured error types and exposes them to Python.
//
// Validation flow:
//   1. Write the flattened JSON doc to a temp file
//   2. Run `cue vet <schema.cue> <doc.json>` as a subprocess
//   3. Parse stdout / stderr and return structured results
//
// Multi-tenant support: each tenant schema lives in its own directory under
// the cloned registry root.  The caller passes the schema path explicitly.

use std::path::PathBuf;
use std::process::Command;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;

use pyo3::prelude::*;
use pyo3::types::PyDict;

// ─── Error types ─────────────────────────────────────────────────────────────

#[derive(Debug, Clone)]
pub struct CueValidationError {
    pub message: String,
    pub path: Option<String>,
}

#[derive(Debug, Clone)]
pub struct CueValidationResult {
    pub valid: bool,
    pub errors: Vec<CueValidationError>,
    pub schema_path: String,
}

impl CueValidationResult {
    fn success(schema_path: &str) -> Self {
        CueValidationResult {
            valid: true,
            errors: vec![],
            schema_path: schema_path.to_string(),
        }
    }

    fn failure(schema_path: &str, errors: Vec<CueValidationError>) -> Self {
        CueValidationResult {
            valid: false,
            errors,
            schema_path: schema_path.to_string(),
        }
    }
}

// ─── Core logic ───────────────────────────────────────────────────────────────

/// Validate `json_content` against the CUE schema at `schema_path`.
///
/// Writes json_content to a tempfile, invokes `cue vet`, collects output.
/// Returns `Err` only on I/O failures; validation errors are in the `Result`.
pub fn validate_with_cue(
    schema_path: &str,
    json_content: &str,
) -> std::io::Result<CueValidationResult> {
    use std::io::Write;

    // Write content to a temporary file
    let mut tmp = tempfile_path();
    std::fs::create_dir_all(tmp.parent().unwrap_or(&PathBuf::from("/tmp")))?;
    let mut f = std::fs::File::create(&tmp)?;
    f.write_all(json_content.as_bytes())?;
    drop(f);

    // cue vet <schema.cue> <data.json> -d '#Config' (schema root definition)
    // Spawn with a timeout so a stuck `cue` process doesn't block the worker thread.
    const CUE_TIMEOUT: Duration = Duration::from_secs(30);
    let mut child = Command::new("cue")
        .args(["vet", schema_path, tmp.to_str().unwrap_or("/tmp/cue_data.json")])
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .spawn();

    // Clean up temp file regardless of subprocess outcome
    let _ = std::fs::remove_file(&tmp);

    let output = match child {
        Err(e) => Err(e),
        Ok(mut c) => {
            // Poll with a busy-wait + sleep rather than a blocking wait_with_output
            // to enforce the timeout without an external crate.
            let start = std::time::Instant::now();
            loop {
                match c.try_wait() {
                    Ok(Some(_)) => break c.wait_with_output().map_err(Into::into),
                    Ok(None) if start.elapsed() >= CUE_TIMEOUT => {
                        let _ = c.kill();
                        // Reap the child after kill() to release the process table entry.
                        let _ = c.wait();
                        return Ok(CueValidationResult::failure(
                            schema_path,
                            vec![CueValidationError {
                                message: format!("cue vet timed out after {}s", CUE_TIMEOUT.as_secs()),
                                path: None,
                            }],
                        ));
                    }
                    Ok(None) => std::thread::sleep(Duration::from_millis(50)),
                    Err(e) => break Err(e),
                }
            }
        }
    };

    match output {
        Ok(out) => {
            if out.status.success() {
                Ok(CueValidationResult::success(schema_path))
            } else {
                let stderr = String::from_utf8_lossy(&out.stderr);
                let errors = parse_cue_errors(&stderr);
                Ok(CueValidationResult::failure(schema_path, errors))
            }
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            // Return valid: false so the caller knows validation could not run.
            Ok(CueValidationResult {
                valid: false,
                errors: vec![CueValidationError {
                    message: "cue binary not found in PATH – validation could not run; \
                              install cue or set SKIP_CUE_VALIDATION=1 to suppress".to_string(),
                    path: None,
                }],
                schema_path: schema_path.to_string(),
            })
        }
        Err(e) => Err(e),
    }
}

/// Parse cue vet stderr lines into structured errors.
fn parse_cue_errors(stderr: &str) -> Vec<CueValidationError> {
    stderr
        .lines()
        .filter(|l| !l.trim().is_empty())
        .map(|line| {
            // Typical format: `path.to.field: some error message`
            if let Some((path, msg)) = line.split_once(':') {
                CueValidationError {
                    message: msg.trim().to_string(),
                    path: Some(path.trim().to_string()),
                }
            } else {
                CueValidationError {
                    message: line.to_string(),
                    path: None,
                }
            }
        })
        .collect()
}

/// Monotonically increasing counter for unique temp file names within a process.
static TMP_COUNTER: AtomicU64 = AtomicU64::new(0);

fn tempfile_path() -> PathBuf {
    // Combine thread ID + atomic counter for a unique path per invocation under concurrent use.
    let tid = std::thread::current().id();
    let seq = TMP_COUNTER.fetch_add(1, Ordering::Relaxed);
    std::env::temp_dir().join(format!("etl_cue_validate_{:?}_{}.json", tid, seq))
}

// ─── PyO3 bindings ────────────────────────────────────────────────────────────

/// Python: validate_cue(schema_path: str, json_content: str) -> dict
///
/// Returns:
///   {
///     "valid":       bool,
///     "schema_path": str,
///     "errors": [{"message": str, "path": str | None}, ...]
///   }
#[pyfunction]
fn py_validate_cue(
    py: Python<'_>,
    schema_path: &str,
    json_content: &str,
) -> PyResult<PyObject> {
    let result = validate_with_cue(schema_path, json_content)
        .map_err(|e| pyo3::exceptions::PyIOError::new_err(e.to_string()))?;

    let out = PyDict::new(py);
    out.set_item("valid", result.valid)?;
    out.set_item("schema_path", &result.schema_path)?;

    let errors_list: Vec<PyObject> = result
        .errors
        .iter()
        .map(|e| -> PyResult<PyObject> {
            let d = PyDict::new(py);
            d.set_item("message", &e.message)?;
            match &e.path {
                Some(p) => d.set_item("path", p)?,
                None => d.set_item("path", py.None())?,
            }
            Ok(d.into())
        })
        .collect::<PyResult<_>>()?;

    out.set_item("errors", errors_list)?;
    Ok(out.into())
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(py_validate_cue, m)?)?;
    Ok(())
}

// ─── Tests ────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_cue_errors_with_path() {
        let stderr = "database.host: conflicting values \"\" and string (mismatched types)\n";
        let errors = parse_cue_errors(stderr);
        assert_eq!(errors.len(), 1);
        assert_eq!(errors[0].path.as_deref(), Some("database.host"));
    }

    #[test]
    fn parse_cue_errors_without_path() {
        let stderr = "some generic error\n";
        let errors = parse_cue_errors(stderr);
        assert_eq!(errors[0].path, None);
    }

    #[test]
    fn validate_missing_binary_returns_skip() {
        // Even without cue installed, validation should not panic
        let result = validate_with_cue("/nonexistent/schema.cue", "{\"key\": \"val\"}");
        // Either Ok (binary not found → skip) or Ok (binary found, schema missing)
        assert!(result.is_ok());
    }
}
