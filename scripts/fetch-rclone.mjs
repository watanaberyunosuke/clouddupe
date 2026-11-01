#!/usr/bin/env node
// Downloads a pinned rclone release for a Rust target triple, verifies it
// against the release's SHA256SUMS, and places it where Tauri expects a
// sidecar: app/src-tauri/binaries/rclone-<triple>[.exe].
//
// Usage: node scripts/fetch-rclone.mjs [--target <triple>]
// Default target: the host triple from `rustc -vV`.

import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { chmodSync, copyFileSync, existsSync, mkdirSync, mkdtempSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const RCLONE_VERSION = 'v1.74.3';
const BASE = `https://github.com/rclone/rclone/releases/download/${RCLONE_VERSION}`;

const PLATFORMS = {
  'aarch64-apple-darwin': 'osx-arm64',
  'x86_64-apple-darwin': 'osx-amd64',
  'x86_64-pc-windows-msvc': 'windows-amd64',
  'aarch64-pc-windows-msvc': 'windows-arm64',
  'x86_64-unknown-linux-gnu': 'linux-amd64',
  'aarch64-unknown-linux-gnu': 'linux-arm64',
};

function hostTriple() {
  const out = execFileSync('rustc', ['-vV'], { encoding: 'utf8' });
  const line = out.split('\n').find((l) => l.startsWith('host:'));
  if (line === undefined) {
    throw new Error('could not read host triple from rustc -vV');
  }
  return line.slice('host:'.length).trim();
}

async function download(url) {
  const res = await fetch(url);
  if (!res.ok) {
    throw new Error(`download failed (${res.status}): ${url}`);
  }
  return Buffer.from(await res.arrayBuffer());
}

function extract(zipPath, dest) {
  // bsdtar (macOS, Windows 10+) reads zip; GNU tar does not, so fall back to unzip.
  try {
    execFileSync('tar', ['-xf', zipPath, '-C', dest], { stdio: 'pipe' });
  } catch {
    execFileSync('unzip', ['-q', '-o', zipPath, '-d', dest], { stdio: 'pipe' });
  }
}

async function main() {
  const i = process.argv.indexOf('--target');
  const target = i === -1 ? hostTriple() : process.argv[i + 1];
  const platform = PLATFORMS[target];
  if (platform === undefined) {
    throw new Error(`unsupported target ${target}; known: ${Object.keys(PLATFORMS).join(', ')}`);
  }
  const isWindows = target.includes('windows');
  const outDir = join(dirname(fileURLToPath(import.meta.url)), '..', 'app', 'src-tauri', 'binaries');
  const outFile = join(outDir, `rclone-${target}${isWindows ? '.exe' : ''}`);
  if (existsSync(outFile) && !process.argv.includes('--force')) {
    console.log(`already present: ${outFile} (use --force to replace)`);
    return;
  }

  const zipName = `rclone-${RCLONE_VERSION}-${platform}.zip`;
  console.log(`fetching ${zipName}`);
  const [zip, sums] = await Promise.all([download(`${BASE}/${zipName}`), download(`${BASE}/SHA256SUMS`)]);

  const expected = sums
    .toString('utf8')
    .split('\n')
    .map((l) => l.trim().split(/\s+/))
    .find((parts) => parts[1] === zipName)?.[0];
  const actual = createHash('sha256').update(zip).digest('hex');
  if (expected === undefined || expected !== actual) {
    throw new Error(`checksum mismatch for ${zipName}: expected ${expected ?? 'none'}, got ${actual}`);
  }
  console.log('checksum OK');

  const work = mkdtempSync(join(tmpdir(), 'rclone-'));
  try {
    const zipPath = join(work, zipName);
    writeFileSync(zipPath, zip);
    extract(zipPath, work);
    const folder = readdirSync(work).find((n) => n.startsWith('rclone-') && !n.endsWith('.zip'));
    if (folder === undefined) {
      throw new Error('unexpected archive layout');
    }
    mkdirSync(outDir, { recursive: true });
    copyFileSync(join(work, folder, isWindows ? 'rclone.exe' : 'rclone'), outFile);
    if (!isWindows) {
      chmodSync(outFile, 0o755);
    }
    console.log(`wrote ${outFile}`);
  } finally {
    rmSync(work, { recursive: true, force: true });
  }
}

main().catch((e) => {
  console.error(`error: ${e.message}`);
  process.exit(1);
});
