import { type ChildProcessByStdio, type SpawnOptionsWithoutStdio, spawn } from 'node:child_process';
import { EventEmitter } from 'node:events';
import type { Readable } from 'node:stream';
import { HOST_RESTART_DELAYS_MS, HOST_STOP_GRACE_PERIOD_MS } from './constants.js';
import { type DshReadyUrl, parseDshReadyLine, redactDshUrl } from './dsh-url.js';
import { pruneHostLogs, RedactingLogWriter } from './logger.js';
import { buildDshInvocation } from './startup-environment.js';

export type HostSupervisorState =
  | 'idle'
  | 'starting'
  | 'ready'
  | 'restarting'
  | 'stopping'
  | 'stopped'
  | 'failed';

export interface HostReadyEvent extends DshReadyUrl {
  redactedUrl: string;
}

export interface HostRestartEvent {
  attempt: number;
  total: number;
  delayMs: number;
}

export type SpawnHostProcess = (
  command: string,
  args: string[],
  options: SpawnOptionsWithoutStdio,
) => HostChildProcess;

export type HostChildProcess = ChildProcessByStdio<null, Readable, Readable>;

export interface HostSupervisorOptions {
  nodeExecutable: string;
  dshEntry: string;
  workspaceDir: string;
  logDir: string;
  env: NodeJS.ProcessEnv;
  spawnHost?: SpawnHostProcess;
  terminateTree?: (pid: number) => Promise<void>;
  restartDelaysMs?: readonly number[];
  stopGracePeriodMs?: number;
}

export interface RestartDecision {
  delayMs: number;
  nextAttempt: number;
}

export function getRestartDecision(
  attempt: number,
  restartDelaysMs: readonly number[] = HOST_RESTART_DELAYS_MS,
): RestartDecision | null {
  const delayMs = restartDelaysMs[attempt];
  if (delayMs === undefined) {
    return null;
  }
  return { delayMs, nextAttempt: attempt + 1 };
}

export function waitForExit(child: HostChildProcess, timeoutMs: number): Promise<boolean> {
  if (child.exitCode !== null || child.signalCode !== null) {
    return Promise.resolve(true);
  }

  return new Promise<boolean>((resolve) => {
    const onExit = (): void => {
      clearTimeout(timer);
      resolve(true);
    };
    const timer = setTimeout(() => {
      child.off('exit', onExit);
      resolve(false);
    }, timeoutMs);
    child.once('exit', onExit);
  });
}

async function terminateProcessTree(pid: number): Promise<void> {
  if (process.platform === 'win32') {
    await new Promise<void>((resolve) => {
      const killer = spawn('taskkill', ['/pid', String(pid), '/t', '/f'], {
        stdio: 'ignore',
        windowsHide: true,
      });
      killer.once('error', () => resolve());
      killer.once('exit', () => resolve());
    });
    return;
  }

  try {
    process.kill(pid, 'SIGKILL');
  } catch {
    // The process may have exited between the graceful signal and the force kill.
  }
}

function defaultSpawnHost(
  command: string,
  args: string[],
  options: SpawnOptionsWithoutStdio,
): HostChildProcess {
  return spawn(command, args, { ...options, stdio: ['ignore', 'pipe', 'pipe'] });
}

export class HostSupervisor extends EventEmitter {
  private readonly spawnHost: SpawnHostProcess;
  private readonly terminateTree: (pid: number) => Promise<void>;
  private readonly restartDelaysMs: readonly number[];
  private readonly stopGracePeriodMs: number;
  private child: HostChildProcess | null = null;
  private logger: RedactingLogWriter | null = null;
  private state: HostSupervisorState = 'idle';
  private generation = 0;
  private restartAttempts = 0;
  private stoppedByUser = false;
  private startPromise: Promise<void> | null = null;
  private stdoutBuffer = '';

  constructor(private readonly options: HostSupervisorOptions) {
    super();
    this.spawnHost = options.spawnHost ?? defaultSpawnHost;
    this.terminateTree = options.terminateTree ?? terminateProcessTree;
    this.restartDelaysMs = options.restartDelaysMs ?? HOST_RESTART_DELAYS_MS;
    this.stopGracePeriodMs = options.stopGracePeriodMs ?? HOST_STOP_GRACE_PERIOD_MS;
  }

  get currentState(): HostSupervisorState {
    return this.state;
  }

  get isRunning(): boolean {
    return this.child !== null && this.child.exitCode === null;
  }

  async start(): Promise<void> {
    if (this.startPromise !== null) {
      return this.startPromise;
    }

    this.startPromise = this.doStart().finally(() => {
      this.startPromise = null;
    });
    return this.startPromise;
  }

