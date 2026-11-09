import { appendFileSync, mkdirSync } from 'node:fs';
import { dirname, join, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  app,
  BrowserWindow,
  dialog,
  Menu,
  type MenuItemConstructorOptions,
  nativeImage,
  shell,
  Tray,
} from 'electron';
import { APP_DISPLAY_NAME, APP_USER_MODEL_ID, DSH_PROFILE_NAME } from './main/constants.js';
import { resolveDefaultWorkspaceDir, resolveDshHome } from './main/dsh-paths.js';
import { ensureDshProfile } from './main/dsh-profile.js';
import { resolveDshEntry, resolvePnpmEntry } from './main/dsh-runtime.js';
import {
  type HostReadyEvent,
  type HostRestartEvent,
  HostSupervisor,
} from './main/host-supervisor.js';
import { TRAY_ICON_DATA_URL } from './main/icon.js';
import { redactHostText } from './main/logger.js';
import { ensurePnpmShim } from './main/pnpm-shim.js';
import { type AppPreferences, loadPreferences, savePreferences } from './main/preferences.js';
import { buildHostEnvironment } from './main/startup-environment.js';

const STATIC_DIR = join(dirname(fileURLToPath(import.meta.url)), 'static');
const STATIC_DIR_PREFIX = `${resolve(STATIC_DIR).toLowerCase()}${sep}`;

app.setName(APP_DISPLAY_NAME);
app.enableSandbox();
if (process.platform === 'win32') {
  app.setAppUserModelId(APP_USER_MODEL_ID);
}

let mainWindow: BrowserWindow | null = null;
let tray: Tray | null = null;
let supervisor: HostSupervisor | null = null;
let preferences: AppPreferences = { launchAtLogin: false };
let userDataDirectory = '';
let workspaceDirectory = '';
let logDirectory = '';
let allowedOrigin: string | null = null;
let isQuitting = false;
let isShuttingDown = false;
let hostStartPromise: Promise<void> | null = null;
let supervisorEnvironment: NodeJS.ProcessEnv | null = null;
let dshEntryPath = '';

const gotSingleInstanceLock = app.requestSingleInstanceLock();

if (!gotSingleInstanceLock) {
  app.quit();
} else {
  app.on('second-instance', () => {
    showMainWindow();
  });
  app.on('window-all-closed', () => {
    // The tray owns the application lifecycle on Windows.
  });
  app.on('activate', () => {
    showMainWindow();
  });
  app.on('before-quit', (event) => {
    if (isShuttingDown) {
      return;
    }
    event.preventDefault();
    isQuitting = true;
    isShuttingDown = true;
    void shutdown().finally(() => {
      app.exit(0);
    });
  });
  (app as unknown as NodeJS.EventEmitter).on('session-end', () => {
    isQuitting = true;
    isShuttingDown = true;
    void supervisor?.stop();
  });

  void app
    .whenReady()
    .then(bootstrap)
    .catch((error: unknown) => {
      const message = redactHostText(error instanceof Error ? error.message : String(error));
      writeDiagnostic(`bootstrap failed: ${message}`);
      console.error(`[puff] bootstrap failed: ${message}`);
      app.exit(1);
    });
}

async function bootstrap(): Promise<void> {
  userDataDirectory = app.getPath('userData');
  workspaceDirectory = resolveDefaultWorkspaceDir();
  logDirectory = join(userDataDirectory, 'logs');
  mkdirSync(workspaceDirectory, { recursive: true });
  mkdirSync(logDirectory, { recursive: true });
  writeDiagnostic(`bootstrap userData=${userDataDirectory}`);

  preferences = loadPreferences(preferencesPath());
  applyLoginItemSetting(preferences.launchAtLogin);

  createMainWindow();
  createTray();
  await showStaticPage('loading', { message: '正在启动 DeepSeek Harness…' });
  await startHost();
}

function createMainWindow(): void {
  mainWindow = new BrowserWindow({
    width: 1_280,
    height: 840,
    minWidth: 900,
    minHeight: 600,
    show: false,
    backgroundColor: '#0b1020',
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
      allowRunningInsecureContent: false,
    },
  });

  mainWindow.once('ready-to-show', () => {
    mainWindow?.show();
  });
  mainWindow.on('close', (event) => {
    if (!isQuitting) {
      event.preventDefault();
      mainWindow?.hide();
    }
  });
  mainWindow.on('closed', () => {
    mainWindow = null;
  });
  mainWindow.webContents.on('will-navigate', (event, url) => {
    handleNavigation(event, url);
  });
  mainWindow.webContents.on('will-redirect', (event, url) => {
    handleNavigation(event, url);
  });
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (isSafeExternalUrl(url)) {
      void shell.openExternal(url);
    }
    return { action: 'deny' };
  });
}

