use std::collections::{BTreeMap, BTreeSet};

use serde::Serialize;

use crate::model::{Confidence, FileEntry, Group};

#[derive(Debug, Clone, Serialize)]
pub struct RemoteStats {
    pub files: usize,
    pub bytes: i64,
    /// Share of files with at least one usable hash, 0.0 to 1.0.
    pub hash_coverage: f64,
    pub hash_types: Vec<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ComparablePair {
    pub a: String,
    pub b: String,
    pub common_hash_types: Vec<String>,
}

#[derive(Debug, Clone, Default, Serialize)]
pub struct ConfidenceStats {
    pub groups: usize,
    pub files: usize,
    pub wasted_bytes: i64,
}

#[derive(Debug, Clone, Serialize)]
pub struct Summary {
    pub generated_at: String,
    pub tool_version: String,
    pub total_files: usize,
    pub remotes: BTreeMap<String, RemoteStats>,
    pub comparability: Vec<ComparablePair>,
    pub by_confidence: BTreeMap<Confidence, ConfidenceStats>,
    pub errors: Vec<String>,
}

pub fn summarise(
    files: &[FileEntry],
    groups: &[Group],
    errors: &[String],
    generated_at: &str,
) -> Summary {
    struct Acc {
        files: usize,
        bytes: i64,
        hashed: usize,
        types: BTreeSet<String>,
    }
    let mut acc: BTreeMap<String, Acc> = BTreeMap::new();
    for f in files {
        let a = acc.entry(f.remote.clone()).or_insert(Acc {
            files: 0,
            bytes: 0,
            hashed: 0,
            types: BTreeSet::new(),
        });
        a.files += 1;
        a.bytes += f.size.max(0);
        if !f.hashes.is_empty() {
            a.hashed += 1;
            a.types.extend(f.hashes.keys().cloned());
        }
    }

    let names: Vec<&String> = acc.keys().collect();
    let mut comparability = Vec::new();
    for (i, a) in names.iter().enumerate() {
        for b in &names[i + 1..] {
            comparability.push(ComparablePair {
                a: (*a).clone(),
                b: (*b).clone(),
                common_hash_types: acc[*a].types.intersection(&acc[*b].types).cloned().collect(),
            });
        }
    }

    let mut by_confidence: BTreeMap<Confidence, ConfidenceStats> = BTreeMap::new();
    for g in groups {
        let c = by_confidence.entry(g.confidence).or_default();
        c.groups += 1;
        c.files += g.members.len();
        c.wasted_bytes += g.wasted_bytes();
    }

    Summary {
        generated_at: generated_at.to_string(),
        tool_version: crate::VERSION.to_string(),
        total_files: files.len(),
        remotes: acc
            .into_iter()
            .map(|(k, a)| {
                let coverage = if a.files == 0 { 0.0 } else { a.hashed as f64 / a.files as f64 };
                (
                    k,
                    RemoteStats {
                        files: a.files,
                        bytes: a.bytes,
                        hash_coverage: (coverage * 10_000.0).round() / 10_000.0,
                        hash_types: a.types.into_iter().collect(),
                    },
                )
            })
            .collect(),
        comparability,
        by_confidence,
        errors: errors.to_vec(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::detect::tests::{build, fixtures};
    use crate::detect::{detect, DetectOptions};

    #[test]
    fn comparability_and_coverage() {
        let files = build(&fixtures());
        let groups = detect(&files, DetectOptions::default());
        let s = summarise(&files, &groups, &[], "2026-11-01T00:00:00Z");
        let pair = |a: &str, b: &str| {
            s.comparability
                .iter()
                .find(|p| p.a == a && p.b == b)
                .map(|p| p.common_hash_types.clone())
                .unwrap()
        };
        assert!(pair("dropbox:", "pikpak:").is_empty());
        assert_eq!(pair("gdrive:", "pikpak:"), vec!["md5".to_string()]);
        assert_eq!(s.remotes["pikpak:"].hash_coverage, 0.75);
        assert_eq!(s.total_files, 11);
    }
}
