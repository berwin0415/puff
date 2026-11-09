import { defineConfig } from 'vitest/config';

export default defineConfig({
  // 解析 tsconfig.json 中声明的路径别名（包括 `nest g library` 添加的），
  // 不需要额外插件。
  resolve: {
    tsconfigPaths: true,
  },
  test: {
    globals: true,
    root: './',
    include: ['**/*.spec.ts'],
  },
});
