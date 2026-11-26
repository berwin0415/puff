import { copyFileSync, mkdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const output = fileURLToPath(new URL('../dist/plugin/', import.meta.url));
const resources = [
  'skills/task-run/SKILL.md',
  'references/setup-workflow.md',
  'references/execution-rules.md',
  'templates/config.example.json',
  'templates/task-context.md',
  'templates/run-result.md',
];

function copy(source, target) {
  const destination = join(output, target);
  mkdirSync(dirname(destination), { recursive: true });
  copyFileSync(source, destination);
}

for (const asset of ['plugin.json', 'skills/setup/SKILL.md', 'references/host.md']) {
  copy(new URL(`../${asset}`, import.meta.url), asset);
}
for (const asset of resources) {
  copy(fileURLToPath(import.meta.resolve(`@puff/agent-task-core/${asset}`)), asset);
}