function createTray(): void {
  tray = new Tray(nativeImage.createFromDataURL(TRAY_ICON_DATA_URL));
  tray.setToolTip(APP_DISPLAY_NAME);
  tray.on('click', () => {
    showMainWindow();
  });
  rebuildTrayMenu();
}

function rebuildTrayMenu(): void {
  if (tray === null) {
    return;
  }

  const template: MenuItemConstructorOptions[] = [
    {
      label: '打开 puff',
      click: () => {
        showMainWindow();
      },
    },
    {
      label: '重启 DSH Host',
      click: () => {
        void restartHost();
      },
    },
    { type: 'separator' },
    {
      label: '开机启动',
      type: 'checkbox',
      checked: preferences.launchAtLogin,
      click: (item) => {
        setLaunchAtLogin(item.checked);
      },
    },
    {
      label: '打开日志',
      click: () => {
        void shell.openPath(logDirectory);
      },
    },
    { type: 'separator' },
    {
      label: '退出 puff',
      click: () => {
        void requestQuit();
      },
    },
  ];

  tray.setContextMenu(Menu.buildFromTemplate(template));
}

function showMainWindow(): void {
  if (mainWindow === null) {
    return;
  }
  if (mainWindow.isMinimized()) {
    mainWindow.restore();
  }
  mainWindow.show();
  mainWindow.focus();
}

async function startHost(): Promise<void> {
  if (hostStartPromise !== null) {
    return hostStartPromise;
  }

  hostStartPromise = doStartHost().finally(() => {
    hostStartPromise = null;
  });
  return hostStartPromise;
}

async function doStartHost(): Promise<void> {
  try {
    if (supervisor === null) {
      const dshHome = resolveDshHome();
      writeDiagnostic(`resolving DSH runtime with DSH_HOME=${dshHome}`);
      const runtimeRoot = app.isPackaged ? join(process.resourcesPath, 'dsh-runtime') : undefined;
      dshEntryPath = resolveDshEntry({ runtimeRoot });
      const pnpmEntry = resolvePnpmEntry({ runtimeRoot });
      const pnpmShimDir = await ensurePnpmShim({
        userDataDir: userDataDirectory,
        nodeExecutable: process.execPath,
        pnpmEntry,
      });
      supervisorEnvironment = buildHostEnvironment({
        pnpmShimDir,
        extra: {
          PUFF_PNPM_NODE: process.execPath,
          PUFF_PNPM_ENTRY: pnpmEntry,
        },
      });

      await ensureDshProfile({
        nodeExecutable: process.execPath,
        dshEntry: dshEntryPath,
        dshHome,
        workspaceDir: workspaceDirectory,
        env: supervisorEnvironment,
        profile: DSH_PROFILE_NAME,
      });
      writeDiagnostic(`DSH profile ready`);

      supervisor = new HostSupervisor({
        nodeExecutable: process.execPath,
        dshEntry: dshEntryPath,
        workspaceDir: workspaceDirectory,
        logDir: logDirectory,
        env: supervisorEnvironment,
      });
      wireSupervisorEvents(supervisor);
    }

    await showStaticPage('loading', { message: '正在启动 DeepSeek Harness…' });
    writeDiagnostic('starting DSH host');
    await supervisor.start();
  } catch (error) {
    writeDiagnostic(`start host failed: ${error instanceof Error ? error.message : String(error)}`);
    await showErrorPage('无法启动 DeepSeek Harness', error);
  }
}

function wireSupervisorEvents(instance: HostSupervisor): void {
  instance.on('ready', (ready: HostReadyEvent) => {
    allowedOrigin = ready.origin;
    writeDiagnostic(`DSH host ready at ${ready.redactedUrl}`);
    if (mainWindow === null) {
      return;
    }
    void mainWindow.loadURL(ready.url).catch((error: unknown) => {
      void showErrorPage('无法加载 DeepSeek Harness Web UI', error);
    });
  });

  instance.on('restarting', (event: HostRestartEvent) => {
    writeDiagnostic(
      `DSH host restarting attempt=${String(event.attempt)} delayMs=${String(event.delayMs)}`,
    );
    const seconds = Math.ceil(event.delayMs / 1_000);
    void showStaticPage('loading', {
      message: `DSH Host 已退出，${seconds} 秒后重启（${event.attempt}/${event.total}）…`,
    });
  });

  instance.on('failed', (error: Error) => {
    writeDiagnostic(`DSH host failed: ${error.message}`);
    void showErrorPage('DSH Host 多次异常退出', error);
  });
}

async function restartHost(): Promise<void> {
  if (supervisor === null) {
    await startHost();
    return;
  }

  try {
    await showStaticPage('loading', { message: '正在重启 DeepSeek Harness…' });
    await supervisor.restart();
  } catch (error) {
    await showErrorPage('无法重启 DeepSeek Harness', error);
  }
}

