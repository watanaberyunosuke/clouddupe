use unicode_normalization::UnicodeNormalization;

/// Map rclone hash names across versions ("SHA-1", "DropboxHash", "md5").
pub fn normalise_hash_type(key: &str) -> String {
    let k: String = key
        .to_lowercase()
        .chars()
        .filter(|c| *c != '-' && *c != '_')
        .collect();
    match k.strip_suffix("hash") {
        Some(stripped) if !stripped.is_empty() => stripped.to_string(),
        _ => k,
    }
}

/// Lower-case, NFC-normalise and strip common copy suffixes so that
/// "Beach (1).JPG", "beach - Copy.jpg" and "Copy of beach.jpg" compare equal.
pub fn normalise_name(name: &str) -> String {
    let n: String = name.nfc().collect::<String>().to_lowercase();
    let n = n.trim();
    let (stem, ext) = match n.rfind('.') {
        Some(i) if i > 0 => (&n[..i], Some(&n[i + 1..])),
        _ => (n, None),
    };
    let mut stem = strip_copy_suffix(stem).trim().to_string();
    if let Some(rest) = stem.strip_prefix("copy of") {
        if rest.starts_with(char::is_whitespace) {
            stem = rest.trim_start().to_string();
        }
    }
    match ext {
        Some(e) => format!("{stem}.{e}"),
        None => stem,
    }
}

/// Mirrors the Python CLI's suffix rule:
/// ` (n)`, ` - copy`, ` - copy (n)`, ` copy`, ` copy n`.
fn strip_copy_suffix(stem: &str) -> &str {
    let s = stem.trim_end();

    // "(n)" with optional preceding "- copy"
    if let Some(inner) = s.strip_suffix(')') {
        if let Some(open) = inner.rfind('(') {
            let digits = &inner[open + 1..];
            if !digits.is_empty() && digits.chars().all(|c| c.is_ascii_digit()) {
                let before = inner[..open].trim_end();
                if let Some(b) = strip_dash_copy(before) {
                    return b;
                }
                return before;
            }
        }
    }

    // " copy n"
    let trimmed_digits = s.trim_end_matches(|c: char| c.is_ascii_digit());
    if trimmed_digits.len() < s.len() && trimmed_digits.ends_with(char::is_whitespace) {
        let before = trimmed_digits.trim_end();
        if let Some(b) = before.strip_suffix("copy") {
            if b.ends_with(char::is_whitespace) && !b.trim_end().ends_with('-') {
                return b.trim_end();
            }
        }
    }

    // " - copy" or " copy"
    if let Some(b) = strip_dash_copy(s) {
        return b;
    }
    if let Some(b) = s.strip_suffix("copy") {
        if b.ends_with(char::is_whitespace) {
            return b.trim_end();
        }
    }
    s
}

fn strip_dash_copy(s: &str) -> Option<&str> {
    let b = s.strip_suffix("copy")?.trim_end();
    let b = b.strip_suffix('-')?;
    Some(b.trim_end())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hash_type_names_across_rclone_versions() {
        assert_eq!(normalise_hash_type("SHA-1"), "sha1");
        assert_eq!(normalise_hash_type("DropboxHash"), "dropbox");
        assert_eq!(normalise_hash_type("md5"), "md5");
        assert_eq!(normalise_hash_type("quickxor"), "quickxor");
        assert_eq!(normalise_hash_type("hash"), "hash");
    }

    #[test]
    fn copy_suffixes_stripped() {
        assert_eq!(normalise_name("Beach (1).JPG"), "beach.jpg");
        assert_eq!(normalise_name("report - Copy.pdf"), "report.pdf");
        assert_eq!(normalise_name("report - Copy (2).pdf"), "report.pdf");
        assert_eq!(normalise_name("notes copy.txt"), "notes.txt");
        assert_eq!(normalise_name("notes copy 3.txt"), "notes.txt");
        assert_eq!(normalise_name("Copy of notes.txt"), "notes.txt");
        assert_eq!(normalise_name("README"), "readme");
        assert_eq!(normalise_name(".bashrc"), ".bashrc");
        assert_eq!(normalise_name("photocopy.txt"), "photocopy.txt");
        assert_eq!(normalise_name("version 2.txt"), "version 2.txt");
    }

    #[test]
    fn unicode_forms_compare_equal() {
        // "é" precomposed vs "e" + combining acute
        assert_eq!(normalise_name("caf\u{e9}.txt"), normalise_name("cafe\u{301}.txt"));
    }
}
