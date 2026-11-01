#!/usr/bin/env python3
"""clouddupe - find duplicate files across cloud drives without downloading them.

Each remote is listed through rclone with the content hashes the provider has
already computed server-side (Dropbox content_hash, PikPak/Drive MD5, etc.).
Files are never downloaded, opened or modified.

Confidence levels
    confirmed  same size and identical server-side content hash
    probable   same size and same normalised name, but no hash type in common
               (or a hash is missing), so content cannot be proven equal
    possible   same size only, no common hash (opt-in, --size-only)
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

VERSION = "0.2.0"

COPY_SUFFIX_RE = re.compile(
    r"(\s*\(\d+\)|\s*-\s*copy(\s*\(\d+\))?|\s+copy(\s+\d+)?)$", re.IGNORECASE
)
SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kmgt]?)i?b?\s*$", re.IGNORECASE)


class ScanError(RuntimeError):
    """Raised when rclone fails to list a remote."""


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #
@dataclass
class FileEntry:
    idx: int
    remote: str
    path: str
    size: int
    mod_time: str
    hashes: dict[str, str]

    @property
    def name(self) -> str:
        return self.path.rsplit("/", 1)[-1]

    @property
    def full_path(self) -> str:
        sep = "" if self.remote.endswith((":", "/")) else "/"
        return f"{self.remote}{sep}{self.path}"


@dataclass
class Group:
    gid: str
    confidence: str
    evidence: list[str]
    members: list[FileEntry]
    warnings: list[str] = field(default_factory=list)

    @property
    def size(self) -> int:
        return self.members[0].size

    @property
    def wasted_bytes(self) -> int:
        return self.size * (len(self.members) - 1)

    @property
    def remotes(self) -> list[str]:
        return sorted({m.remote for m in self.members})


class UnionFind:
    def __init__(self) -> None:
        self.parent: dict[int, int] = {}

    def find(self, x: int) -> int:
        self.parent.setdefault(x, x)
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra

    def groups(self) -> list[list[int]]:
        out: dict[int, list[int]] = defaultdict(list)
        for x in self.parent:
            out[self.find(x)].append(x)
        return [sorted(v) for v in out.values() if len(v) > 1]


# --------------------------------------------------------------------------- #
# Normalisation helpers
# --------------------------------------------------------------------------- #
def normalise_hash_type(key: str) -> str:
    """Map rclone hash names across versions ("SHA-1", "DropboxHash", "md5")."""
    k = key.lower().replace("-", "").replace("_", "")
    if k.endswith("hash") and k != "hash":
        k = k[:-4]
    return k


def normalise_name(name: str) -> str:
    """Case-fold, NFC-normalise and strip common copy suffixes."""
    n = unicodedata.normalize("NFC", name).casefold().strip()
    stem, dot, ext = n.rpartition(".")
    if not dot or not stem:
        stem, ext = n, ""
    stem = COPY_SUFFIX_RE.sub("", stem).strip()
    stem = re.sub(r"^copy of\s+", "", stem)
    return f"{stem}.{ext}" if ext else stem


def parse_size(text: str) -> int:
    m = SIZE_RE.match(text)
    if not m:
        raise argparse.ArgumentTypeError(f"invalid size: {text!r} (e.g. 0, 1, 50M, 2G)")
    mult = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}[m.group(2).lower()]
    return int(float(m.group(1)) * mult)


def human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.2f} {unit}"
        size /= 1024
    return f"{n} B"


def safe_filename(remote: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", remote).strip("_") or "remote"


# --------------------------------------------------------------------------- #
# Listing
# --------------------------------------------------------------------------- #
def parse_item(item: dict, remote: str, idx: int) -> FileEntry | None:
    if item.get("IsDir"):
        return None
    raw_hashes = item.get("Hashes") or {}
    hashes = {
        normalise_hash_type(k): str(v).strip().lower()
        for k, v in raw_hashes.items()
        if v and str(v).strip()  # PikPak can return empty MD5s
    }
    return FileEntry(
        idx=idx,
        remote=remote,
        path=item.get("Path") or item.get("Name", ""),
        size=int(item.get("Size", -1)),
        mod_time=item.get("ModTime", ""),
        hashes=hashes,
    )


def iter_lsjson_lines(stream: Iterable[str]) -> Iterator[dict]:
    """Parse rclone lsjson output, which writes one item per line."""
    for raw in stream:
        line = raw.strip()
        if not line or line in ("[", "]"):
            continue
        if line.endswith(","):
            line = line[:-1]
        yield json.loads(line)


def list_remote(target: str, rclone: str, extra_args: list[str]) -> list[dict]:
    cmd = [rclone, "lsjson", "-R", "--files-only", "--hash", "--no-mimetype",
           *extra_args, target]
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as err:
        with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err,
                              text=True, encoding="utf-8") as proc:
            assert proc.stdout is not None
            try:
                items = list(iter_lsjson_lines(proc.stdout))
            except json.JSONDecodeError as exc:
                proc.kill()
                raise ScanError(f"{target}: could not parse rclone output ({exc})") from exc
            code = proc.wait()
        if code != 0:
            err.seek(0)
            tail = err.read().strip().splitlines()[-5:]
            raise ScanError(f"{target}: rclone exited {code}\n  " + "\n  ".join(tail))
    return items


def load_listing(path: Path) -> tuple[str, list[dict]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["remote"], data["items"]


def save_listing(out_dir: Path, remote: str, items: list[dict]) -> Path:
    path = out_dir / f"listing-{safe_filename(remote)}.json"
    payload = {"remote": remote, "scanned_at": now_iso(), "items": items}
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# --------------------------------------------------------------------------- #
# Duplicate detection
# --------------------------------------------------------------------------- #
def compatible(a: FileEntry, b: FileEntry) -> bool:
    """False if a shared hash type proves the contents differ."""
    return all(a.hashes[t] == b.hashes[t] for t in a.hashes.keys() & b.hashes.keys())


def comparable(a: FileEntry, b: FileEntry) -> bool:
    return bool(a.hashes.keys() & b.hashes.keys())


def find_confirmed(files: list[FileEntry], min_size: int) -> list[Group]:
    by_idx = {f.idx: f for f in files}
    uf = UnionFind()
    matched_on: dict[int, set[str]] = defaultdict(set)
    index: dict[tuple[str, str, int], list[int]] = defaultdict(list)

    for f in files:
        if f.size < min_size:
            continue
        for htype, hval in f.hashes.items():
            index[(htype, hval, f.size)].append(f.idx)

    for (htype, _, _), ids in index.items():
        if len(ids) < 2:
            continue
        for other in ids[1:]:
            uf.union(ids[0], other)
        for i in ids:
            matched_on[i].add(htype)

    groups = []
    for ids in uf.groups():
        members = [by_idx[i] for i in ids]
        evidence = sorted(set().union(*(matched_on[i] for i in ids)))
        warnings = []
        for i, a in enumerate(members):
            for b in members[i + 1:]:
                if not compatible(a, b):
                    warnings.append(f"hash conflict between {a.full_path} and {b.full_path}")
        groups.append(Group("", "confirmed", evidence, members, warnings))
    return groups


def find_unverified(files: list[FileEntry], min_size: int, size_only: bool) -> list[Group]:
    """Same size (and name, unless size_only) where no shared hash proves equality."""
    buckets: dict[tuple, list[FileEntry]] = defaultdict(list)
    for f in files:
        if f.size < min_size:
            continue
        key = (f.size,) if size_only else (f.size, normalise_name(f.name))
        buckets[key].append(f)

    label = "possible" if size_only else "probable"
    evidence = ["size"] if size_only else ["size", "name"]
    groups = []
    for bucket in buckets.values():
        if len(bucket) < 2:
            continue
        clusters: list[list[FileEntry]] = []
        for f in bucket:
            for cluster in clusters:
                if all(compatible(f, m) for m in cluster):
                    cluster.append(f)
                    break
            else:
                clusters.append([f])
        for cluster in clusters:
            if len(cluster) < 2:
                continue
            unverified = any(
                not comparable(a, b)
                for i, a in enumerate(cluster) for b in cluster[i + 1:]
            )
            if unverified:
                groups.append(Group("", label, evidence, cluster))
    return groups


def detect(files: list[FileEntry], min_size: int, size_only: bool,
           size_only_min: int) -> list[Group]:
    groups = find_confirmed(files, min_size)
    groups += find_unverified(files, min_size, size_only=False)
    if size_only:
        seen = {frozenset(m.idx for m in g.members) for g in groups}
        for g in find_unverified(files, max(min_size, size_only_min), size_only=True):
            if frozenset(m.idx for m in g.members) not in seen:
                groups.append(g)

    order = {"confirmed": 0, "probable": 1, "possible": 2}
    groups.sort(key=lambda g: (order[g.confidence], -g.wasted_bytes))
    prefix = {"confirmed": "C", "probable": "P", "possible": "S"}
    counters: dict[str, int] = defaultdict(int)
    for g in groups:
        counters[g.confidence] += 1
        g.gid = f"{prefix[g.confidence]}{counters[g.confidence]:05d}"
        g.members.sort(key=lambda m: (m.remote, m.path))
    return groups


# --------------------------------------------------------------------------- #
# Summary and reports
# --------------------------------------------------------------------------- #
def summarise(files: list[FileEntry], groups: list[Group], errors: list[str]) -> dict:
    per_remote: dict[str, dict] = {}
    for f in files:
        r = per_remote.setdefault(f.remote, {"files": 0, "bytes": 0, "hashed": 0,
                                             "hash_types": set()})
        r["files"] += 1
        r["bytes"] += max(f.size, 0)
        if f.hashes:
            r["hashed"] += 1
            r["hash_types"].update(f.hashes)

    remotes = sorted(per_remote)
    comparability = []
    for i, a in enumerate(remotes):
        for b in remotes[i + 1:]:
            common = sorted(per_remote[a]["hash_types"] & per_remote[b]["hash_types"])
            comparability.append({"a": a, "b": b, "common_hash_types": common})

    by_conf: dict[str, dict] = {}
    for g in groups:
        c = by_conf.setdefault(g.confidence, {"groups": 0, "files": 0, "wasted_bytes": 0})
        c["groups"] += 1
        c["files"] += len(g.members)
        c["wasted_bytes"] += g.wasted_bytes

    return {
        "generated_at": now_iso(),
        "tool_version": VERSION,
        "total_files": len(files),
        "remotes": {
            r: {
                "files": v["files"],
                "bytes": v["bytes"],
                "hash_coverage": round(v["hashed"] / v["files"], 4) if v["files"] else 0,
                "hash_types": sorted(v["hash_types"]),
            }
            for r, v in sorted(per_remote.items())
        },
        "comparability": comparability,
        "by_confidence": by_conf,
        "errors": errors,
    }


def group_to_dict(g: Group) -> dict:
    return {
        "id": g.gid,
        "confidence": g.confidence,
        "evidence": g.evidence,
        "size": g.size,
        "count": len(g.members),
        "wasted_bytes": g.wasted_bytes,
        "remotes": g.remotes,
        "warnings": g.warnings,
        "members": [
            {"remote": m.remote, "path": m.path, "full_path": m.full_path,
             "size": m.size, "mod_time": m.mod_time, "hashes": m.hashes}
            for m in g.members
        ],
    }


def write_json(out_dir: Path, summary: dict, groups: list[Group]) -> Path:
    path = out_dir / "report.json"
    payload = {"summary": summary, "groups": [group_to_dict(g) for g in groups]}
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def write_csv(out_dir: Path, groups: list[Group]) -> Path:
    path = out_dir / "duplicates.csv"
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["group_id", "confidence", "evidence", "remote", "path",
                    "size_bytes", "mod_time", "hashes"])
        for g in groups:
            for m in g.members:
                w.writerow([g.gid, g.confidence, "+".join(g.evidence), m.remote, m.path,
                            m.size, m.mod_time,
                            ";".join(f"{k}={v}" for k, v in sorted(m.hashes.items()))])
    return path


HTML_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cloud Duplicate Report</title>
<style>
:root{--bg:#f7f7f5;--fg:#1d1d1b;--muted:#6b6b66;--card:#fff;--line:#e2e2dc;
--ok:#1f7a4d;--warn:#a15c00;--info:#6d4bb5;}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--fg:#ececea;--muted:#9a9a94;
--card:#1f1f1e;--line:#33332f;--ok:#4fbf86;--warn:#e0a24a;--info:#b49cf0;}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:1100px;margin:0 auto;padding:24px 16px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:28px 0 10px}
.muted{color:var(--muted)}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px}
.card b{display:block;font-size:20px}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:8px;overflow:hidden}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}
.scroll{overflow-x:auto}
.controls{display:flex;flex-wrap:wrap;gap:8px;margin:10px 0}
input,select{font:inherit;padding:6px 8px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--fg)}
input{flex:1;min-width:180px}
details{background:var(--card);border:1px solid var(--line);border-radius:8px;margin:8px 0}
summary{cursor:pointer;padding:10px 12px;display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.tag{font-size:12px;padding:1px 8px;border-radius:999px;border:1px solid currentColor}
.confirmed{color:var(--ok)}.probable{color:var(--warn)}.possible{color:var(--info)}
.path{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:13px;word-break:break-all}
.members{padding:0 12px 12px}.warn{color:var(--warn)}
</style></head><body><main>
<h1>Cloud Duplicate Report</h1>
<p class="muted">Generated __GENERATED__ &middot; __TOTAL__ files scanned &middot; nothing was downloaded</p>
<div class="cards">__CARDS__</div>
__ERRORS__
<h2>Hash coverage by remote</h2>
<div class="scroll"><table><tr><th>Remote</th><th>Files</th><th>Size</th><th>With hash</th><th>Hash types</th></tr>__REMOTES__</table></div>
<h2>Can remotes be compared by content?</h2>
<div class="scroll"><table><tr><th>Remote A</th><th>Remote B</th><th>Common hash</th></tr>__COMPARE__</table></div>
<h2>Duplicate groups</h2>
<div class="controls">
<input id="q" type="search" placeholder="Filter by path or remote">
<select id="c"><option value="">All confidence levels</option>
<option value="confirmed">Confirmed</option><option value="probable">Probable</option>
<option value="possible">Possible</option></select></div>
<p id="count" class="muted"></p>
<div id="groups">__GROUPS__</div>
</main>
<script>
const q = document.getElementById('q');
const c = document.getElementById('c');
const items = Array.from(document.querySelectorAll('#groups details'));
const count = document.getElementById('count');
function apply() {
  const term = q.value.trim().toLowerCase();
  const conf = c.value;
  let shown = 0;
  items.forEach((el) => {
    const ok = (conf === '' || el.dataset.conf === conf)
      && (term === '' || el.dataset.text.includes(term));
    el.style.display = ok ? '' : 'none';
    if (ok) { shown += 1; }
  });
  count.textContent = shown + ' of ' + items.length + ' groups shown';
}
q.addEventListener('input', apply);
c.addEventListener('change', apply);
apply();
</script></body></html>
"""


