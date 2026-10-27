/** 核验导出的上游文件及独立准备的实验代码。 */
import { createHash } from 'node:crypto'
import { lstatSync, readFileSync, readlinkSync, realpathSync } from 'node:fs'
import { dirname, isAbsolute, resolve, sep } from 'node:path'
import { fileURLToPath } from 'node:url'

const materials = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const expectedCommit = 'dd6322d604e00eec1ba5e0c8541159906a21094a'
const hash = data => createHash('sha256').update(data).digest('hex')

function checkedFile(relative) {
  if (typeof relative !== 'string' || isAbsolute(relative) || relative.split('/').some(part => !part || part === '..' || part.toLowerCase() === 'old')) {
    throw new Error('来源清单路径不安全')
  }
  const absolute = resolve(materials, relative)
  if (!absolute.startsWith(materials + sep)) throw new Error('来源记录路径超出镜像材料目录')
  const target = realpathSync(absolute)
  if (!target.startsWith(materials + sep)) throw new Error('来源记录链接超出镜像材料目录')
  return absolute
}

function verifyInventory(files) {
  if (!Array.isArray(files) || files.length === 0) throw new Error('来源清单为空')
  for (const entry of files) {
    const path = checkedFile(entry.path)
    const info = lstatSync(path)
    let contents
    if (entry.type === 'symlink') {
      if (!info.isSymbolicLink() || readlinkSync(path) !== entry.target) throw new Error('源码符号链接不匹配')
      contents = Buffer.from(readlinkSync(path))
    } else if (entry.type === 'file') {
      if (!info.isFile() || info.isSymbolicLink()) throw new Error('源码文件类型不匹配')
      contents = readFileSync(path)
    } else {
      throw new Error('未知的来源记录文件类型')
    }
    if (hash(contents) !== entry.sha256) throw new Error(`镜像材料哈希不匹配：${entry.path}`)
  }
}

export function verifySource({ sourceOnly = false } = {}) {
  const manifestBytes = readFileSync(resolve(materials, 'SOURCE_MANIFEST.json'))
  const source = JSON.parse(manifestBytes)
  if (source.schema !== 'harness-lab-source-manifest/1' || source.candidate !== 'deepseek_harness') throw new Error('源码清单错误')
  if (source.sources.length !== 1 || source.sources[0].id !== 'main' || source.sources[0].commit !== expectedCommit || source.sources[0].observed_commit !== expectedCommit || source.sources[0].dirty !== false) {
    throw new Error('DSH 源码提交或干净导出的来源记录不匹配')
  }
  verifyInventory(source.files)
  const provenance = { candidate: 'deepseek_harness', commit: expectedCommit, source_export_sha256: source.export_sha256, source_file_count: source.files.length }
  if (sourceOnly) return provenance
  const adapter = JSON.parse(readFileSync(resolve(materials, 'LAB_ADAPTER_MANIFEST.json'), 'utf8'))
  if (adapter.schema !== 'harness-lab-adapter-manifest/1' || adapter.candidate !== 'deepseek_harness' || adapter.source_manifest_sha256 !== hash(manifestBytes)) throw new Error('适配器与源码清单不匹配')
  verifyInventory(adapter.files)
  return { ...provenance, adapter_export_sha256: adapter.export_sha256, adapter_file_count: adapter.files.length }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const args = process.argv.slice(2)
  if (args.length > 1 || (args.length === 1 && args[0] !== '--source-only')) throw new Error('用法：verify-source.mjs [--source-only]')
  console.log(JSON.stringify(verifySource({ sourceOnly: args[0] === '--source-only' })))
}
