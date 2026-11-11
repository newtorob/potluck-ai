import { defineConfig, searchForWorkspaceRoot } from 'vite';
import react from '@vitejs/plugin-react';
import { fileURLToPath, URL } from 'node:url';
import { existsSync, realpathSync } from 'node:fs';

const root = fileURLToPath(new URL('.', import.meta.url));
const dependencies = fileURLToPath(new URL('./node_modules', import.meta.url));

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [react()],
  base: './',
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    fs: {
      // Linked worktree dependencies can live outside the workspace root.
      allow: [searchForWorkspaceRoot(root), ...(existsSync(dependencies) ? [realpathSync(dependencies)] : [])],
    },
  },
  preview: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
  },
});
