/** 详细用例的 Node 侧支持：可信输入校验、证据记录与下载搬运。 */
import { createHash } from 'node:crypto'
import { appendFileSync, closeSync, existsSync, lstatSync, openSync, readFileSync, readSync, realpathSync, writeFileSync } from 'node:fs'
import { isAbsolute, resolve } from 'node:path'

export const SCOPE_FIELDS = ['experiment_id', 'variant', 'user_id', 'session_id']
export const BROWSER_TOOLS = new Set([
  'browser_navigate', 'browser_navigate_back', 'browser_snapshot', 'browser_click',
  'browser_fill_form', 'browser_type', 'browser_press_key', 'browser_select_option',
  'browser_wait_for', 'browser_tabs', 'browser_handle_dialog',
])
export const REQUIRED_BROWSER_TOOLS = ['browser_navigate', 'browser_snapshot', 'browser_click', 'browser_fill_form']
const IDENTIFIER = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/u

export function trustedPath(value, { directory = false } = {}) {
  if (typeof value !== 'string' || !isAbsolute(value) || value.split('/').some(part => part === '..' || part.toLowerCase() === 'old')) throw new Error('可信路径无效')
  const path = resolve(value)
  let current = '/'
  for (const part of path.split('/').filter(Boolean)) {
    current = resolve(current, part)
    if (existsSync(current) && lstatSync(current).isSymbolicLink()) throw new Error('可信路径不得跟随符号链接')
  }
  return realpathSync(path)
}

export function fields(value, required, optional = []) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('可信详细验证字段无效')
  const keys = Object.keys(value)
  if (required.some(key => !keys.includes(key)) || keys.some(key => !required.includes(key) && !optional.includes(key))) throw new Error('可信详细验证字段无效')
}

export function validateOptions(value) {
  fields(value, ['case', 'trusted_scope_file'], ['browser', 'business_extension'])
  if (!['tools', 'browser', 'sensitive', 'extension'].includes(value.case) || value.trusted_scope_file !== '/config/scope.json') throw new Error('详细验证用例或作用域挂载不符合预期')
  if (value.case === 'browser') {
    const expected = value.browser
    if (!expected || expected.url !== 'http://browser:8931/mcp' || expected.credentials_file !== '/config/browser-credentials.json' || expected.artifacts_dir !== '/browser-artifacts') throw new Error('可信浏览器接口或挂载不符合预期')
  } else if ('browser' in value) throw new Error('浏览器配置仅适用于浏览器用例')
  if (value.case === 'extension') {
    fields(value.business_extension, ['quantity_limit'])
    if (value.business_extension.quantity_limit !== 3) throw new Error('业务数量上限必须保持冻结值 3')
  } else if ('business_extension' in value) throw new Error('业务扩展仅适用于扩展用例')
  return value
}

export function readJson(path) {
  const target = trustedPath(path)
  const info = lstatSync(target)
  if (!info.isFile() || info.size > 65536) throw new Error('必须提供大小受限的可信 JSON 文件')
  return JSON.parse(readFileSync(target, 'utf8'))
}

export function readScope(path, sessionId) {
  const scope = readJson(path)
  fields(scope, SCOPE_FIELDS)
  if (Object.values(scope).some(item => typeof item !== 'string' || !IDENTIFIER.test(item) || item.toLowerCase() === 'old') || scope.session_id !== sessionId) throw new Error('可信作用域或会话身份不匹配')
  return scope
}

export function readCredentials(path, userId) {
  const credentials = readJson(path)
  fields(credentials, ['url', 'username', 'password'])
  if (credentials.url !== 'http://site:8081' || credentials.username !== userId || typeof credentials.password !== 'string' || !credentials.password || credentials.password.length > 1024 || /[\r\n\0]/u.test(credentials.password)) throw new Error('可信浏览器测试凭证无效')
  return credentials
}

/** 只从真实 MCP 完成事件里取下载路径。 */
export function nativeDownloadPaths(result) {
  const paths = []
  for (const block of result?.content ?? []) {
    if (block?.type !== 'text' || typeof block.text !== 'string') continue
    for (const match of block.text.matchAll(/^- Downloaded file [^\r\n]+ to "([^"\r\n]+)"\s*$/gmu)) paths.push(match[1])
  }
  return [...new Set(paths)]
}

export function copyDownload(nativePath, artifactRoot, workspace) {
  const root = trustedPath(artifactRoot)
  const relative = nativePath.replace(/^\/data\/browser-artifacts\//u, '').replace(/^\.\//u, '')
  if (!relative || relative.startsWith('/') || relative.includes('\\') || relative.split('/').some(part => !part || part === '.' || part === '..' || part.toLowerCase() === 'old')) throw new Error('MCP 下载文件超出本次运行的浏览器产物目录')
  const source = trustedPath(resolve(root, relative))
  if (!source.startsWith(root + '/')) throw new Error('MCP 下载文件超出只读产物挂载目录')
  if (!source.toLowerCase().endsWith('.csv')) throw new Error('任务输出必须来自实际下载的 CSV 文件')
  const info = lstatSync(source)
  if (!info.isFile() || info.size > 2 * 1024 * 1024) throw new Error('MCP 下载文件不是大小受限的普通文件')
  const content = readFileSync(source)
  const destination = resolve(workspace, 'result.csv')
  if (existsSync(destination)) throw new Error('目标下载文件已存在')
  writeFileSync(destination, content, { flag: 'wx', mode: 0o600 })
  return { native_path: nativePath, source_file: source, destination, bytes: content.length, sha256: createHash('sha256').update(content).digest('hex') }
}

export class Evidence {
  constructor(directory, encode, scope) {
    this.directory = directory
    this.encode = encode
    this.scope = scope
    this.lines = {}
  }

  append(filename, value) {
    const blob = this.encode(value)
    appendFileSync(resolve(this.directory, filename), blob)
    const line = (this.lines[filename] ?? 0) + 1
    this.lines[filename] = line
    return { path: filename, line, line_sha256: createHash('sha256').update(blob).digest('hex') }
  }

  save(filename, value) {
    writeFileSync(resolve(this.directory, filename), this.encode(value))
  }

  finalize() {
    const files = {}
    for (const name of Object.keys(this.lines)) {
      const data = readFileSync(resolve(this.directory, name))
      files[name] = { lines: this.lines[name], sha256: createHash('sha256').update(data).digest('hex') }
    }
    this.save('evidence-index.json', { scope: this.scope, files })
  }

  now() {
    return new Date().toISOString()
  }
}

/** 业务状态：只由真实回执查询结果聚合。 */
export function aggregateBusinessState(scope, observations) {
  const operations = {}
  let last = null
  for (const { call_id: callId, operation, body, source } of observations) {
    const result = body?.result
    if (!result || result.operation_id !== operation) continue
    const receipts = result.receipts
    const count = result.write_count
    if (!Number.isInteger(count) || count < 1 || !Array.isArray(receipts) || receipts.length !== count) continue
    if (!receipts.every(item => item && SCOPE_FIELDS.every(key => item[key] === scope[key]))) continue
    operations[operation] = {
      operation_id: operation, receipt_confirmed: true, write_count: count, receipts,
      write_acknowledged: true, source, call_id: callId,
      last_observation: 'actual_receipt_query_confirmed',
    }
    last = operation
  }
  return { schema_version: 1, scope: { ...scope }, operation_id: last, receipt_confirmed: Boolean(last), operations }
}
