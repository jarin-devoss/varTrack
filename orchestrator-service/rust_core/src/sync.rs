// vartrack_core/src/sync.rs
//
// Sync helpers: content hashing for change detection.
//
// BLAKE3 is used because it is:
//   - Faster than SHA-256 on modern hardware
//   - Parallel (tree hash, scales with data size)
//   - Cryptographically secure

use pyo3::prelude::*;

/// Compute a BLAKE3 content hash of a byte string.
/// Returns a 64-char lowercase hex string.
pub fn content_hash(data: &[u8]) -> String {
    blake3::hash(data).to_hex().to_string()
}

/// Python binding: compute BLAKE3 hash of content string or bytes.
/// Returns lowercase hex string (64 chars).
#[pyfunction]
pub fn py_content_hash(data: &Bound<'_, PyAny>) -> PyResult<String> {
    if let Ok(s) = data.extract::<String>() {
        Ok(content_hash(s.as_bytes()))
    } else if let Ok(b) = data.extract::<Vec<u8>>() {
        Ok(content_hash(&b))
    } else {
        Err(pyo3::exceptions::PyTypeError::new_err(
            "expected str or bytes",
        ))
    }
}
