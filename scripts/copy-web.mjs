#!/usr/bin/env node
// Copies the built frontend (app/dist) into cli/web so `clouddupe.py gui`
// can serve it without Node installed. Run via `npm run build:web` in app/.

import { cpSync, existsSync, rmSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const repo = join(dirname(fileURLToPath(import.meta.url)), '..');
const src = join(repo, 'app', 'dist');
const dest = join(repo, 'cli', 'web');

if (!existsSync(join(src, 'index.html'))) {
  console.error('app/dist/index.html not found; run `npm run build` first');
  process.exit(1);
}
rmSync(dest, { recursive: true, force: true });
cpSync(src, dest, { recursive: true });
console.log(`copied ${src} -> ${dest}`);
