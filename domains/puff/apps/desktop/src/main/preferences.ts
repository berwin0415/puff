import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname } from 'node:path';

export interface AppPreferences {
  launchAtLogin: boolean;
}

export const DEFAULT_PREFERENCES: AppPreferences = {
  launchAtLogin: false,
};

function isPreferences(value: unknown): value is AppPreferences {
  if (typeof value !== 'object' || value === null) {
    return false;
  }
  return typeof (value as { launchAtLogin?: unknown }).launchAtLogin === 'boolean';
}

export function loadPreferences(filePath: string): AppPreferences {
  try {
    const parsed = JSON.parse(readFileSync(filePath, 'utf8')) as unknown;
    return isPreferences(parsed) ? parsed : { ...DEFAULT_PREFERENCES };
  } catch {
    return { ...DEFAULT_PREFERENCES };
  }
}

export function savePreferences(filePath: string, preferences: AppPreferences): void {
  mkdirSync(dirname(filePath), { recursive: true });
  writeFileSync(filePath, `${JSON.stringify(preferences, null, 2)}\n`, 'utf8');
}
