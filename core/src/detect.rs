use std::collections::{BTreeSet, HashMap, HashSet};

use crate::model::{Confidence, FileEntry, Group};
use crate::normalise::normalise_name;

#[derive(Debug, Clone, Copy)]
pub struct DetectOptions {
    /// Ignore files smaller than this (default 1: skips empty files).
    pub min_size: i64,
    /// Also report same-size files with no shared hash, regardless of name.
    pub size_only: bool,
    /// Minimum size for size-only matches.
    pub size_only_min: i64,
}

impl Default for DetectOptions {
    fn default() -> Self {
        Self { min_size: 1, size_only: false, size_only_min: 50 * 1024 * 1024 }
    }
}

struct UnionFind {
    parent: HashMap<usize, usize>,
}

impl UnionFind {
    fn new() -> Self {
        Self { parent: HashMap::new() }
    }

    fn find(&mut self, x: usize) -> usize {
        let mut root = *self.parent.entry(x).or_insert(x);
        while self.parent[&root] != root {
            root = self.parent[&root];
        }
        let mut cur = x;
        while self.parent[&cur] != root {
            let next = self.parent[&cur];
            self.parent.insert(cur, root);
            cur = next;
        }
        root
    }

    fn union(&mut self, a: usize, b: usize) {
        let (ra, rb) = (self.find(a), self.find(b));
        if ra != rb {
            self.parent.insert(rb, ra);
        }
    }

    fn groups(&mut self) -> Vec<Vec<usize>> {
        let keys: Vec<usize> = self.parent.keys().copied().collect();
        let mut out: HashMap<usize, Vec<usize>> = HashMap::new();
        for k in keys {
            let r = self.find(k);
            out.entry(r).or_default().push(k);
        }
        let mut groups: Vec<Vec<usize>> = out
            .into_values()
            .filter(|v| v.len() > 1)
            .map(|mut v| {
                v.sort_unstable();
                v
            })
            .collect();
        groups.sort();
        groups
    }
}

/// False if a shared hash type proves the contents differ.
fn compatible(a: &FileEntry, b: &FileEntry) -> bool {
    a.hashes
        .iter()
        .all(|(t, v)| b.hashes.get(t).map_or(true, |bv| bv == v))
}

fn comparable(a: &FileEntry, b: &FileEntry) -> bool {
    a.hashes.keys().any(|t| b.hashes.contains_key(t))
}

fn find_confirmed(files: &[FileEntry], min_size: i64) -> Vec<Group> {
    let mut index: HashMap<(&str, &str, i64), Vec<usize>> = HashMap::new();
    for f in files.iter().filter(|f| f.size >= min_size) {
        for (t, v) in &f.hashes {
            index.entry((t.as_str(), v.as_str(), f.size)).or_default().push(f.idx);
        }
    }

    let mut uf = UnionFind::new();
    let mut matched_on: HashMap<usize, BTreeSet<String>> = HashMap::new();
    for ((t, _, _), ids) in &index {
        if ids.len() < 2 {
            continue;
        }
        for other in &ids[1..] {
            uf.union(ids[0], *other);
        }
        for i in ids {
            matched_on.entry(*i).or_default().insert((*t).to_string());
        }
    }

    let by_idx: HashMap<usize, &FileEntry> = files.iter().map(|f| (f.idx, f)).collect();
    uf.groups()
        .into_iter()
        .map(|ids| {
            let members: Vec<FileEntry> = ids.iter().map(|i| by_idx[i].clone()).collect();
            let evidence: BTreeSet<String> = ids
                .iter()
                .flat_map(|i| matched_on.get(i).into_iter().flatten().cloned())
                .collect();
            let mut warnings = Vec::new();
            for (i, a) in members.iter().enumerate() {
                for b in &members[i + 1..] {
                    if !compatible(a, b) {
                        warnings.push(format!(
                            "hash conflict between {} and {}",
                            a.full_path(),
                            b.full_path()
                        ));
                    }
                }
            }
            Group {
                id: String::new(),
                confidence: Confidence::Confirmed,
                evidence: evidence.into_iter().collect(),
                members,
                warnings,
            }
        })
        .collect()
}

