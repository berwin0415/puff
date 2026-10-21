/** Verify the exported upstream files and separately staged experiment code. */
import { createHash } from 'node:crypto'
import { lstatSync, readFileSync, readlinkSync, realpathSync } from 'node:fs'
import { dirname, isAbsolute, resolve, sep } from 'node:path'
import { fileURLToPath } from 'node:url'

const materials = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const expectedCommit = 'b6e0872bdb7228e22fac7608345e0529a35ec46e'
const hash = data => createHash('sha256').update(data).digest('hex')

function checkedFile(relative) {
  if (typeof relative !== 'string' || isAbsolute(relative) || relative.split('/').some(part => !part || part === '..' || part.toLowerCase() === 'old')) {
    throw new Error('Unsafe provenance manifest path')
  }
  const absolute = resolve(materials, relative)
  if (!absolute.startsWith(materials + sep)) throw new Error('Provenance path escapes image materials')
  const target = realpathSync(absolute)
  if (!target.startsWith(materials + sep)) throw new Error('Provenance link escapes image materials')
  return absolute
}

function verifyInventory(files) {
  if (!Array.isArray(files) || files.length === 0) throw new Error('Empty provenance inventory')
  for (const entry of files) {
    const path = checkedFile(entry.path)
    const info = lstatSync(path)
    let contents
    if (entry.type === 'symlink') {
      if (!info.isSymbolicLink() || readlinkSync(path) !== entry.target) throw new Error('Source symbolic link mismatch')
      contents = Buffer.from(readlinkSync(path))
    } else if (entry.type === 'file') {
      if (!info.isFile() || info.isSymbolicLink()) throw new Error('Source file kind mismatch')
      contents = readFileSync(path)
    } else {
      throw new Error('Unknown provenance file kind')
    }
    if (hash(contents) !== entry.sha256) throw new Error(`Image material hash mismatch: ${entry.path}`)
  }
}

export function verifySource({ sourceOnly = false } = {}) {
  const manifestBytes = readFileSync(resolve(materials, 'SOURCE_MANIFEST.json'))
  const source = JSON.parse(manifestBytes)
  if (source.schema !== 'harness-lab-source-manifest/1' || source.candidate !== 'openclaw') throw new Error('Wrong source manifest')
  if (source.sources.length !== 1 || source.sources[0].id !== 'main' || source.sources[0].commit !== expectedCommit || source.sources[0].observed_commit !== expectedCommit || source.sources[0].dirty !== false) {
    throw new Error('OpenClaw source commit or clean-export provenance mismatch')
  }
  verifyInventory(source.files)
  const provenance = { candidate: 'openclaw', commit: expectedCommit, source_export_sha256: source.export_sha256, source_file_count: source.files.length }
  if (sourceOnly) return provenance
  const adapter = JSON.parse(readFileSync(resolve(materials, 'LAB_ADAPTER_MANIFEST.json'), 'utf8'))
  if (adapter.schema !== 'harness-lab-adapter-manifest/1' || adapter.candidate !== 'openclaw' || adapter.source_manifest_sha256 !== hash(manifestBytes)) throw new Error('Adapter/source manifest mismatch')
  verifyInventory(adapter.files)
  return { ...provenance, adapter_export_sha256: adapter.export_sha256, adapter_file_count: adapter.files.length }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const args = process.argv.slice(2)
  if (args.length > 1 || (args.length === 1 && args[0] !== '--source-only')) throw new Error('Usage: verify-source.mjs [--source-only]')
  console.log(JSON.stringify(verifySource({ sourceOnly: args[0] === '--source-only' })))
}
