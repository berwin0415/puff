import { DSH_READY_PREFIX } from './constants.js';

export interface DshReadyUrl {
  url: string;
  origin: string;
  port: number;
}

function isLoopbackUrl(url: URL): boolean {
  return url.protocol === 'http:' && url.hostname === '127.0.0.1' && url.port !== '';
}

export function parseDshReadyLine(line: string): DshReadyUrl | null {
  const trimmed = line.trim();
  if (!trimmed.startsWith(DSH_READY_PREFIX)) {
    return null;
  }

  const rawUrl = trimmed.slice(DSH_READY_PREFIX.length).split(/\s+/u)[0];
  if (rawUrl === undefined || rawUrl === '') {
    return null;
  }

  let parsed: URL;
  try {
    parsed = new URL(rawUrl);
  } catch {
    return null;
  }

  if (!isLoopbackUrl(parsed)) {
    return null;
  }

  const port = Number(parsed.port);
  if (!Number.isInteger(port) || port < 1 || port > 65_535) {
    return null;
  }

  return {
    url: parsed.toString(),
    origin: parsed.origin,
    port,
  };
}

export function redactDshUrl(value: string | URL): string {
  const parsed = value instanceof URL ? value : safeUrl(value);
  if (parsed !== null && isLoopbackUrl(parsed)) {
    return `http://127.0.0.1:${parsed.port}/`;
  }
  return String(value).replace(/token=[^&\s"']+/giu, 'token=<redacted>');
}

function safeUrl(value: string): URL | null {
  try {
    return new URL(value);
  } catch {
    return null;
  }
}
