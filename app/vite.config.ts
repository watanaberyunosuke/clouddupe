import { defineConfig } from 'vite';

// Relative base so the same build runs inside Tauri and from the Python web server.
export default defineConfig({
  base: './',
  clearScreen: false,
  server: { port: 1420, strictPort: true },
  build: { target: 'es2021', outDir: 'dist', emptyOutDir: true },
});