async function requestQuit(): Promise<void> {
  if (isQuitting) {
    return;
  }

  if (supervisor?.isRunning === true) {
    const options = {
      type: 'warning' as const,
      buttons: ['退出', '取消'],
      defaultId: 1,
      cancelId: 1,
      title: '退出 puff',
      message: '退出会终止 DSH Host 与正在运行的任务。',
      detail: '关闭窗口只会隐藏到托盘；只有确认退出才会结束后台任务。',
      noLink: true,
    };
    const result =
      mainWindow === null
        ? await dialog.showMessageBox(options)
        : await dialog.showMessageBox(mainWindow, options);
    if (result.response !== 0) {
      return;
    }
  }

  isQuitting = true;
  app.quit();
}

async function shutdown(): Promise<void> {
  try {
    await supervisor?.stop();
  } finally {
    tray?.destroy();
    tray = null;
  }
}

function handleNavigation(event: Electron.Event, rawUrl: string): void {
  const action = getPuffAction(rawUrl);
  if (action !== null) {
    event.preventDefault();
    void handlePuffAction(action);
    return;
  }

  if (isAllowedDshUrl(rawUrl) || isLocalPageUrl(rawUrl)) {
    return;
  }

  event.preventDefault();
  if (isSafeExternalUrl(rawUrl)) {
    void shell.openExternal(rawUrl);
  }
}

async function handlePuffAction(action: string): Promise<void> {
  switch (action) {
    case 'retry':
      await startHost();
      break;
    case 'restart':
      await restartHost();
      break;
    case 'open-logs':
      await shell.openPath(logDirectory);
      break;
    case 'quit':
      await requestQuit();
      break;
    default:
      break;
  }
}

function getPuffAction(rawUrl: string): string | null {
  try {
    const parsed = new URL(rawUrl);
    return parsed.protocol === 'puff-action:' ? parsed.hostname : null;
  } catch {
    return null;
  }
}

function isAllowedDshUrl(rawUrl: string): boolean {
  if (allowedOrigin === null) {
    return false;
  }
  try {
    const parsed = new URL(rawUrl);
    return (
      parsed.protocol === 'http:' &&
      parsed.hostname === '127.0.0.1' &&
      parsed.origin === allowedOrigin
    );
  } catch {
    return false;
  }
}

function isLocalPageUrl(rawUrl: string): boolean {
  try {
    const parsed = new URL(rawUrl);
    if (parsed.protocol !== 'file:') {
      return false;
    }
    return resolve(fileURLToPath(parsed)).toLowerCase().startsWith(STATIC_DIR_PREFIX);
  } catch {
    return false;
  }
}

function isSafeExternalUrl(rawUrl: string): boolean {
  try {
    const parsed = new URL(rawUrl);
    return parsed.protocol === 'http:' || parsed.protocol === 'https:';
  } catch {
    return false;
  }
}

async function showStaticPage(
  page: 'loading' | 'error',
  query: Record<string, string>,
): Promise<void> {
  if (mainWindow === null) {
    return;
  }

  try {
    await mainWindow.loadFile(join(STATIC_DIR, `${page}.html`), { query });
    mainWindow.setTitle(page === 'error' ? `${APP_DISPLAY_NAME} - 启动失败` : APP_DISPLAY_NAME);
  } catch (error) {
    if (!isQuitting) {
      console.error(
        `[puff] failed to load static page: ${
          error instanceof Error ? error.message : String(error)
        }`,
      );
    }
  }
}

async function showErrorPage(summary: string, error: unknown): Promise<void> {
  const detail = redactHostText(error instanceof Error ? error.message : String(error));
  writeDiagnostic(`error page: ${summary} - ${detail}`);
  await showStaticPage('error', {
    message: `${summary}: ${detail}`,
    logs: logDirectory,
  });
  showMainWindow();
}

function preferencesPath(): string {
  return join(userDataDirectory, 'preferences.json');
}

function setLaunchAtLogin(enabled: boolean): void {
  preferences = { ...preferences, launchAtLogin: enabled };
  savePreferences(preferencesPath(), preferences);
  applyLoginItemSetting(enabled);
  rebuildTrayMenu();
}

function applyLoginItemSetting(enabled: boolean): void {
  if (!app.isPackaged) {
    return;
  }
  app.setLoginItemSettings({
    openAtLogin: enabled,
    path: process.execPath,
    args: [],
  });
}

function writeDiagnostic(message: string): void {
  if (logDirectory === '') {
    return;
  }
  try {
    appendFileSync(
      join(logDirectory, 'app.log'),
      `${new Date().toISOString()} ${redactHostText(message)}\n`,
      'utf8',
    );
  } catch {
    // Diagnostics must never prevent the application from starting.
  }
}
