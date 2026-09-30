import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';
import { DEFAULT_PREFERENCES, loadPreferences, savePreferences } from './preferences.js';

const cleanupPaths: string[] = [];

afterEach(async () => {
  await Promise.all(
    cleanupPaths.splice(0).map((path) => rm(path, { recursive: true, force: true })),
  );
});

describe('preferences', () => {
  it('returns defaults for a missing or malformed file', async () => {
    const directory = await mkdtemp(join(tmpdir(), 'puff-preferences-'));
    cleanupPaths.push(directory);
    const filePath = join(directory, 'preferences.json');

    expect(loadPreferences(filePath)).toEqual(DEFAULT_PREFERENCES);
    await writeFile(filePath, '{not-json', 'utf8');
    expect(loadPreferences(filePath)).toEqual(DEFAULT_PREFERENCES);
  });

  it('persists the launch-at-login preference', async () => {
    const directory = await mkdtemp(join(tmpdir(), 'puff-preferences-'));
    cleanupPaths.push(directory);
    const filePath = join(directory, 'preferences.json');

    savePreferences(filePath, { launchAtLogin: true });

    expect(loadPreferences(filePath)).toEqual({ launchAtLogin: true });
    expect(await readFile(filePath, 'utf8')).toContain('"launchAtLogin": true');
  });
});
