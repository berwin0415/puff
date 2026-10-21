/** Verify immutable official exports, adapter files and compiled source inputs. */
import { createHash } from 'node:crypto'
import { lstatSync, readFileSync, readlinkSync, realpathSync } from 'node:fs'
import { dirname, isAbsolute, resolve, sep } from 'node:path'
import { fileURLToPath } from 'node:url'

const materials = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const commit = '73f8de80bc87021375f8b525292ac07492253002'
const archiveHash = 'ce969e2917f93ed49d8be62fc7e7a0a02caa02959f40c28c2fb87f2a62639fd8'
const exportHash = '493304f15f7b2f15789a00353eb16215dd27346948a99e51372c4369c3dbda59'
const hash = data => createHash('sha256').update(data).digest('hex')

function checkedFile(root, relative) {
  if (typeof relative !== 'string' || isAbsolute(relative) || relative.split('/').some(part => !part || part === '.' || part === '..' || part.toLowerCase() === 'old')) throw new Error('Unsafe provenance path')
  const path = resolve(root, relative)
  if (!path.startsWith(root + sep) || !realpathSync(path).startsWith(root + sep)) throw new Error('Provenance path escapes image materials')
  return path
}

function verifyInventory(entries) {
  if (!Array.isArray(entries) || !entries.length) throw new Error('Empty provenance inventory')
  const seen = new Set()
  for (const entry of entries) {
    if (seen.has(entry.path)) throw new Error('Duplicate provenance entry')
    seen.add(entry.path)
    const path = checkedFile(materials, entry.path)
    const info = lstatSync(path)
    let contents
    if (entry.type === 'symlink') {
      if (!info.isSymbolicLink() || readlinkSync(path) !== entry.target) throw new Error('Source symbolic link mismatch')
      contents = Buffer.from(readlinkSync(path))
    } else if (entry.type === 'file') {
      if (!info.isFile() || info.isSymbolicLink()) throw new Error('Source file kind mismatch')
      contents = readFileSync(path)
      if (contents.length !== entry.size) throw new Error('Source file size mismatch')
    } else throw new Error('Unknown provenance file kind')
    if (hash(contents) !== entry.sha256) throw new Error(`Image material hash mismatch: ${entry.path}`)
  }
}

export function verifySource({ sourceOnly = false } = {}) {
  const bytes = readFileSync(resolve(materials, 'SOURCE_MANIFEST.json'))
  const source = JSON.parse(bytes)
  const pin = source.sources?.[0]
  if (source.schema !== 'harness-lab-source-manifest/1' || source.candidate !== 'pi_agent_core' || source.export_sha256 !== exportHash || source.sources?.length !== 1 || pin.id !== 'main' || pin.kind !== 'archive' || pin.commit !== commit || pin.sha256 !== archiveHash || pin.observed_archive_sha256 !== archiveHash || pin.dirty !== null) throw new Error('Wrong or unverified Pi source archive')
  verifyInventory(source.files)
  const packages = ['agent', 'ai', 'telemetry'].map(name => {
    const pkg = JSON.parse(readFileSync(resolve(materials, 'sources/main/packages', name, 'package.json'), 'utf8'))
    const expectedName = name === 'agent' ? '@earendil-works/pi-agent-core' : '@earendil-works/pi-' + name
    if (pkg.name !== expectedName || pkg.version !== '1.3.0') throw new Error('Wrong official source package identity/version')
    return { name: pkg.name, version: pkg.version }
  })
  const provenance = { candidate: 'pi_agent_core', commit, archive_sha256: archiveHash, source_export_sha256: source.export_sha256, source_file_count: source.files.length, source_packages: packages }
  if (sourceOnly) return provenance
  const adapter = JSON.parse(readFileSync(resolve(materials, 'LAB_ADAPTER_MANIFEST.json'), 'utf8'))
  if (adapter.schema !== 'harness-lab-adapter-manifest/1' || adapter.candidate !== 'pi_agent_core' || adapter.source_manifest_sha256 !== hash(bytes)) throw new Error('Adapter/source manifest mismatch')
  verifyInventory(adapter.files)
  const buildBytes = readFileSync(resolve(materials, 'PI_BUILD_MANIFEST.json'))
  const build = JSON.parse(buildBytes)
  const upstreamLockHash = hash(readFileSync(resolve(materials, 'sources/main/package-lock.json')))
  if (build.schema !== 'harness-lab-pi-build/1' || build.candidate !== 'pi_agent_core' || build.source_manifest_sha256 !== hash(bytes) || build.upstream_lock_sha256 !== upstreamLockHash || build.compiler?.name !== 'esbuild' || build.compiler?.version !== '0.28.2' || JSON.stringify(build.source_packages) !== JSON.stringify(packages)) throw new Error('Compiled Pi source provenance mismatch')
  if (!Array.isArray(build.inputs) || !build.inputs.length || !Array.isArray(build.outputs) || !build.outputs.some(entry => entry.path === 'packages/ai/pi-core.bundle.mjs')) throw new Error('Incomplete compiled input/output inventory')
  const sourceFiles = new Map(source.files.map(entry => [entry.path, entry.sha256]))
  for (const input of build.inputs) {
    if (sourceFiles.get(input.path) !== input.sha256 || hash(readFileSync(checkedFile(materials, input.path))) !== input.sha256) throw new Error('Compiled input differs from verified source')
  }
  for (const output of build.outputs) {
    const path = checkedFile('/opt/pi-build', output.path)
    if (!lstatSync(path).isFile() || lstatSync(path).isSymbolicLink() || hash(readFileSync(path)) !== output.sha256) throw new Error('Compiled Pi bundle hash mismatch')
  }
  const installed = {}
  for (const [name, expected] of [['esbuild', '0.28.2'], ['openai', '7.19.0'], ['typebox', '1.3.27']]) {
    const packageRoot = name === 'openai' ? '/opt/pi-build/packages/ai/node_modules' : '/opt/pi-build/node_modules'
    const pkg = JSON.parse(readFileSync(resolve(packageRoot, name, 'package.json'), 'utf8'))
    if (pkg.name !== name || pkg.version !== expected) throw new Error('Wrong frozen dependency installation')
    installed[name] = pkg.version
  }
  return { ...provenance, adapter_export_sha256: adapter.export_sha256, adapter_file_count: adapter.files.length, build_manifest_sha256: hash(buildBytes), bundle_input_count: build.inputs.length, compiled_outputs: build.outputs, upstream_lock_sha256: upstreamLockHash, installed_dependencies: installed }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const args = process.argv.slice(2)
  if (args.length > 1 || (args.length === 1 && args[0] !== '--source-only')) throw new Error('Usage: verify-source.mjs [--source-only]')
  process.stdout.write(JSON.stringify(verifySource({ sourceOnly: args[0] === '--source-only' })) + '\n')
}