/// Same size (and normalised name, unless `size_only`) where no shared hash
/// proves equality. Files a shared hash proves different never share a group.
fn find_unverified(files: &[FileEntry], min_size: i64, size_only: bool) -> Vec<Group> {
    let mut buckets: HashMap<(i64, String), Vec<&FileEntry>> = HashMap::new();
    for f in files.iter().filter(|f| f.size >= min_size) {
        let name = if size_only { String::new() } else { normalise_name(f.name()) };
        buckets.entry((f.size, name)).or_default().push(f);
    }

    let (confidence, evidence) = if size_only {
        (Confidence::Possible, vec!["size".to_string()])
    } else {
        (Confidence::Probable, vec!["size".to_string(), "name".to_string()])
    };

    let mut keys: Vec<_> = buckets.keys().cloned().collect();
    keys.sort();
    let mut groups = Vec::new();
    for key in keys {
        let bucket = &buckets[&key];
        if bucket.len() < 2 {
            continue;
        }
        let mut clusters: Vec<Vec<&FileEntry>> = Vec::new();
        for f in bucket {
            match clusters.iter_mut().find(|c| c.iter().all(|m| compatible(f, m))) {
                Some(c) => c.push(f),
                None => clusters.push(vec![*f]),
            }
        }
        for cluster in clusters.into_iter().filter(|c| c.len() > 1) {
            let unverified = cluster
                .iter()
                .enumerate()
                .any(|(i, a)| cluster[i + 1..].iter().any(|b| !comparable(a, b)));
            if unverified {
                groups.push(Group {
                    id: String::new(),
                    confidence,
                    evidence: evidence.clone(),
                    members: cluster.into_iter().cloned().collect(),
                    warnings: Vec::new(),
                });
            }
        }
    }
    groups
}

