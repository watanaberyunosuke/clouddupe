#!/usr/bin/env node
// Builds Cloud Dupe.app and packs it into a drag-to-install DMG with plain
// hdiutil. Avoids the Finder window styling in Tauri's own DMG step, which can
// fail with "couldn't unmount disk - Resource busy".
//
// Usage (from app/): npm run dmg            build the app, then the DMG
//                    npm run dmg -- --skip-build   pack the existing .app only
// Output: target/release/bundle/dmg/Cloud Dupe_<version>_<arch>.dmg

import { execFileSync } from 'node:child_process';
import { cpSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, symlinkSync } from 'node:fs';
import { arch, platform, tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

if (platform() !== 'darwin') {
  console.error('error: DMGs can only be made on macOS');
  process.exit(1);
}

const repo = join(dirname(fileURLToPath(import.meta.url)), '..');
const conf = JSON.parse(readFileSync(join(repo, 'app', 'src-tauri', 'tauri.conf.json'), 'utf8'));
const name = conf.productName;
const bundle = join(repo, 'target', 'release', 'bundle');
const appPath = join(bundle, 'macos', `${name}.app`);
const outDir = join(bundle, 'dmg');
const outFile = join(outDir, `${name}_${conf.version}_${arch() === 'arm64' ? 'aarch64' : 'x64'}.dmg`);

const run = (cmd, args, cwd) => execFileSync(cmd, args, { cwd, stdio: 'inherit' });

if (!process.argv.includes('--skip-build')) {
  run('npx', ['tauri', 'build', '--bundles', 'app'], join(repo, 'app'));
}
if (!existsSync(appPath)) {
  console.error(`error: ${appPath} not found; run without --skip-build`);
  process.exit(1);
}

// Stage outside Dropbox/iCloud folders so sync clients do not lock files mid-build.
const stage = mkdtempSync(join(tmpdir(), 'clouddupe-dmg-'));
try {
  cpSync(appPath, join(stage, `${name}.app`), { recursive: true, verbatimSymlinks: true });
  symlinkSync('/Applications', join(stage, 'Applications'));
  mkdirSync(outDir, { recursive: true });
  run('hdiutil', ['create', '-volname', name, '-srcfolder', stage, '-ov', '-format', 'UDZO', outFile]);
  run('hdiutil', ['verify', outFile]);
  console.log(`\nwrote ${outFile}`);
} finally {
  rmSync(stage, { recursive: true, force: true });
}
