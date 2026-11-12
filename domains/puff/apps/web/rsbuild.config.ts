import { defineConfig, loadEnv } from '@rsbuild/core';
import { pluginReact } from '@rsbuild/plugin-react';

// 读取 .env / .env.local / .env.<mode>，让开发代理目标不进入源码。
// 只有 PUBLIC_* 前缀的变量会被内联到浏览器产物。
const { parsed } = loadEnv();

export default defineConfig({
  plugins: [pluginReact()],
  html: {
    title: 'NEON VEIL // 霓虹帷幕',
    favicon: './public/favicon.svg',
  },
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      // 开发时让浏览器保持同源：API 请求通过 dev server 转发，而不是跨域直连。
      '/api': {
        target: parsed.API_PROXY_TARGET ?? 'http://localhost:3000',
        changeOrigin: true,
      },
    },
  },
});
