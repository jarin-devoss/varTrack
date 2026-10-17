/// <reference types="vitest" />
import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';

// The dev server proxies each varTrack service under /api/<service> so the
// browser never makes cross-origin calls. Override the targets with
// VT_GATEWAY_URL / VT_ORCHESTRATOR_URL / VT_WATCHER_URL.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, '.', 'VT_');
  const target = (key: string, fallback: string) => env[key] || fallback;
  const proxy = (prefix: string, url: string) => ({
    [prefix]: {
      target: url,
      changeOrigin: true,
      rewrite: (path: string) => path.slice(prefix.length) || '/',
    },
  });

  return {
    plugins: [react()],
    server: {
      port: 5173,
      proxy: {
        ...proxy('/api/gateway', target('VT_GATEWAY_URL', 'http://localhost:5657')),
        ...proxy('/api/orchestrator', target('VT_ORCHESTRATOR_URL', 'http://localhost:8000')),
        ...proxy('/api/watcher', target('VT_WATCHER_URL', 'http://localhost:9091')),
      },
    },
    test: {
      environment: 'node',
      include: ['src/**/*.test.ts'],
    },
  };
});
