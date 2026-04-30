// vartrack_core/src/merge.rs
//
// Variables-map merging and environment resolution.
//
// Resolution order (exactly as specified):
//   1. base variables_map  (from Rule.variables_map)
//   2. env via branch_map  (branch → env name)
//   3. env via file_path_map  (if branch_map miss)
//   4. env_as_branch / env_as_pr / env_as_tags flags
//
// The caller passes already-resolved env string; this module handles the
// precedence chain and placeholder substitution {env}, {branch}, {repo} etc.

use std::collections::HashMap;

use pyo3::prelude::*;
use pyo3::types::PyDict;

// ── Internal ──────────────────────────────────────────────────────────────────

/// Merge two variable maps, right overrides left.
pub fn merge_variables(
    base: HashMap<String, String>,
    override_map: HashMap<String, String>,
) -> HashMap<String, String> {
    let mut result = base;
    for (k, v) in override_map {
        result.insert(k, v);
    }
    result
}

/// Resolve `{key}` placeholders in a template using the provided context.
///
/// Context keys: env, branch, repo, tag, pr_number
///
/// Builds the output in a single pass so a substituted value is never
/// rescanned for further replacements.
pub fn resolve_placeholder(template: &str, context: &HashMap<String, String>) -> String {
    let mut result = String::with_capacity(template.len());
    let mut chars = template.char_indices().peekable();

    while let Some((i, ch)) = chars.next() {
        if ch == '{' {
            // Scan for matching '}'
            let start = i + 1;
            let mut end = None;
            let mut tmp = chars.clone();
            while let Some((j, c)) = tmp.next() {
                if c == '}' {
                    end = Some(j);
                    break;
                }
            }
            if let Some(close) = end {
                let key = &template[start..close];
                if let Some(val) = context.get(key) {
                    result.push_str(val);
                    // Advance chars past the closing '}'
                    while let Some(&(j, _)) = chars.peek() {
                        if j <= close {
                            chars.next();
                        } else {
                            break;
                        }
                    }
                    continue;
                }
            }
        }
        result.push(ch);
    }

    result
}

/// Sanitize a raw branch (or tag) name into a safe environment identifier.
///
/// Rules (mirrors Kubernetes DNS subdomain — RFC 1123):
///   - Lowercase all ASCII letters
///   - Replace every character that is not [a-z0-9.-] with '-'
///   - Collapse consecutive '-' into one
///   - Strip leading/trailing '-'
///   - Empty result → "default"
///
/// Examples:
///   "fix/issue-1"        → "fix-issue-1"
///   "feature/MY-Feature" → "feature-my-feature"
///   "release/v1.0.0"     → "release-v1.0.0"
fn sanitize_env_name(name: &str) -> String {
    let mut result = String::with_capacity(name.len());
    let mut prev_dash = true; // suppress leading '-'

    for ch in name.chars() {
        let c = match ch {
            'a'..='z' | '0'..='9' | '.' => ch,
            'A'..='Z' => ch.to_ascii_lowercase(),
            _ => '-',
        };
        if c == '-' {
            if !prev_dash {
                result.push('-');
            }
            prev_dash = true;
        } else {
            result.push(c);
            prev_dash = false;
        }
    }

    // Strip trailing '-'
    let trimmed = result.trim_end_matches('-');
    if trimmed.is_empty() {
        "default".to_owned()
    } else {
        trimmed.to_owned()
    }
}

/// Determine the environment string from branch/tag/PR metadata.
///
/// Priority:
///   1. branch_map[branch]     → env name
///   2. file_path_map keys that match branch → derive env from path
///   3. env_as_branch          → use branch name as env
///   4. env_as_pr              → use pr_number as env
///   5. env_as_tags            → use tag ref as env
///   6. fallback               → "default"
pub fn resolve_env(
    branch: Option<&str>,
    tag: Option<&str>,
    pr_number: Option<&str>,
    branch_map: &HashMap<String, String>,
    file_path_map: &HashMap<String, String>,
    env_as_branch: bool,
    env_as_pr: bool,
    env_as_tags: bool,
) -> String {
    // 1. Explicit branch → env mapping.
    if let Some(b) = branch {
        if let Some(env) = branch_map.get(b) {
            return env.clone();
        }

        // 2. File path map key contains branch reference.
        for (path_template, mapped_env) in file_path_map {
            if path_template.contains(b) {
                if !mapped_env.is_empty() {
                    return mapped_env.clone();
                }
            }
        }

        // 3. env_as_pr (higher priority than env_as_branch).
        if env_as_pr {
            if let Some(pr) = pr_number {
                return format!("pr-{pr}");
            }
        }

        // 4. env_as_branch.
        if env_as_branch {
            return sanitize_env_name(b);
        }
    }

    // 5. env_as_pr when no branch is present (standalone PR ref).
    if env_as_pr {
        if let Some(pr) = pr_number {
            return format!("pr-{pr}");
        }
    }

    // 6. env_as_tags.
    if env_as_tags {
        if let Some(t) = tag {
            return t.trim_start_matches("refs/tags/").to_owned();
        }
    }

    "default".to_owned()
}

/// Apply variable substitution across all values in the resolved document.
/// Replaces {env}, {branch}, {repo}, {tag}, {pr_number} placeholders.
pub fn substitute_variables(
    flat_doc: HashMap<String, String>,
    context: &HashMap<String, String>,
) -> HashMap<String, String> {
    flat_doc
        .into_iter()
        .map(|(k, v)| (k, resolve_placeholder(&v, context)))
        .collect()
}

// ── PyO3 bindings ─────────────────────────────────────────────────────────────

/// Merge two Python dicts, right overrides left. Returns a new dict.
#[pyfunction]
pub fn py_merge_variables(
    py: Python<'_>,
    base: HashMap<String, String>,
    override_map: HashMap<String, String>,
) -> PyResult<PyObject> {
    let merged = merge_variables(base, override_map);
    let d = PyDict::new(py);
    for (k, v) in merged {
        d.set_item(k, v)?;
    }
    Ok(d.into())
}

/// Resolve the environment string from branch/tag/PR metadata and rule maps.
///
/// Returns a single string – the resolved environment name.
#[pyfunction]
#[pyo3(signature = (
    branch = None,
    tag = None,
    pr_number = None,
    branch_map = None,
    file_path_map = None,
    env_as_branch = false,
    env_as_pr = false,
    env_as_tags = false,
))]
pub fn py_resolve_env(
    branch: Option<String>,
    tag: Option<String>,
    pr_number: Option<String>,
    branch_map: Option<HashMap<String, String>>,
    file_path_map: Option<HashMap<String, String>>,
    env_as_branch: bool,
    env_as_pr: bool,
    env_as_tags: bool,
) -> String {
    resolve_env(
        branch.as_deref(),
        tag.as_deref(),
        pr_number.as_deref(),
        &branch_map.unwrap_or_default(),
        &file_path_map.unwrap_or_default(),
        env_as_branch,
        env_as_pr,
        env_as_tags,
    )
}