  async restart(): Promise<void> {
    await this.stop();
    this.stoppedByUser = false;
    this.restartAttempts = 0;
    this.generation += 1;
    await this.launch(this.generation);
  }

  async stop(): Promise<void> {
    this.stoppedByUser = true;
    this.generation += 1;
    this.setState('stopping');
    await this.terminateCurrent();
    this.setState('stopped');
  }

  private async doStart(): Promise<void> {
    await this.terminateCurrent();
    this.stoppedByUser = false;
    this.restartAttempts = 0;
    this.generation += 1;
    await this.launch(this.generation);
  }

  private async launch(generation: number): Promise<void> {
    if (this.stoppedByUser || generation !== this.generation) {
      return;
    }

    this.setState('starting');
    this.stdoutBuffer = '';
    await pruneHostLogs(this.options.logDir);

    const logger = await RedactingLogWriter.create(this.options.logDir, 'host');
    this.logger = logger;
    logger.write(`[puff] starting DSH profile\n`);

    const child = this.spawnHost(
      this.options.nodeExecutable,
      [this.options.dshEntry, ...buildDshInvocation('serve')],
      {
        cwd: this.options.workspaceDir,
        env: this.options.env,
        windowsHide: true,
      },
    );
    this.child = child;

    child.stdout.setEncoding('utf8');
    child.stderr.setEncoding('utf8');
    child.stdout.on('data', (chunk: string) => {
      logger.write(chunk);
      this.consumeStdout(chunk, generation);
    });
    child.stderr.on('data', (chunk: string) => {
      logger.write(chunk);
    });
    child.once('error', (error) => {
      this.handleChildExit(generation, child, null, null, error);
    });
    child.once('exit', (code, signal) => {
      this.handleChildExit(generation, child, code, signal);
    });
  }

  private consumeStdout(chunk: string, generation: number): void {
    this.stdoutBuffer += chunk;
    let newlineIndex = this.stdoutBuffer.indexOf('\n');
    while (newlineIndex !== -1) {
      const line = this.stdoutBuffer.slice(0, newlineIndex).replace(/\r$/u, '');
      this.stdoutBuffer = this.stdoutBuffer.slice(newlineIndex + 1);
      if (generation === this.generation && this.state === 'starting') {
        const ready = parseDshReadyLine(line);
        if (ready !== null) {
          this.restartAttempts = 0;
          this.setState('ready');
          this.emit('ready', {
            ...ready,
            redactedUrl: redactDshUrl(ready.url),
          } satisfies HostReadyEvent);
        }
      }
      newlineIndex = this.stdoutBuffer.indexOf('\n');
    }
  }

  private handleChildExit(
    generation: number,
    child: HostChildProcess,
    code: number | null,
    signal: NodeJS.Signals | null,
    error?: Error,
  ): void {
    if (generation !== this.generation || this.child !== child) {
      return;
    }

    this.child = null;
    const logger = this.logger;
    this.logger = null;
    void logger?.close();

    if (this.stoppedByUser) {
      this.setState('stopped');
      return;
    }

    const decision = getRestartDecision(this.restartAttempts, this.restartDelaysMs);
    if (decision === null) {
      const reason =
        error?.message ?? (signal === null ? `exit code ${String(code)}` : `signal ${signal}`);
      const failure = new Error(`DSH Host exited unexpectedly (${reason})`);
      this.setState('failed');
      this.emit('failed', failure);
      return;
    }

    this.restartAttempts = decision.nextAttempt;
    this.setState('restarting');
    this.emit('restarting', {
      attempt: decision.nextAttempt,
      total: this.restartDelaysMs.length,
      delayMs: decision.delayMs,
    } satisfies HostRestartEvent);

    setTimeout(() => {
      if (!this.stoppedByUser && generation === this.generation) {
        void this.launch(generation);
      }
    }, decision.delayMs);
  }

  private async terminateCurrent(): Promise<void> {
    const child = this.child;
    const logger = this.logger;
    this.child = null;
    this.logger = null;

    if (child !== null && child.exitCode === null && child.signalCode === null) {
      child.kill('SIGTERM');
      const exited = await waitForExit(child, this.stopGracePeriodMs);
      if (!exited) {
        if (child.pid === undefined) {
          child.kill('SIGKILL');
        } else {
          await this.terminateTree(child.pid);
        }
      }
    }

    await logger?.close();
  }

  private setState(state: HostSupervisorState): void {
    this.state = state;
    this.emit('state', state);
  }
}
