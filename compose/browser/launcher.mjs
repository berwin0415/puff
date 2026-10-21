import { chromium } from 'playwright';
import { spawn } from 'node:child_process';

const child = spawn(process.execPath, [
  '/opt/browser/node_modules/@playwright/mcp/cli.js',
  '--host', '0.0.0.0', '--port', '8931', '--allowed-hosts', '*',
  '--headless', '--no-sandbox', '--isolated',
  '--executable-path', chromium.executablePath(),
  '--output-dir', '/data/browser-artifacts',
  '--file-paths', 'absolute',
], { stdio: 'inherit' });
for (const signal of ['SIGTERM', 'SIGINT']) process.on(signal, () => child.kill(signal));
child.on('exit', code => process.exit(code ?? 1));
