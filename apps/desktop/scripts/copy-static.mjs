import { cp, mkdir, rm } from 'node:fs/promises';

const source = new URL('../src/static/', import.meta.url);
const target = new URL('../dist/static/', import.meta.url);

await mkdir(new URL('../dist/', import.meta.url), { recursive: true });
await rm(target, { recursive: true, force: true });
await cp(source, target, { recursive: true });
