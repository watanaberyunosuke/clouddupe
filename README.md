# Cloud Dupe

Finds duplicate files across cloud drives (Dropbox, PikPak, Google Drive, OneDrive and 60+ others) by comparing the **content hashes your provider already stores**. Files are never downloaded, opened or changed.

Three ways to run it:

| Option | For | Needs |
|---|---|---|
| Desktop app | Everyday use; double-click on macOS or Windows | Nothing; rclone is built in |
| Browser GUI | The same screens without installing an app | Python 3.10+ and rclone |
| Command line | Scripting and scheduled runs | Python 3.10+ and rclone |

## How it works

- Each drive is listed through [rclone](https://rclone.org) with hashes included. Only file metadata is read.
- Matches are grouped by confidence:

| Level | Rule | Meaning |
|---|---|---|
| Confirmed | Same size and identical server-side hash | Identical content, whatever the file name |
| Probable | Same size and same name, no hash type in common (or hash missing) | Likely identical; not proven |
| Possible | Same size only (optional, 50 MiB and up by default) | Worth checking by hand |

- Files sharing a hash type but with different values are always treated as different.
- Name matching ignores case, Unicode form and copy suffixes such as `(1)`, `- Copy` and `Copy of`.
- Read-only: nothing is ever deleted. Review the results, then tidy up in your drive's own app.

## Limitations

Providers use different hash algorithms, so content can only be confirmed between drives that share one ([rclone hash support](https://rclone.org/overview/)):

| Drive | Hash |
|---|---|
| Dropbox | `dropbox` (Dropbox-specific) |
| PikPak | `md5`, often empty for files you uploaded ([rclone PikPak notes](https://rclone.org/pikpak/#limitations)) |
| Google Drive | `md5`, `sha1`, `sha256`; none for native Docs/Sheets/Slides |
| OneDrive | `quickxor` |

- Dropbox vs PikPak can never be confirmed without downloading; those matches stop at Probable.
- Within one drive, renamed copies are Confirmed whenever both files have a hash.
- The results show hash coverage per drive and which drive pairs can be compared.

## Desktop app

### Install

Download the build for your system from the GitHub Actions run (Artifacts) or a release:

- macOS Apple silicon: `aarch64-apple-darwin` `.dmg`
- macOS Intel: `x86_64-apple-darwin` `.dmg`
- Windows: `.msi` or `-setup.exe`

The app is not signed with a paid developer certificate, so the system warns on first open:

- macOS: right-click the app and choose Open, or run `xattr -dr com.apple.quarantine "/Applications/Cloud Dupe.app"`.
- Windows: on the SmartScreen prompt choose More info, then Run anyway.

### Use

1. Add drive: pick a provider, name it, answer its questions. OAuth providers (Dropbox, Google Drive, OneDrive) open your browser to sign in.
2. Tick the drives to scan. Optionally limit each to a folder.
3. Scan, review groups, filter, and export HTML, CSV or JSON.

Drive sign-ins are stored in rclone's standard config file, shared with any rclone you already use:

- macOS: `~/.config/rclone/rclone.conf`
- Windows: `%APPDATA%\rclone\rclone.conf`

Passwords there are obscured, not encrypted. rclone's own config encryption is not supported by the app.

### Build from source

Prerequisites: Rust (stable), Node.js 20+, and on macOS the Xcode command line tools.

```bash
cd app
npm ci
npm run fetch-rclone        # downloads rclone for this machine and verifies its checksum
npx tauri build             # output in ../target/release/bundle/
```

For a drag-to-install DMG on macOS: `npm run dmg` (output in `../target/release/bundle/dmg/`). Add `-- --skip-build` to pack an existing `.app` without rebuilding.

For development with live reload: `npx tauri dev`.

## Browser GUI

```bash
python3 cli/clouddupe.py gui
```

- Opens the same interface in your browser, using your installed rclone.
- Listens on 127.0.0.1 only and requires the random token in the opened URL.
- Options: `--port 8765`, `--no-browser`, `--rclone /path/to/rclone`. Stop with Ctrl+C.
- After changing the frontend, refresh the bundled copy with `cd app && npm run build:web`.

## Command line

```bash
python3 cli/clouddupe.py scan dropbox: pikpak: gdrive:Photos
python3 cli/clouddupe.py scan pikpak: --size-only --size-only-min 1M
python3 cli/clouddupe.py analyse clouddupe-report/listing-*.json
```

Writes `report.html`, `duplicates.csv`, `report.json` and raw listings to `./clouddupe-report/`. Exit codes: `0` success, `1` a drive failed (others still reported), `2` rclone not found.

## Project layout

| Path | Contents |
|---|---|
| `core/` | Rust duplicate detection and report export (used by the desktop app) |
| `app/` | Tauri desktop app: `src/` shared frontend, `src-tauri/` Rust backend |
| `cli/` | Python CLI and browser GUI; `cli/web/` is the built frontend |
| `scripts/` | rclone download and frontend copy helpers |
| `.github/workflows/build.yml` | Tests, then macOS and Windows builds |

The Rust core and Python CLI implement the same rules and share test cases.

## Tests

```bash
cargo test -p clouddupe-core
cd cli && python3 -m unittest discover -s tests   # GUI tests run when rclone is on PATH
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| "bundled rclone not found" when running a source build | Run `npm run fetch-rclone` in `app/` before `npx tauri build` |
| Browser sign-in never finishes | Cancel and retry; check nothing else is using port 53682 (rclone's sign-in callback) |
| A drive shows an auth error during scan | Remove it and add it again to refresh the sign-in |
| PikPak hash coverage is low | Expected; enable "Also flag same-size files" to catch renamed copies |
| HTTP 429 or slow listing | Scan sub-folders instead of whole drives |
| macOS says the app is damaged | Clear the quarantine flag (see Install) |
| `npx tauri build` fails with "couldn't unmount disk - Resource busy" | Use `npm run dmg` instead |

## Licence

MIT. Bundles rclone (MIT); see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
