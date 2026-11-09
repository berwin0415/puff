#!/usr/bin/env node
// Enforces the dependency rules from docs/monorepo-architecture-design.md (4.5).
// The repo is TypeScript-only today, so workspace edges come from package.json
// manifests instead of an Nx project graph. Revisit when a second language joins.

import { existsSync } from 'node:fs';
import { readdir, readFile } from 'node:fs/promises';
import { dirname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), '../..');

const DOMAIN_KINDS = {
  apps: 'app',
  services: 'service',
  libs: 'lib',
  labs: 'lab',
};

const PLATFORM_KINDS = {
  libs: 'lib',
  services: 'service',
};

const errors = [];

function fail(project, message) {
  errors.push(`${project}: ${message}`);
}

async function listDirectories(path) {
  try {
    const entries = await readdir(path, { withFileTypes: true });
    return entries.filter((entry) => entry.isDirectory()).map((entry) => entry.name);
  } catch {
    return [];
  }
}

async function readJson(path) {
  return JSON.parse(await readFile(path, 'utf8'));
}

function tagValue(tags, prefix) {
  const matches = tags.filter((tag) => tag.startsWith(prefix));
  if (matches.length !== 1) {
    return null;
  }
  return matches[0].slice(prefix.length);
}

async function loadProject(projectRoot, expectedDomain, expectedType) {
  const projectJsonPath = join(projectRoot, 'project.json');
  const pathLabel = relative(repoRoot, projectRoot);

  if (!existsSync(projectJsonPath)) {
    fail(pathLabel, 'missing project.json');
    return null;
  }

  const manifest = await readJson(projectJsonPath);
  const packageJsonPath = join(projectRoot, 'package.json');
  const manifestPackage = existsSync(packageJsonPath) ? await readJson(packageJsonPath) : {};
  const name = typeof manifest.name === 'string' ? manifest.name : manifestPackage.name;

  if (!name) {
    fail(pathLabel, 'project.json or package.json must declare a name');
    return null;
  }

  const tags = Array.isArray(manifest.tags) ? manifest.tags : [];
  const domain = tagValue(tags, 'domain:');
  const type = tagValue(tags, 'type:');
  const language = tagValue(tags, 'lang:');

  if (!domain) fail(name, 'must declare exactly one domain:* tag');
  if (!type) fail(name, 'must declare exactly one type:* tag');
  if (!language) fail(name, 'must declare exactly one lang:* tag');
  if (domain && domain !== expectedDomain) {
    fail(name, `domain tag is "${domain}" but path belongs to "${expectedDomain}"`);
  }
  if (type && type !== expectedType) {
    fail(name, `type tag is "${type}" but directory implies "${expectedType}"`);
  }

  const dependencies = {
    ...(manifestPackage.dependencies ?? {}),
    ...(manifestPackage.devDependencies ?? {}),
    ...(manifestPackage.peerDependencies ?? {}),
  };

  return {
    name,
    root: pathLabel,
    domain: domain ?? expectedDomain,
    type: type ?? expectedType,
    productionDependencies: Object.keys(manifestPackage.dependencies ?? {}),
    dependencies: Object.keys(dependencies),
  };
}

async function discoverProjects() {
  const projects = [];

  for (const domain of await listDirectories(join(repoRoot, 'domains'))) {
    for (const [kind, expectedType] of Object.entries(DOMAIN_KINDS)) {
      for (const name of await listDirectories(join(repoRoot, 'domains', domain, kind))) {
        const project = await loadProject(
          join(repoRoot, 'domains', domain, kind, name),
          domain,
          expectedType,
        );
        if (project) projects.push(project);
      }
    }
  }

  for (const [kind, expectedType] of Object.entries(PLATFORM_KINDS)) {
    for (const name of await listDirectories(join(repoRoot, 'platform', kind))) {
      const project = await loadProject(
        join(repoRoot, 'platform', kind, name),
        'platform',
        expectedType,
      );
      if (project) projects.push(project);
    }
  }

  for (const domain of await listDirectories(join(repoRoot, 'contracts'))) {
    for (const name of await listDirectories(join(repoRoot, 'contracts', domain))) {
      const projectRoot = join(repoRoot, 'contracts', domain, name);
      if (!existsSync(join(projectRoot, 'project.json'))) continue;
      const project = await loadProject(projectRoot, domain, 'contract');
      if (project) projects.push(project);
    }
  }

  return projects;
}

function isAllowed(dependent, dependency) {
  if (dependent.type === 'tool' || dependent.type === 'e2e') return true;
  if (dependent.type === 'contract') return dependency.type === 'contract';
  if (dependency.type === 'contract') return true;

  if (dependent.domain === 'platform') {
    return dependency.domain === 'platform' && dependency.type === 'lib';
  }

  if (dependent.type === 'lab') {
    return (
      dependency.type === 'lib' &&
      (dependency.domain === dependent.domain || dependency.domain === 'platform')
    );
  }

  if (dependency.type === 'lib') {
    return dependency.domain === dependent.domain || dependency.domain === 'platform';
  }

  return false;
}

async function main() {
  const projects = await discoverProjects();
  const byName = new Map();

  for (const project of projects) {
    if (byName.has(project.name)) {
      fail(
        project.name,
        `duplicate project name (${byName.get(project.name).root} and ${project.root})`,
      );
      continue;
    }
    byName.set(project.name, project);
  }

  for (const project of projects) {
    for (const dependencyName of project.dependencies) {
      const dependency = byName.get(dependencyName);
      if (!dependency) continue;

      if (dependency === project) {
        fail(project.name, 'must not depend on itself');
        continue;
      }

      if (!isAllowed(project, dependency)) {
        fail(
          project.name,
          `(${project.type}) is not allowed to depend on ${dependency.name} (${dependency.type}, domain:${dependency.domain})`,
        );
      }

      if (
        (dependency.type === 'app' || dependency.type === 'service' || dependency.type === 'lab') &&
        project.type !== 'e2e'
      ) {
        fail(project.name, `must not depend on ${dependency.type} ${dependency.name}`);
      }

      if (
        dependency.type === 'tool' &&
        project.type !== 'tool' &&
        project.productionDependencies.includes(dependencyName)
      ) {
        fail(project.name, `production code must not depend on tool ${dependency.name}`);
      }
    }
  }

  const summary = projects
    .map((project) => `${project.name} [${project.domain}/${project.type}]`)
    .join(', ');

  if (errors.length > 0) {
    console.error(`Project boundaries: ${errors.length} error(s)`);
    for (const error of errors) {
      console.error(`ERROR: ${error}`);
    }
    process.exitCode = 1;
    return;
  }

  console.log(`Project boundaries: ${projects.length} project(s) checked`);
  console.log(`OK: ${summary}`);
}

await main();
