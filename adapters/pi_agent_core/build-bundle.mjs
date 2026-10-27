/** 构建可追溯到源码的子集，不使用在线生成的模型目录。 */
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
if (version !== expectedVersion || version !== '0.28.2') throw new Error('固定的源码编译器版本与预期不符')
if (hash(readFileSync(resolve(root, 'package-lock.json'))) !== hash(readFileSync(resolve(original, 'package-lock.json')))) throw new Error('编译目录的依赖锁与源码不一致')
const aliases = {
  '@earendil-works/pi-agent-core': resolve(root, 'packages/agent/src/index.ts'),
  '@earendil-works/pi-ai': resolve(root, 'packages/ai/src/index.ts'),
  '@earendil-works/pi-telemetry': resolve(root, 'packages/telemetry/src/index.ts'),
}
// npm 将 AI 包依赖的 OpenAI 7 保留在此工作区下，因为另一个
// 工作区固定使用 OpenAI 6。保留 Node 官方的工作区解析方式。
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
  // 上游 tsconfig 将 "typebox" 映射到 node_modules。显式声明的外部依赖
  // 会在路径重写前生效，使包注册表依赖保持外置。
  external: ['typebox', 'typebox/*', 'openai', 'openai/*', 'partial-json'],
  packages: 'external', treeShaking: true, sourcemap: 'external', sourcesContent: true,
  metafile: true, legalComments: 'linked',
})
const inputs = []
for (const [name, metadata] of Object.entries(result.metafile.inputs)) {
  if (name === 'harness-lab-public-entry.ts' || name === '<stdin>') continue
  const path = isAbsolute(name) ? name : resolve(root, name)
  const rel = relative(root, path)
  if (rel.startsWith('../') || isAbsolute(rel) || rel.split('/').includes('node_modules')) throw new Error(`打包依赖输入与预期不符: ${name}`)
  const manifestPath = 'sources/main/' + rel
  const expected = inventory.get(manifestPath)
  const contents = readFileSync(path)
  if (!expected || expected.type !== 'file' || expected.sha256 !== hash(contents) || hash(readFileSync(resolve(original, rel))) !== expected.sha256) throw new Error(`编译输入与已核验源码不匹配: ${name}`)
  inputs.push({ path: manifestPath, sha256: expected.sha256, bytes: metadata.bytes })
}
if (!inputs.some(input => input.path.endsWith('/packages/agent/src/agent-loop.ts')) || !inputs.some(input => input.path.endsWith('/packages/ai/src/api/openai-completions.ts'))) throw new Error('编译输入必须包含官方 Agent 循环和聊天 API')
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
