/** Build a source-traceable subset without online generated model catalogs. */
import { createHash } from 'node:crypto'
import { readFileSync, writeFileSync } from 'node:fs'
import { isAbsolute, relative, resolve } from 'node:path'
import { build, version } from '/opt/pi-build/node_modules/esbuild/lib/main.js'

const hash = data => createHash('sha256').update(data).digest('hex')
const root = '/opt/pi-build'
const materials = '/opt/materials'
const original = resolve(materials, 'sources/main')
const source = JSON.parse(readFileSync(resolve(materials, 'SOURCE_MANIFEST.json'), 'utf8'))
const inventory = new Map(source.files.map(entry => [entry.path, entry]))
const expectedVersion = JSON.parse(readFileSync(resolve(root, 'package.json'), 'utf8')).devDependencies.esbuild
if (version !== expectedVersion || version !== '0.28.2') throw new Error('Unexpected locked source compiler version')
if (hash(readFileSync(resolve(root, 'package-lock.json'))) !== hash(readFileSync(resolve(original, 'package-lock.json')))) throw new Error('Compiler tree dependency lock differs from source')
const aliases = {
  '@earendil-works/pi-agent-core': resolve(root, 'packages/agent/src/index.ts'),
  '@earendil-works/pi-ai': resolve(root, 'packages/ai/src/index.ts'),
  '@earendil-works/pi-telemetry': resolve(root, 'packages/telemetry/src/index.ts'),
}
// npm keeps AI's OpenAI 7 dependency below this workspace because another
// workspace pins OpenAI 6. Preserve Node's official workspace resolution.
const output = resolve(root, 'packages/ai/pi-core.bundle.mjs')
const result = await build({
  absWorkingDir: root,
  stdin: {
    contents: 'export { Agent } from "@earendil-works/pi-agent-core"; export { streamSimple } from "./packages/ai/src/api/openai-completions.ts";',
    resolveDir: root, sourcefile: 'harness-lab-public-entry.ts', loader: 'ts',
  },
  plugins: [{ name: 'verified-official-workspace', setup(build) {
    build.onResolve({ filter: /^@earendil-works\/pi-(agent-core|ai|telemetry)$/ }, args => ({ path: aliases[args.path] }))
  } }],
  outfile: output, bundle: true, platform: 'node', format: 'esm', target: 'node24',
  // Upstream tsconfig maps "typebox" into node_modules. Explicit externals
  // apply before that path rewrite, keeping registry dependencies external.
  external: ['typebox', 'typebox/*', 'openai', 'openai/*', 'partial-json'],
  packages: 'external', treeShaking: true, sourcemap: 'external', sourcesContent: true,
  metafile: true, legalComments: 'linked',
})
const inputs = []
for (const [name, metadata] of Object.entries(result.metafile.inputs)) {
  if (name === 'harness-lab-public-entry.ts' || name === '<stdin>') continue
  const path = isAbsolute(name) ? name : resolve(root, name)
  const rel = relative(root, path)
  if (rel.startsWith('../') || isAbsolute(rel) || rel.split('/').includes('node_modules')) throw new Error(`Unexpected bundled dependency input: ${name}`)
  const manifestPath = 'sources/main/' + rel
  const expected = inventory.get(manifestPath)
  const contents = readFileSync(path)
  if (!expected || expected.type !== 'file' || expected.sha256 !== hash(contents) || hash(readFileSync(resolve(original, rel))) !== expected.sha256) throw new Error(`Compiler input does not match verified source: ${name}`)
  inputs.push({ path: manifestPath, sha256: expected.sha256, bytes: metadata.bytes })
}
if (!inputs.some(input => input.path.endsWith('/packages/agent/src/agent-loop.ts')) || !inputs.some(input => input.path.endsWith('/packages/ai/src/api/openai-completions.ts'))) throw new Error('Expected official Agent loop and chat API inputs')
const outputs = Object.keys(result.metafile.outputs).map(name => {
  const path = isAbsolute(name) ? name : resolve(root, name)
  return { path: relative(root, path), sha256: hash(readFileSync(path)) }
})
const manifest = {
  schema: 'harness-lab-pi-build/1', candidate: 'pi_agent_core',
  source_manifest_sha256: hash(readFileSync(resolve(materials, 'SOURCE_MANIFEST.json'))),
  compiler: { name: 'esbuild', version },
  upstream_lock_sha256: hash(readFileSync(resolve(original, 'package-lock.json'))),
  source_packages: ['agent', 'ai', 'telemetry'].map(name => {
    const pkg = JSON.parse(readFileSync(resolve(original, 'packages', name, 'package.json'), 'utf8'))
    return { name: pkg.name, version: pkg.version }
  }),
  public_entry: 'Agent; api/openai-completions.streamSimple',
  inputs: inputs.sort((a, b) => a.path.localeCompare(b.path)), outputs,
}
writeFileSync(resolve(materials, 'PI_BUILD_MANIFEST.json'), JSON.stringify(manifest, null, 2) + '\n')
writeFileSync(resolve(materials, 'PI_BUNDLE_METAFILE.json'), JSON.stringify(result.metafile, null, 2) + '\n')
