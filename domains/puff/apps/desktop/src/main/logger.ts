import { createWriteStream, promises as fs, type WriteStream } from 'node:fs';
import { join } from 'node:path';
import { LOG_FILE_LIMIT } from './constants.js';
import { redactDshUrl } from './dsh-url.js';

export function redactHostText(input: string): string {
  return input
    .replace(/http:\/\/127\.0\.0\.1:\d+\/[^\s]*/gu, (match) => redactDshUrl(match))
    .replace(/token=[^&\s"']+/giu, 'token=<redacted>');
}

export class RedactingLogWriter {
  private pending = '';

  private constructor(
    readonly filePath: string,
    private readonly stream: WriteStream,
  ) {}

  static async create(
    directory: string,
    prefix: string,
    now: Date = new Date(),
  ): Promise<RedactingLogWriter> {
    await fs.mkdir(directory, { recursive: true });
    const stamp = now.toISOString().replace(/[:.]/gu, '-');
    const filePath = join(directory, `${prefix}-${stamp}.log`);
    return new RedactingLogWriter(filePath, createWriteStream(filePath, { flags: 'a' }));
  }

  write(chunk: string | Buffer): void {
    this.pending += chunk.toString();

    let newlineIndex = this.pending.indexOf('\n');
    while (newlineIndex !== -1) {
      const line = this.pending.slice(0, newlineIndex + 1);
      this.pending = this.pending.slice(newlineIndex + 1);
      this.stream.write(redactHostText(line));
      newlineIndex = this.pending.indexOf('\n');
    }

    if (this.pending.length > 64 * 1024) {
      this.stream.write(redactHostText(this.pending));
      this.pending = '';
    }
  }

  async close(): Promise<void> {
    if (this.pending !== '') {
      this.stream.write(redactHostText(this.pending));
      this.pending = '';
    }
    await new Promise<void>((resolve, reject) => {
      this.stream.end((error?: Error | null) => {
        if (error !== undefined && error !== null) {
          reject(error);
          return;
        }
        resolve();
      });
    });
  }
}

export async function pruneHostLogs(directory: string, keep = LOG_FILE_LIMIT): Promise<void> {
  let entries: string[];
  try {
    entries = await fs.readdir(directory);
  } catch {
    return;
  }

  const logs = entries
    .filter((entry) => entry.endsWith('.log'))
    .sort()
    .reverse();

  await Promise.all(
    logs.slice(keep).map((entry) => fs.rm(join(directory, entry), { force: true })),
  );
}
