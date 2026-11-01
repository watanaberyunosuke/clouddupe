use std::collections::BTreeMap;

use serde::Serialize;
use serde_json::Value;

use crate::normalise::normalise_hash_type;

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct FileEntry {
    pub idx: usize,
    pub remote: String,
    pub path: String,
    pub size: i64,
    pub mod_time: String,
    pub hashes: BTreeMap<String, String>,
}

impl FileEntry {
    pub fn name(&self) -> &str {
        self.path.rsplit('/').next().unwrap_or(&self.path)
    }

    pub fn full_path(&self) -> String {
        if self.remote.ends_with(':') || self.remote.ends_with('/') {
            format!("{}{}", self.remote, self.path)
        } else {
            format!("{}/{}", self.remote, self.path)
        }
    }
}

#[derive(Debug, Clone, Copy, Serialize, PartialEq, Eq, PartialOrd, Ord, Hash)]
#[serde(rename_all = "lowercase")]
pub enum Confidence {
    Confirmed,
    Probable,
    Possible,
}

impl Confidence {
    pub fn as_str(self) -> &'static str {
        match self {
            Confidence::Confirmed => "confirmed",
            Confidence::Probable => "probable",
            Confidence::Possible => "possible",
        }
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct Group {
    pub id: String,
    pub confidence: Confidence,
    pub evidence: Vec<String>,
    pub members: Vec<FileEntry>,
    pub warnings: Vec<String>,
}

impl Group {
    pub fn size(&self) -> i64 {
        self.members.first().map_or(0, |m| m.size)
    }

    pub fn wasted_bytes(&self) -> i64 {
        self.size().max(0) * (self.members.len() as i64 - 1)
    }

    pub fn remotes(&self) -> Vec<String> {
        let mut r: Vec<String> = self.members.iter().map(|m| m.remote.clone()).collect();
        r.sort();
        r.dedup();
        r
    }
}

/// Convert one `rclone lsjson` item. Returns `None` for directories.
/// Empty hash values are dropped (PikPak can return blank MD5s).
pub fn parse_item(item: &Value, remote: &str, idx: usize) -> Option<FileEntry> {
    if item.get("IsDir").and_then(Value::as_bool).unwrap_or(false) {
        return None;
    }
    let mut hashes = BTreeMap::new();
    if let Some(map) = item.get("Hashes").and_then(Value::as_object) {
        for (k, v) in map {
            let val = match v {
                Value::String(s) => s.trim().to_lowercase(),
                Value::Null => continue,
                other => other.to_string().to_lowercase(),
            };
            if !val.is_empty() {
                hashes.insert(normalise_hash_type(k), val);
            }
        }
    }
    let path = item
        .get("Path")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .or_else(|| item.get("Name").and_then(Value::as_str))
        .unwrap_or_default()
        .to_string();
    Some(FileEntry {
        idx,
        remote: remote.to_string(),
        path,
        size: item.get("Size").and_then(Value::as_i64).unwrap_or(-1),
        mod_time: item
            .get("ModTime")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string(),
        hashes,
    })
}