def write_html(out_dir: Path, summary: dict, groups: list[Group]) -> Path:
    e = html.escape
    cards = [f'<div class="card"><span class="muted">Files scanned</span><b>{summary["total_files"]:,}</b></div>']
    for conf in ("confirmed", "probable", "possible"):
        c = summary["by_confidence"].get(conf)
        if c:
            cards.append(
                f'<div class="card"><span class="{conf}">{conf.title()}</span>'
                f'<b>{c["groups"]:,} groups</b>'
                f'<span class="muted">{human_size(c["wasted_bytes"])} reclaimable</span></div>'
            )

    remotes = "".join(
        f"<tr><td class='path'>{e(r)}</td><td>{v['files']:,}</td><td>{human_size(v['bytes'])}</td>"
        f"<td>{v['hash_coverage']:.1%}</td><td>{e(', '.join(v['hash_types']) or 'none')}</td></tr>"
        for r, v in summary["remotes"].items()
    )
    compare = "".join(
        f"<tr><td class='path'>{e(p['a'])}</td><td class='path'>{e(p['b'])}</td>"
        f"<td>{e(', '.join(p['common_hash_types'])) if p['common_hash_types'] else '<span class=warn>none - name and size only</span>'}</td></tr>"
        for p in summary["comparability"]
    ) or "<tr><td colspan=3 class=muted>Only one remote scanned</td></tr>"

    errors = ""
    if summary["errors"]:
        errors = "<h2 class='warn'>Errors</h2><ul>" + "".join(
            f"<li class='path'>{e(x)}</li>" for x in summary["errors"]) + "</ul>"

    blocks = []
    for g in groups:
        text = " ".join([g.gid, *g.remotes, *(m.path for m in g.members)]).lower()
        rows = "".join(
            f"<tr><td class='path'>{e(m.full_path)}</td><td>{e(m.mod_time[:19].replace('T', ' '))}</td>"
            f"<td class='path'>{e(', '.join(f'{k}:{v[:12]}' for k, v in sorted(m.hashes.items())) or '-')}</td></tr>"
            for m in g.members
        )
        warn = "".join(f"<p class='warn'>{e(w)}</p>" for w in g.warnings)
        blocks.append(
            f"<details data-conf='{g.confidence}' data-text='{e(text, quote=True)}'>"
            f"<summary><span class='tag {g.confidence}'>{g.confidence}</span>"
            f"<b>{e(g.members[0].name)}</b>"
            f"<span class='muted'>{len(g.members)} copies &middot; {human_size(g.size)} each &middot; "
            f"matched on {e(' + '.join(g.evidence))} &middot; {g.gid}</span></summary>"
            f"<div class='members'>{warn}<div class='scroll'><table><tr><th>Location</th><th>Modified</th>"
            f"<th>Hashes</th></tr>{rows}</table></div></div></details>"
        )

    page = (HTML_TEMPLATE
            .replace("__GENERATED__", e(summary["generated_at"]))
            .replace("__TOTAL__", f"{summary['total_files']:,}")
            .replace("__CARDS__", "".join(cards))
            .replace("__ERRORS__", errors)
            .replace("__REMOTES__", remotes)
            .replace("__COMPARE__", compare)
            .replace("__GROUPS__", "".join(blocks) or "<p class='muted'>No duplicates found.</p>"))
    path = out_dir / "report.html"
    path.write_text(page, encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="clouddupe",
        description="Find duplicate files across cloud drives using server-side "
                    "content hashes. Read-only; nothing is downloaded.")
    p.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    sub = p.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--out", type=Path, default=Path("clouddupe-report"),
                        help="output directory (default: ./clouddupe-report)")
    common.add_argument("--min-size", type=parse_size, default=1,
                        help="ignore files smaller than this (default: 1 byte, skips empty files)")
    common.add_argument("--size-only", action="store_true",
                        help="also report same-size files with no common hash, regardless of name")
    common.add_argument("--size-only-min", type=parse_size, default=parse_size("50M"),
                        help="minimum size for --size-only matches (default: 50M)")

    scan = sub.add_parser("scan", parents=[common],
                          help="list remotes via rclone and find duplicates")
    scan.add_argument("remotes", nargs="+", metavar="REMOTE",
                      help="rclone remote and optional path, e.g. dropbox: pikpak:Movies")
    scan.add_argument("--rclone", default="rclone", help="path to the rclone binary")
    scan.add_argument("--rclone-arg", action="append", default=[], metavar="ARG",
                      help="extra argument passed to rclone lsjson (repeatable), "
                           "e.g. --rclone-arg=--exclude=*.tmp")
    scan.add_argument("--no-save-listings", action="store_true",
                      help="do not keep raw listings for later re-analysis")

    analyse = sub.add_parser("analyse", parents=[common],
                             help="re-run detection on saved listings without calling rclone")
    analyse.add_argument("listings", nargs="+", type=Path, metavar="LISTING_JSON")

    gui = sub.add_parser("gui", help="open the point-and-click interface in your browser")
    gui.add_argument("--rclone", default="rclone", help="path to the rclone binary")
    gui.add_argument("--port", type=int, default=0, help="port on 127.0.0.1 (default: any free port)")
    gui.add_argument("--no-browser", action="store_true", help="print the URL instead of opening it")
    return p


