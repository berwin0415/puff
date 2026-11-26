import { readFileSync } from 'node:fs';

import { runCommand } from './commands.js';
import { createStore } from './store.js';

try {
  const [command, inputPath] = process.argv.slice(2);
  if (!command) throw new Error('请指定命令');
  const raw = inputPath ? readFileSync(inputPath === '-' ? 0 : inputPath, 'utf8') : '';
  const result = runCommand(
    createStore(process.env.PUFF_AGENT_TASK_DATA_DIR || undefined),
    command,
    raw.trim() ? (JSON.parse(raw) as unknown) : {},
  );
  console.log(JSON.stringify(result, null, 2));
} catch (error) {
  const detail = error instanceof Error ? error.message : '未知错误';
  console.error(JSON.stringify({ error: detail }));
  process.exitCode = 1;
}
