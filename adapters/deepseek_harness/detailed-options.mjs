/** DSH 专用可信配置；此模块不导入候选运行时。 */
import { constants, closeSync, fstatSync, lstatSync, openSync, readFileSync, realpathSync, writeFileSync, fsyncSync } from 'node:fs'
import { createHash } from 'node:crypto'
import { isAbsolute, resolve, sep } from 'node:path'

const id = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/u
const scopeFields = ['experiment_id', 'variant', 'user_id', 'session_id']

function objectFields(value, required, optional = []) {
  if (!value || typeof value !== 'object' || Array.isArray(value) || required.some(key => !Object.hasOwn(value, key)) || Object.keys(value).some(key => !required.includes(key) && !optional.includes(key))) throw new Error('DSH 详细验证配置字段无效')
}

export function validateDetailedOptions(value) {
  objectFields(value, ['case', 'trusted_scope_file'], ['browser', 'business_extension', 'session_action', 'followup'])
  if (!['tools', 'browser', 'sensitive', 'extension'].includes(value.case) || value.trusted_scope_file !== '/config/scope.json') throw new Error('DSH 详细验证用例或作用域绑定路径无效')
  if (Object.hasOwn(value, 'session_action') && !['create', 'resume'].includes(value.session_action)) throw new Error('必须明确指定 create 或 resume 会话操作')
  if (Object.hasOwn(value, 'followup') && (value.session_action !== 'resume' || typeof value.followup !== 'string' || value.followup.trim().length === 0 || value.followup.length > 65536)) throw new Error('仅 resume 接受长度受限且显式提供的新跟进消息')
  if (value.case === 'browser') {
    objectFields(value.browser, ['url', 'credentials_file', 'artifacts_dir'])
    if (value.browser.url !== 'http://browser:8931/mcp' || value.browser.credentials_file !== '/config/browser-credentials.json' || value.browser.artifacts_dir !== '/browser-artifacts') throw new Error('浏览器接口或可信挂载不符合预期')
  } else if (value.browser !== undefined) throw new Error('浏览器配置仅适用于浏览器用例')
  if (value.case === 'extension') {
    objectFields(value.business_extension, ['quantity_limit'])
    if (value.business_extension.quantity_limit !== 3) throw new Error('必须使用冻结的业务扩展数量上限')
  } else if (value.business_extension !== undefined) throw new Error('业务扩展配置仅适用于扩展用例')
  return structuredClone(value)
}

/** 恢复会话时不隐式重放通用请求提示词。 */
export function selectedSessionInput(options, initialPrompt) {
  if ((options.session_action ?? 'create') === 'create') return initialPrompt
  if (options.session_action === 'resume') return options.followup
  throw new Error('会话操作无效')
}

export function checkedExistingPath(value) {
  if (typeof value !== 'string' || !isAbsolute(value) || value.split('/').some(part => part === '..' || part.toLowerCase() === 'old')) throw new Error('必须使用归档目录以外的绝对路径')
  const absolute = resolve(value)
  let current = '/'
  for (const part of absolute.split('/').filter(Boolean)) {
    current = resolve(current, part)
    if (lstatSync(current).isSymbolicLink()) throw new Error('可信路径不得跟随符号链接')
  }
  if (realpathSync(absolute) !== absolute) throw new Error('可信路径在解析期间发生变化')
  return absolute
}

export function readTrustedJson(filename) {
  const descriptor = openSync(checkedExistingPath(filename), constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK)
  try {
    const info = fstatSync(descriptor)
    if (!info.isFile() || info.size > 65536) throw new Error('必须提供大小受限的可信 JSON 文件')
    return JSON.parse(readFileSync(descriptor, 'utf8'))
  } finally { closeSync(descriptor) }
}

export function readTrustedScope(filename, sessionId) {
  const scope = readTrustedJson(filename)
  objectFields(scope, scopeFields)
  if (scopeFields.some(key => typeof scope[key] !== 'string' || !id.test(scope[key]) || scope[key].toLowerCase() === 'old') || scope.session_id !== sessionId) throw new Error('可信任务或会话身份不匹配')
  return Object.freeze(scope)
}

export function sameScope(left, right) {
  return !!left && typeof left === 'object' && scopeFields.every(key => left[key] === right[key]) && Object.keys(left).length === scopeFields.length
}

export function readBrowserCredentials(filename) {
  const value = readTrustedJson(filename)
  objectFields(value, ['url', 'username', 'password'])
  if (value.url !== 'http://site:8081' || typeof value.username !== 'string' || !id.test(value.username) || typeof value.password !== 'string' || value.password.length < 1 || value.password.length > 1024 || /[\r\n\u0000]/u.test(value.password)) throw new Error('可信浏览器测试凭证无效')
  return value
}

/** 仅已完成的原生 MCP 下载事件可指定待导出文件。 */
export function nativeDownloadPaths(value) {
  const paths = []
  for (const block of value?.content ?? []) {
    if (block?.type !== 'text' || typeof block.text !== 'string') continue
    let inEvents = false
    let fence
    for (const line of block.text.split(/\r?\n/u)) {
      const marker = /^ {0,3}(`{3,}|~{3,})(.*)$/u.exec(line)
      if (fence) {
        if (marker && marker[1][0] === fence[0] && marker[1].length >= fence.length && marker[2].trim() === '') fence = undefined
        continue
      }
      if (marker) { fence = marker[1]; continue }
      if (/^#{1,6}(?:[ \t]|$)/u.test(line)) {
        inEvents = /^### Events[ \t]*$/u.test(line)
        continue
      }
      if (!inEvents) continue
      const match = /^- Downloaded file [^\r\n]+ to "([^"\r\n]+)"[ \t]*$/u.exec(line)
      if (match) paths.push(match[1])
    }
  }
  return [...new Set(paths)]
}

export function copyNativeDownload(nativePath, artifactRoot, workspace) {
  // MCP 服务端输出相对 --output-dir 的路径，或其容器内的
  // 绝对路径；两者均映射到本次运行的只读挂载。
  let relative = nativePath
  const serverRoot = '/data/browser-artifacts/'
  if (relative.startsWith(serverRoot)) relative = relative.slice(serverRoot.length)
  if (relative.startsWith('./')) relative = relative.slice(2)
  if (typeof relative !== 'string' || isAbsolute(relative) || relative.includes('\\') || relative.split('/').some(part => !part || part === '.' || part === '..' || part.toLowerCase() === 'old')) throw new Error('MCP 下载路径超出其产物目录')
  const root = checkedExistingPath(artifactRoot)
  const source = checkedExistingPath(resolve(root, relative))
  if (!source.startsWith(root + sep)) throw new Error('MCP 下载文件超出其产物挂载目录')
  const descriptor = openSync(source, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK)
  let data
  try {
    const info = fstatSync(descriptor)
    if (!info.isFile() || info.size > 2 * 1024 * 1024) throw new Error('MCP 下载文件不是大小受限的普通文件')
    data = readFileSync(descriptor)
  } finally { closeSync(descriptor) }
  const destination = resolve(checkedExistingPath(workspace), 'result.csv')
  // 此浏览器用例没有 Agent 文件写入工具；拒绝覆盖已有目标文件，
  // 避免旧文件静默替换来源证据。
  const target = openSync(destination, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW, 0o600)
  try { writeFileSync(target, data); fsyncSync(target) } finally { closeSync(target) }
  return { native_path: nativePath, source_file: source, destination, bytes: data.length, sha256: createHash('sha256').update(data).digest('hex') }
}