pub fn detect(files: &[FileEntry], opts: DetectOptions) -> Vec<Group> {
    let mut groups = find_confirmed(files, opts.min_size);
    groups.extend(find_unverified(files, opts.min_size, false));
    if opts.size_only {
        let seen: HashSet<Vec<usize>> = groups
            .iter()
            .map(|g| {
                let mut ids: Vec<usize> = g.members.iter().map(|m| m.idx).collect();
                ids.sort_unstable();
                ids
            })
            .collect();
        for g in find_unverified(files, opts.min_size.max(opts.size_only_min), true) {
            let mut ids: Vec<usize> = g.members.iter().map(|m| m.idx).collect();
            ids.sort_unstable();
            if !seen.contains(&ids) {
                groups.push(g);
            }
        }
    }

    groups.sort_by(|a, b| {
        a.confidence
            .cmp(&b.confidence)
            .then(b.wasted_bytes().cmp(&a.wasted_bytes()))
    });
    let mut counters: HashMap<Confidence, usize> = HashMap::new();
    for g in &mut groups {
        let n = counters.entry(g.confidence).or_insert(0);
        *n += 1;
        let prefix = match g.confidence {
            Confidence::Confirmed => 'C',
            Confidence::Probable => 'P',
            Confidence::Possible => 'S',
        };
        g.id = format!("{prefix}{:05}", *n);
        g.members.sort_by(|a, b| (&a.remote, &a.path).cmp(&(&b.remote, &b.path)));
    }
    groups
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    use crate::model::parse_item;
    use serde_json::{json, Value};

    pub fn fixtures() -> Vec<(&'static str, Vec<Value>)> {
        vec![
            (
                "dropbox:",
                vec![
                    json!({"Path": "Photos/beach.jpg", "Size": 2048, "ModTime": "2024-01-01T00:00:00Z", "Hashes": {"dropbox": "aaa111"}}),
                    json!({"Path": "Backup/beach (1).jpg", "Size": 2048, "ModTime": "2024-02-01T00:00:00Z", "Hashes": {"dropbox": "AAA111"}}),
                    json!({"Path": "Docs/report.pdf", "Size": 5000, "Hashes": {"dropbox": "bbb222"}}),
                    json!({"Path": "Docs/notes.txt", "Size": 300, "Hashes": {"dropbox": "ccc333"}}),
                    json!({"Path": "Docs", "Size": -1, "IsDir": true}),
                    json!({"Path": "empty.txt", "Size": 0, "Hashes": {"dropbox": "e3b0"}}),
                    json!({"Path": "empty2.txt", "Size": 0, "Hashes": {"dropbox": "e3b0"}}),
                ],
            ),
            (
                "pikpak:",
                vec![
                    json!({"Path": "Inbox/Report.pdf", "Size": 5000, "Hashes": {"md5": "m1"}}),
                    json!({"Path": "Inbox/notes.txt", "Size": 300, "Hashes": {"md5": ""}}),
                    json!({"Path": "Movies/film.mkv", "Size": 900_000_000i64, "Hashes": {"md5": "f00d"}}),
                    json!({"Path": "Movies/old/film.mkv", "Size": 900_000_000i64, "Hashes": {"md5": "beef"}}),
                ],
            ),
            (
                "gdrive:",
                vec![json!({"Path": "Media/film-final.mkv", "Size": 900_000_000i64,
                            "Hashes": {"MD5": "F00D", "SHA-1": "s1", "SHA-256": "s256"}})],
            ),
        ]
    }

    pub fn build(sources: &[(&str, Vec<Value>)]) -> Vec<FileEntry> {
        let mut files = Vec::new();
        for (remote, items) in sources {
            for item in items {
                if let Some(f) = parse_item(item, remote, files.len()) {
                    files.push(f);
                }
            }
        }
        files
    }

    fn paths(g: &Group) -> Vec<String> {
        let mut p: Vec<String> = g.members.iter().map(FileEntry::full_path).collect();
        p.sort();
        p
    }

    fn of(groups: &[Group], c: Confidence) -> Vec<Vec<String>> {
        groups.iter().filter(|g| g.confidence == c).map(paths).collect()
    }

    fn run() -> (Vec<FileEntry>, Vec<Group>) {
        let files = build(&fixtures());
        let groups = detect(&files, DetectOptions { size_only_min: 0, ..Default::default() });
        (files, groups)
    }

    #[test]
    fn directories_and_empty_hashes_skipped() {
        let (files, _) = run();
        assert_eq!(files.len(), 11);
        let notes = files.iter().find(|f| f.full_path() == "pikpak:Inbox/notes.txt").unwrap();
        assert!(notes.hashes.is_empty());
    }

    #[test]
    fn same_provider_hash_match_ignores_name() {
        let (_, groups) = run();
        assert!(of(&groups, Confidence::Confirmed)
            .contains(&vec!["dropbox:Backup/beach (1).jpg".into(), "dropbox:Photos/beach.jpg".into()]));
    }

    #[test]
    fn cross_provider_md5_match() {
        let (_, groups) = run();
        assert!(of(&groups, Confidence::Confirmed)
            .contains(&vec!["gdrive:Media/film-final.mkv".into(), "pikpak:Movies/film.mkv".into()]));
    }

    #[test]
    fn same_name_different_hash_not_grouped() {
        let (_, groups) = run();
        for g in &groups {
            let p = paths(g);
            assert!(!(p.contains(&"pikpak:Movies/film.mkv".into())
                && p.contains(&"pikpak:Movies/old/film.mkv".into())));
        }
    }

    #[test]
    fn no_common_hash_is_probable_only() {
        let (_, groups) = run();
        let probable = of(&groups, Confidence::Probable);
        assert!(probable.contains(&vec!["dropbox:Docs/report.pdf".into(), "pikpak:Inbox/Report.pdf".into()]));
        assert!(probable.contains(&vec!["dropbox:Docs/notes.txt".into(), "pikpak:Inbox/notes.txt".into()]));
    }

    #[test]
    fn empty_files_ignored_by_default() {
        let (_, groups) = run();
        assert!(groups.iter().all(|g| g.size() > 0));
    }

    #[test]
    fn size_only_mode() {
        let mb60 = 60 * 1024 * 1024i64;
        let files = build(&[
            ("a:", vec![json!({"Path": "x.bin", "Size": mb60, "Hashes": {"dropbox": "1"}})]),
            ("b:", vec![json!({"Path": "y.bin", "Size": mb60, "Hashes": {"md5": "2"}})]),
        ]);
        assert!(detect(&files, DetectOptions::default()).is_empty());
        let groups = detect(&files, DetectOptions { size_only: true, ..Default::default() });
        assert_eq!(groups.len(), 1);
        assert_eq!(groups[0].confidence, Confidence::Possible);
        assert_eq!(groups[0].id, "S00001");
    }

    #[test]
    fn wasted_bytes_and_ids() {
        let (_, groups) = run();
        let g = groups.iter().find(|g| paths(g).contains(&"dropbox:Photos/beach.jpg".into())).unwrap();
        assert_eq!(g.wasted_bytes(), 2048);
        assert!(groups.iter().all(|g| g.id.len() == 6));
        assert_eq!(groups[0].confidence, Confidence::Confirmed);
    }
}
