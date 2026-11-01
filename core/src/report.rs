use serde_json::json;

use crate::model::{Confidence, Group};
use crate::summary::Summary;

const HTML_TEMPLATE: &str = include_str!("report_template.html");

pub fn human_size(n: i64) -> String {
    let mut size = n.max(0) as f64;
    for unit in ["B", "KiB", "MiB", "GiB"] {
        if size < 1024.0 {
            return if unit == "B" { format!("{size:.0} B") } else { format!("{size:.2} {unit}") };
        }
        size /= 1024.0;
    }
    format!("{size:.2} TiB")
}

fn group_json(g: &Group) -> serde_json::Value {
    json!({
        "id": g.id,
        "confidence": g.confidence,
        "evidence": g.evidence,
        "size": g.size(),
        "count": g.members.len(),
        "wasted_bytes": g.wasted_bytes(),
        "remotes": g.remotes(),
        "warnings": g.warnings,
        "members": g.members.iter().map(|m| json!({
            "remote": m.remote,
            "path": m.path,
            "full_path": m.full_path(),
            "size": m.size,
            "mod_time": m.mod_time,
            "hashes": m.hashes,
        })).collect::<Vec<_>>(),
    })
}

/// Same shape as the CLI's report.json.
pub fn to_json(summary: &Summary, groups: &[Group]) -> String {
    let payload = json!({
        "summary": summary,
        "groups": groups.iter().map(group_json).collect::<Vec<_>>(),
    });
    serde_json::to_string_pretty(&payload).unwrap_or_default()
}

fn csv_field(s: &str) -> String {
    if s.contains([',', '"', '\n', '\r']) {
        format!("\"{}\"", s.replace('"', "\"\""))
    } else {
        s.to_string()
    }
}

/// One row per file. Starts with a UTF-8 BOM so Excel detects the encoding.
pub fn to_csv(groups: &[Group]) -> String {
    let mut out = String::from("\u{feff}group_id,confidence,evidence,remote,path,size_bytes,mod_time,hashes\r\n");
    for g in groups {
        for m in &g.members {
            let hashes: Vec<String> = m.hashes.iter().map(|(k, v)| format!("{k}={v}")).collect();
            let row = [
                g.id.clone(),
                g.confidence.as_str().to_string(),
                g.evidence.join("+"),
                m.remote.clone(),
                m.path.clone(),
                m.size.to_string(),
                m.mod_time.clone(),
                hashes.join(";"),
            ];
            let row: Vec<String> = row.iter().map(|f| csv_field(f)).collect();
            out.push_str(&row.join(","));
            out.push_str("\r\n");
        }
    }
    out
}

fn esc(s: &str) -> String {
    s.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
        .replace('\'', "&#x27;")
}

fn thousands(n: usize) -> String {
    let s = n.to_string();
    let mut out = String::new();
    for (i, c) in s.chars().enumerate() {
        if i > 0 && (s.len() - i) % 3 == 0 {
            out.push(',');
        }
        out.push(c);
    }
    out
}

