/** 核验源码和发布材料清单，不导入或启动 SDK。 */
import { createHash } from 'node:crypto'
import { lstatSync, readFileSync, readlinkSync, realpathSync } from 'node:fs'
import { dirname, isAbsolute, relative, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const materials = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const commit = '83086188ae0614bd882818ff1aa23f3194962012'
const archiveHash = 'e57f1d4f914c71c3532a12d5c14f740909fca4e7e041fa1b3db71618c88e5567'
const exportHash = 'c3c767a93ef93cdc22666039a48be5a2292b0929c48bd933d39500e097fcd156'
const sha256 = data => createHash('sha256').update(data).digest('hex')
const json = path => JSON.parse(readFileSync(resolve(materials, path), 'utf8'))
// Python 生成源码清单时使用排序后的键和默认分隔符。
const canonical = value => {
  if (Array.isArray(value)) return '[' + value.map(canonical).join(', ') + ']'
  if (value !== null && typeof value === 'object') return '{' + Object.keys(value).sort().map(key => JSON.stringify(key) + ': ' + canonical(value[key])).join(', ') + '}'
  return JSON.stringify(value)
}

function inventory(files) {
  if (!Array.isArray(files) || files.length === 0) throw new Error('材料清单为空')
  const seen = new Set()
  for (const entry of files) {
    if (typeof entry.path !== 'string' || isAbsolute(entry.path) || entry.path.split('/').some(part => ['', '.', '..'].includes(part) || part.toLowerCase() === 'old') || seen.has(entry.path)) throw new Error('材料路径不安全或重复')
    seen.add(entry.path)
    const path = resolve(materials, entry.path)
    const stat = lstatSync(path)
    const actual = realpathSync(path)
    if (relative(materials, actual).startsWith('..') || isAbsolute(relative(materials, actual))) throw new Error('材料链接指向镜像上下文之外')
    let bytes
    if (entry.type === 'symlink') {
      if (!stat.isSymbolicLink() || readlinkSync(path) !== entry.target) throw new Error('源码链接不匹配')
      bytes = Buffer.from(readlinkSync(path))
    } else if (entry.type === 'file') {
      if (!stat.isFile() || stat.isSymbolicLink()) throw new Error('材料文件类型不匹配')
      bytes = readFileSync(path)
      if (bytes.length !== entry.size || (entry.mode !== undefined && (stat.mode & 0o777) !== entry.mode)) throw new Error('材料大小或权限不匹配')
    } else throw new Error('未知的材料文件类型')
    if (sha256(bytes) !== entry.sha256) throw new Error(`材料哈希不匹配: ${entry.path}`)
  }
}

export function verifySource() {
  const sourceBytes = readFileSync(resolve(materials, 'SOURCE_MANIFEST.json'))
  const source = JSON.parse(sourceBytes)
  const pin = source.sources?.[0]
  if (source.schema !== 'harness-lab-source-manifest/1' || source.candidate !== 'codex_sdk' || source.sources?.length !== 1 || source.export_sha256 !== exportHash || pin.commit !== commit || pin.sha256 !== archiveHash || pin.observed_archive_sha256 !== archiveHash || pin.dirty !== null) throw new Error('官方源码错误或尚未核验')
  inventory(source.files)
  if (sha256(canonical(source.files)) !== exportHash) throw new Error('源码清单摘要不匹配')
  const registryBytes = readFileSync(resolve(materials, 'REGISTRY_MANIFEST.json'))
  const registry = JSON.parse(registryBytes)
  if (registry.schema !== 'harness-lab-npm-release-manifest/1' || registry.candidate !== 'codex_sdk' || registry.source_commit !== commit || registry.target !== 'linux-arm64' || registry.packages?.length !== 3) throw new Error('发布依赖集合不正确')
  inventory(registry.files)
  if (sha256(canonical(registry.files)) !== registry.export_sha256) throw new Error('发布清单摘要不匹配')
  const expected = [
    ['sdk', '@openai/codex-sdk', '0.163.0', '@openai/codex-sdk', 'sha512-o10d8dqxFjhzoWp75MUrhaN8C6QrXPQqUJGwVnySj++ztAnrA322UuFRzHZvSa903lFgjjaimuf5rkqVsj0RHg=='],
    ['cli', '@openai/codex', '0.163.0', '@openai/codex', 'sha512-HNd7vR9oTK6M2ikxFnvNDCdYUgAsyo9VVnd5KnEd2ON0GYI++7MiX9XNPUkhtdgGKFPsWS+CEppQ1UU63uVhUw=='],
    ['linux-arm64', '@openai/codex', '0.163.0-linux-arm64', '@openai/codex-linux-arm64', 'sha512-IH2/R+4OarjGvsAf1ll3fdyLwrPgYLl3uzp6u9CyD9sj2Lw+ViDSe3QBZVHdcHJs0eIiZFg6Wprcuy4CRZaUGw=='],
  ]
  for (const [index, [id, name, version, installName, integrity]] of expected.entries()) {
    const record = registry.packages[index]
    const metadata = json(`registry/${id}.metadata.json`)
    const tarball = readFileSync(resolve(materials, `registry/${id}.tgz`))
    const sha512 = createHash('sha512').update(tarball).digest()
    if (record.id !== id || record.name !== name || record.version !== version || record.install_name !== installName || record.source_commit !== commit || record.integrity !== integrity || metadata.name !== name || metadata.version !== version || metadata.dist.integrity !== integrity || 'sha512-' + sha512.toString('base64') !== integrity) throw new Error('固定的 npm 发布身份不匹配')
    let provenanceSeen = false
    for (const attestation of json(`registry/${id}.attestations.json`).attestations) {
      const statement = JSON.parse(Buffer.from(attestation.bundle.dsseEnvelope.payload, 'base64'))
      if (!statement.subject?.some(subject => subject.digest?.sha512 === sha512.toString('hex'))) throw new Error('发布证明的摘要不匹配')
      if (statement.predicateType !== 'https://slsa.dev/provenance/v1') continue
      const definition = statement.predicate?.buildDefinition
      if (definition?.externalParameters?.workflow?.repository !== 'https://github.com/openai/codex' || definition.externalParameters.workflow.ref !== 'refs/tags/rust-v0.163.0' || !definition.resolvedDependencies?.some(item => item.uri?.startsWith('git+https://github.com/openai/codex@') && item.digest?.gitCommit === commit)) throw new Error('发布证明的源码不匹配')
      provenanceSeen = true
    }
    if (!provenanceSeen) throw new Error('缺少发布材料的源码来源证明')
    const installed = json(`packages/${installName}/package.json`)
    if (installed.name !== name || installed.version !== version) throw new Error('解压后的包版本不正确')
  }
  const sdk = json('packages/@openai/codex-sdk/package.json')
  const cli = json('packages/@openai/codex/package.json')
  const platform = json('packages/@openai/codex-linux-arm64/package.json')
  if (JSON.stringify(sdk.dependencies) !== JSON.stringify({ '@openai/codex': '0.163.0' }) || cli.dependencies || cli.optionalDependencies?.['@openai/codex-linux-arm64'] !== 'npm:@openai/codex@0.163.0-linux-arm64' || platform.dependencies || platform.optionalDependencies || JSON.stringify(platform.os) !== '["linux"]' || JSON.stringify(platform.cpu) !== '["arm64"]') throw new Error('发布的依赖集合发生变化')
  const adapter = json('LAB_ADAPTER_MANIFEST.json')
  if (adapter.schema !== 'harness-lab-adapter-manifest/1' || adapter.candidate !== 'codex_sdk' || adapter.source_manifest_sha256 !== sha256(sourceBytes) || adapter.registry_manifest_sha256 !== sha256(registryBytes)) throw new Error('适配器、源码或发布清单不匹配')
  inventory(adapter.files)
  if (sha256(canonical(adapter.files)) !== adapter.export_sha256) throw new Error('适配器清单摘要不匹配')
  return { candidate: 'codex_sdk', source_commit: commit, source_export_sha256: exportHash, source_file_count: source.files.length, release_version: '0.163.0', registry_export_sha256: registry.export_sha256, registry_file_count: registry.files.length, adapter_export_sha256: adapter.export_sha256, release_installation: 'integrity_verified_official_npm_tarballs;_source_linked_attestation_contents;_no_independent_Sigstore_signature_chain_verification' }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) console.log(JSON.stringify(verifySource()))
