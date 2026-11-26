import { defineConfig } from 'tsdown';

export default defineConfig({
  entry: { automation: 'src/cli.ts' },
  outDir: 'dist/plugin/scripts',
  format: ['esm'],
  platform: 'node',
  target: 'node22',
  dts: false,
  deps: { alwaysBundle: ['@puff/agent-task-core'] },
});
