import { defineConfig, loadEnv } from '@rsbuild/core';
import { pluginReact } from '@rsbuild/plugin-react';

// Reads .env / .env.local / .env.<mode> so the dev proxy target stays out of
// source code. Only PUBLIC_* keys are inlined into the client bundle.
const { parsed } = loadEnv();

export default defineConfig({
  plugins: [pluginReact()],
  html: {
    title: 'puff',
    favicon: './public/favicon.svg',
  },
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      // Keeps the browser on a single origin during development: the API is
      // reached through the dev server instead of cross-origin requests.
      '/api': {
        target: parsed.API_PROXY_TARGET ?? 'http://localhost:3000',
        changeOrigin: true,
      },
    },
  },
});
