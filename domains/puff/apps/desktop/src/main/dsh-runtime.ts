import { existsSync, readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { dirname, isAbsolute, join, resolve } from 'node:path';

interface PackageManifest {
  bin?: string | Record<string, string>;
}

const require = createRequire(import.meta.url);

export interface ResolveRuntimeOptions {
  runtimeRoot?: string;
}

function readPackageManifest(
  packageName: string,
  runtimeRoot?: string,
): {
  manifest: PackageManifest;
  packageDir: string;
} {
  if (runtimeRoot !== undefined && runtimeRoot !== '') {
    const manifestPath = join(runtimeRoot, 'node_modules', packageName, 'package.json');
    const manifest = JSON.parse(readFileSync(manifestPath, 'utf8')) as PackageManifest;
    return { manifest, packageDir: dirname(manifestPath) };
  }

  let manifestPath: string;
  try {
    manifestPath = require.resolve(`${packageName}/package.json`);
  } catch {
    manifestPath = findPackageManifest(require.resolve(packageName));
  }
  const manifest = JSON.parse(readFileSync(manifestPath, 'utf8')) as PackageManifest;
  return { manifest, packageDir: dirname(manifestPath) };
}

function findPackageManifest(startPath: string): string {
  let directory = dirname(startPath);
  for (;;) {
    const candidate = join(directory, 'package.json');
    if (existsSync(candidate)) {
      return candidate;
    }
    const parent = dirname(directory);
    if (parent === directory) {
      throw new Error(`Cannot find package.json for ${startPath}`);
    }
    directory = parent;
  }
}

function resolveBin(manifest: PackageManifest, packageDir: string, binName: string): string {
  const bin = manifest.bin;
  const relativePath = typeof bin === 'string' ? bin : bin?.[binName];
  if (relativePath === undefined || relativePath === '') {
    throw new Error(`Package ${binName} does not declare a ${binName} executable`);
  }
  return normalizeUnpackedPath(
    isAbsolute(relativePath) ? relativePath : resolve(packageDir, relativePath),
  );
}

export function normalizeUnpackedPath(filePath: string): string {
  if (!filePath.includes('app.asar')) {
    return filePath;
  }

  const unpackedPath = filePath.replace('app.asar', 'app.asar.unpacked');
  return existsSync(unpackedPath) ? unpackedPath : filePath;
}

export function resolveDshEntry(options: ResolveRuntimeOptions = {}): string {
  const { manifest, packageDir } = readPackageManifest('@deepseek-ai/dsh', options.runtimeRoot);
  return resolveBin(manifest, packageDir, 'dsh');
}

export function resolvePnpmEntry(options: ResolveRuntimeOptions = {}): string {
  const { manifest, packageDir } = readPackageManifest('pnpm', options.runtimeRoot);
  return resolveBin(manifest, packageDir, 'pnpm');
}