def run(args: argparse.Namespace) -> int:
    if args.command == "gui":
        if shutil.which(args.rclone) is None:
            print(f"error: rclone not found ({args.rclone}). Install from https://rclone.org/install/",
                  file=sys.stderr)
            return 2
        import gui  # local module; only needed for this subcommand
        return gui.serve(args.rclone, args.port, open_browser=not args.no_browser)

    args.out.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    sources: list[tuple[str, list[dict]]] = []

    if args.command == "scan":
        if shutil.which(args.rclone) is None:
            print(f"error: rclone not found ({args.rclone}). Install from https://rclone.org/install/",
                  file=sys.stderr)
            return 2
        for remote in args.remotes:
            print(f"listing {remote} ...", file=sys.stderr)
            try:
                items = list_remote(remote, args.rclone, args.rclone_arg)
            except ScanError as exc:
                errors.append(str(exc))
                print(f"  failed: {exc}", file=sys.stderr)
                continue
            print(f"  {len(items):,} files", file=sys.stderr)
            if not args.no_save_listings:
                save_listing(args.out, remote, items)
            sources.append((remote, items))
    else:
        for path in args.listings:
            try:
                sources.append(load_listing(path))
            except (OSError, KeyError, json.JSONDecodeError) as exc:
                errors.append(f"{path}: {exc}")

    files: list[FileEntry] = []
    for remote, items in sources:
        for item in items:
            entry = parse_item(item, remote, len(files))
            if entry is not None:
                files.append(entry)

    groups = detect(files, args.min_size, args.size_only, args.size_only_min)
    summary = summarise(files, groups, errors)
    outputs = [write_json(args.out, summary, groups), write_csv(args.out, groups),
               write_html(args.out, summary, groups)]

    print("", file=sys.stderr)
    for r, v in summary["remotes"].items():
        print(f"{r}: {v['files']:,} files, hash coverage {v['hash_coverage']:.1%} "
              f"({', '.join(v['hash_types']) or 'no hashes'})", file=sys.stderr)
    for pair in summary["comparability"]:
        if not pair["common_hash_types"]:
            print(f"note: {pair['a']} and {pair['b']} share no hash type; matches between "
                  f"them are 'probable' (size + name) only", file=sys.stderr)
    for conf, c in summary["by_confidence"].items():
        print(f"{conf}: {c['groups']:,} groups, {human_size(c['wasted_bytes'])} reclaimable",
              file=sys.stderr)
    for path in outputs:
        print(f"wrote {path}", file=sys.stderr)
    return 1 if errors else 0


def main(argv: list[str] | None = None) -> int:
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