/// Self-contained, offline HTML report.
pub fn to_html(summary: &Summary, groups: &[Group]) -> String {
    let mut cards = format!(
        "<div class=\"card\"><span class=\"muted\">Files scanned</span><b>{}</b></div>",
        thousands(summary.total_files)
    );
    for c in [Confidence::Confirmed, Confidence::Probable, Confidence::Possible] {
        if let Some(s) = summary.by_confidence.get(&c) {
            let name = c.as_str();
            let title = format!("{}{}", name[..1].to_uppercase(), &name[1..]);
            cards.push_str(&format!(
                "<div class=\"card\"><span class=\"{name}\">{title}</span><b>{} groups</b>\
                 <span class=\"muted\">{} reclaimable</span></div>",
                thousands(s.groups),
                human_size(s.wasted_bytes)
            ));
        }
    }

    let remotes: String = summary
        .remotes
        .iter()
        .map(|(r, v)| {
            let types = if v.hash_types.is_empty() { "none".to_string() } else { v.hash_types.join(", ") };
            format!(
                "<tr><td class='path'>{}</td><td>{}</td><td>{}</td><td>{:.1}%</td><td>{}</td></tr>",
                esc(r),
                thousands(v.files),
                human_size(v.bytes),
                v.hash_coverage * 100.0,
                esc(&types)
            )
        })
        .collect();

    let mut compare: String = summary
        .comparability
        .iter()
        .map(|p| {
            let common = if p.common_hash_types.is_empty() {
                "<span class='warn'>none - name and size only</span>".to_string()
            } else {
                esc(&p.common_hash_types.join(", "))
            };
            format!(
                "<tr><td class='path'>{}</td><td class='path'>{}</td><td>{}</td></tr>",
                esc(&p.a),
                esc(&p.b),
                common
            )
        })
        .collect();
    if compare.is_empty() {
        compare = "<tr><td colspan=3 class=muted>Only one remote scanned</td></tr>".to_string();
    }

    let errors = if summary.errors.is_empty() {
        String::new()
    } else {
        let items: String = summary
            .errors
            .iter()
            .map(|e| format!("<li class='path'>{}</li>", esc(e)))
            .collect();
        format!("<h2 class='warn'>Errors</h2><ul>{items}</ul>")
    };

    let mut blocks = String::new();
    for g in groups {
        let mut text = vec![g.id.clone()];
        text.extend(g.remotes());
        text.extend(g.members.iter().map(|m| m.path.clone()));
        let text = text.join(" ").to_lowercase();
        let rows: String = g
            .members
            .iter()
            .map(|m| {
                let hashes: Vec<String> = m
                    .hashes
                    .iter()
                    .map(|(k, v)| format!("{k}:{}", &v[..v.len().min(12)]))
                    .collect();
                let hashes = if hashes.is_empty() { "-".to_string() } else { hashes.join(", ") };
                let modified: String = m.mod_time.chars().take(19).collect::<String>().replace('T', " ");
                format!(
                    "<tr><td class='path'>{}</td><td>{}</td><td class='path'>{}</td></tr>",
                    esc(&m.full_path()),
                    esc(&modified),
                    esc(&hashes)
                )
            })
            .collect();
        let warn: String = g.warnings.iter().map(|w| format!("<p class='warn'>{}</p>", esc(w))).collect();
        let conf = g.confidence.as_str();
        blocks.push_str(&format!(
            "<details data-conf='{conf}' data-text='{}'><summary><span class='tag {conf}'>{conf}</span>\
             <b>{}</b><span class='muted'>{} copies &middot; {} each &middot; matched on {} &middot; {}</span>\
             </summary><div class='members'>{warn}<div class='scroll'><table><tr><th>Location</th>\
             <th>Modified</th><th>Hashes</th></tr>{rows}</table></div></div></details>",
            esc(&text),
            esc(g.members.first().map_or("", |m| m.name())),
            g.members.len(),
            human_size(g.size()),
            esc(&g.evidence.join(" + ")),
            g.id
        ));
    }
    if blocks.is_empty() {
        blocks = "<p class='muted'>No duplicates found.</p>".to_string();
    }

    HTML_TEMPLATE
        .replace("__GENERATED__", &esc(&summary.generated_at))
        .replace("__TOTAL__", &thousands(summary.total_files))
        .replace("__CARDS__", &cards)
        .replace("__ERRORS__", &errors)
        .replace("__REMOTES__", &remotes)
        .replace("__COMPARE__", &compare)
        .replace("__GROUPS__", &blocks)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::detect::tests::{build, fixtures};
    use crate::detect::{detect, DetectOptions};
    use crate::summary::summarise;

    #[test]
    fn exports_are_well_formed() {
        let files = build(&fixtures());
        let groups = detect(&files, DetectOptions::default());
        let s = summarise(&files, &groups, &["x: failed".into()], "2026-11-01T00:00:00Z");

        let v: serde_json::Value = serde_json::from_str(&to_json(&s, &groups)).unwrap();
        assert_eq!(v["summary"]["total_files"], 11);
        assert!(v["summary"]["by_confidence"]["confirmed"]["groups"].as_u64().unwrap() >= 2);

        let csv = to_csv(&groups);
        assert!(csv.starts_with("\u{feff}group_id,"));
        assert!(csv.contains("\"Backup/beach (1).jpg\"") || csv.contains("Backup/beach (1).jpg"));

        let html = to_html(&s, &groups);
        assert!(html.contains("Cloud Duplicate Report"));
        assert!(html.contains("x: failed"));
        assert!(!html.contains("__GROUPS__"));
    }

    #[test]
    fn html_escapes_paths() {
        let files = build(&[(
            "a:",
            vec![
                serde_json::json!({"Path": "<script>.txt", "Size": 5, "Hashes": {"md5": "1"}}),
                serde_json::json!({"Path": "b'.txt", "Size": 5, "Hashes": {"md5": "1"}}),
            ],
        )]);
        let groups = detect(&files, DetectOptions::default());
        let s = summarise(&files, &groups, &[], "t");
        let html = to_html(&s, &groups);
        assert!(!html.contains("<script>.txt"));
        assert!(html.contains("&lt;script&gt;.txt"));
    }

    #[test]
    fn sizes_and_csv_quoting() {
        assert_eq!(human_size(512), "512 B");
        assert_eq!(human_size(2048), "2.00 KiB");
        assert_eq!(csv_field("a,b"), "\"a,b\"");
        assert_eq!(csv_field("say \"hi\""), "\"say \"\"hi\"\"\"");
    }
}
