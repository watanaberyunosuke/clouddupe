# AGENTS.md

<!-- handoff:learnings:begin -->
## Session Learnings (auto-updated by handoff)

### Implicit Rules
- Python 3.10+, stdlib only; rclone required on PATH or via --rclone
- Australian/British English in docs; no emojis; concise docs
- JS in HTML report uses ===/!==
- Commits/branches: user's account as author, no AI trace; meaningful branch names
- Run tests: python3 -m unittest discover -s tests

### Key Decisions
- Decision: wrap rclone, not per-provider SDKs — Reason: one auth/listing layer for 70+ backends incl. PikPak; metadata-only listing = no download
- Decision: stdlib-only Python 3.10+ — Reason: zero install friction
- Decision: tiers confirmed/probable/possible — Reason: providers use different hash algorithms; must not overclaim content equality
- Decision: union-find across hash types — Reason: lets matches chain across providers (e.g. PikPak md5 to Drive md5+sha1 to pCloud sha1)
- Decision: same hash type with different value = never grouped, even in probable/possible — Reason: proven different content
- Decision: empty hash values dropped — Reason: PikPak often returns empty MD5
- Decision: normalise hash key names (SHA-1→sha1, DropboxHash→dropbox) — Reason: rclone versions differ
- Decision: skip zero-byte files by default (--min-size 1) — Reason: trivial noise
- Decision: --size-only opt-in, default min 50M — Reason: size-only too noisy for small files
- Decision: never delete — Reason: user reviews report then acts manually

<!-- handoff:learnings:end -->
