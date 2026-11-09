import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';
import { pruneHostLogs, RedactingLogWriter, redactHostText } from './logger.js';

const cleanupPaths: string[] = [];

afterEach(async () => {
  await Promise.all(
    cleanupPaths.splice(0).map((path) => rm(path, { recursive: true, force: true })),
  );
});

describe('redactHostText', () => {
  it('removes the token-bearing DSH URL', () => {
    expect(redactHostText('dsh web: http://127.0.0.1:41234/?token=secret-token\n')).toBe(
      'dsh web: http://127.0.0.1:41234/\n',
    );
  });
});

describe('RedactingLogWriter', () => {
  it('redacts a URL split across stdout chunks', async () => {
    const directory = await mkdtemp(join(tmpdir(), 'puff-logs-'));
    cleanupPaths.push(directory);
    const writer = await RedactingLogWriter.create(directory, 'host', new Date(0));

    writer.write('dsh web: http://127.0.0.1:4');
    writer.write('1234/?token=secret-token\n');
    await writer.close();

    const contents = await readFile(writer.filePath, 'utf8');
    expect(contents).toBe('dsh web: http://127.0.0.1:41234/\n');
  });
});

describe('pruneHostLogs', () => {
  it('keeps only the newest log files', async () => {
    const directory = await mkdtemp(join(tmpdir(), 'puff-logs-'));
    cleanupPaths.push(directory);
    await Promise.all(
      Array.from({ length: 12 }, (_, index) =>
        writeFile(join(directory, `host-${String(index).padStart(2, '0')}.log`), ''),
      ),
    );

    await pruneHostLogs(directory, 10);

    const { readdir } = await import('node:fs/promises');
    const entries = (await readdir(directory)).sort();
    expect(entries).toHaveLength(10);
    expect(entries[0]).toBe('host-02.log');
  });
});
