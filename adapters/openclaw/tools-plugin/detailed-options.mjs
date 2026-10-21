/** Trusted experiment inputs and evidence helpers; imports no Agent runtime. */
import { constants, closeSync, fstatSync, lstatSync, openSync, readFileSync, realpathSync, writeFileSync, appendFileSync, fsyncSync, existsSync } from 'node:fs'
import { createHash, randomUUID } from 'node:crypto'
import { basename, isAbsolute, resolve, sep } from 'node:path'

const id = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/u
const scopeFields = ['experiment_id', 'variant', 'user_id', 'session_id']
export const BROWSER_TOOLS = Object.freeze(['browser_navigate', 'browser_snapshot', 'browser_fill_form', 'browser_type', 'browser_click', 'browser_tabs', 'browser_wait_for'])

function objectFields(value, required, optional = []) {
  if (!value || typeof value !== 'object' || Array.isArray(value) || required.some(key => !Object.hasOwn(value, key)) || Object.keys(value).some(key => !required.includes(key) && !optional.includes(key))) throw new Error('Invalid OpenClaw detailed configuration fields')
}

export function validateDetailedOptions(value) {
  objectFields(value, ['case', 'trusted_scope_file'], ['browser', 'business_extension'])
  if (!['tools', 'browser', 'sensitive', 'extension'].includes(value.case) || value.trusted_scope_file !== '/config/scope.json') throw new Error('Invalid OpenClaw detailed case or scope path')
  if (value.case === 'browser') {
    objectFields(value.browser, ['url', 'credentials_file', 'artifacts_dir'])
    if (value.browser.url !== 'http://browser:8931/mcp' || value.browser.credentials_file !== '/config/browser-credentials.json' || value.browser.artifacts_dir !== '/browser-artifacts') throw new Error('Unexpected browser endpoint or trusted mount')
  } else if (value.browser !== undefined) throw new Error('Browser configuration belongs only to the browser case')
  if (value.case === 'extension') {
    objectFields(value.business_extension, ['quantity_limit'])
    if (value.business_extension.quantity_limit !== 3) throw new Error('The frozen business extension requires quantity_limit=3')
  } else if (value.business_extension !== undefined) throw new Error('Business extension configuration belongs only to the extension case')
  return structuredClone(value)
}

export function checkedExistingPath(value) {
  if (typeof value !== 'string' || !isAbsolute(value) || value.split('/').some(part => part === '..' || part.toLowerCase() === 'old')) throw new Error('Expected an absolute non-archive path')
  const absolute = resolve(value)
  let current = '/'
  for (const part of absolute.split('/').filter(Boolean)) {
    current = resolve(current, part)
    if (lstatSync(current).isSymbolicLink()) throw new Error('Trusted path must not follow symbolic links')
  }
  if (realpathSync(absolute) !== absolute) throw new Error('Trusted path changed during resolution')
  return absolute
}

export function readTrustedJson(filename) {
  const descriptor = openSync(checkedExistingPath(filename), constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK)
  try {
    const info = fstatSync(descriptor)
    if (!info.isFile() || info.size > 65536) throw new Error('Expected a bounded trusted JSON file')
    return JSON.parse(readFileSync(descriptor, 'utf8'))
  } finally { closeSync(descriptor) }
}

export function readTrustedScope(filename, sessionId) {
  const scope = readTrustedJson(filename)
  objectFields(scope, scopeFields)
  if (scopeFields.some(key => typeof scope[key] !== 'string' || !id.test(scope[key]) || scope[key].toLowerCase() === 'old') || scope.session_id !== sessionId) throw new Error('Trusted task/session identity mismatch')
  return Object.freeze(scope)
}

export function readBrowserCredentials(filename) {
  const value = readTrustedJson(filename)
  objectFields(value, ['url', 'username', 'password'])
  if (value.url !== 'http://site:8081' || typeof value.username !== 'string' || !id.test(value.username) || typeof value.password !== 'string' || value.password.length < 1 || value.password.length > 1024 || /[\r\n\u0000]/u.test(value.password)) throw new Error('Invalid trusted browser test credentials')
  return value
}

export function redactor(secrets) {
  const values = [...new Set(secrets.filter(value => typeof value === 'string' && value.length).flatMap(value => {
    const escaped = JSON.stringify(value).slice(1, -1)
    return [value, escaped, JSON.stringify(escaped).slice(1, -1)]
  }))].sort((left, right) => right.length - left.length)
  const redact = value => values.reduce((text, secret) => text.replaceAll(secret, '[redacted-local-secret]'), String(value))
  const json = value => JSON.stringify(value, (_key, item) => typeof item === 'string' ? redact(item) : item)
  return { text: redact, json }
}

/** Source line numbers refer to the actual appended record, including concurrent writers. */
export function appendEvidence(filename, value, redact = redactor([])) {
  const recordId = randomUUID()
  appendFileSync(filename, redact.json({ ...value, record_id: recordId }) + '\n', { mode: 0o600 })
  const lines = readFileSync(filename, 'utf8').split('\n')
  const marker = '"record_id":' + JSON.stringify(recordId)
  const line = lines.findIndex(item => item.includes(marker)) + 1
  if (!line) throw new Error('Appended evidence record is missing')
  return { path: basename(filename), line }
}

export function sanitizeEvidenceFiles(directory, names, redact) {
  for (const name of names) {
    const filename = resolve(directory, name)
    if (!existsSync(filename)) continue
    checkedExistingPath(filename)
    if (!lstatSync(filename).isFile()) throw new Error('Expected regular candidate evidence')
    const original = readFileSync(filename, 'utf8')
    // The redactor includes JSON-escaped forms used in native log messages.
    writeFileSync(filename, redact.text(original), { mode: 0o600 })
  }
}

/** Only a completed native MCP download response may identify an export. */
export function nativeDownloadPaths(value) {
  const paths = []
  for (const block of value?.content ?? []) {
    if (block?.type !== 'text' || typeof block.text !== 'string') continue
    for (const match of block.text.matchAll(/^- Downloaded file [^\r\n]+ to "([^"\r\n]+)"\s*$/gmu)) paths.push(match[1])
  }
  return [...new Set(paths)]
}

export function copyNativeDownload(nativePath, artifactRoot, workspace) {
  let relative = nativePath
  const serverRoot = '/data/browser-artifacts/'
  if (relative.startsWith(serverRoot)) relative = relative.slice(serverRoot.length)
  if (relative.startsWith('./')) relative = relative.slice(2)
  if (typeof relative !== 'string' || isAbsolute(relative) || relative.includes('\\') || relative.split('/').some(part => !part || part === '.' || part === '..' || part.toLowerCase() === 'old')) throw new Error('MCP download path escapes its artifact directory')
  const root = checkedExistingPath(artifactRoot)
  const source = checkedExistingPath(resolve(root, relative))
  if (!source.startsWith(root + sep)) throw new Error('MCP download escapes its artifact mount')
  const descriptor = openSync(source, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK)
  let data
  try {
    const info = fstatSync(descriptor)
    if (!info.isFile() || info.size > 2 * 1024 * 1024) throw new Error('MCP download is not a bounded regular file')
    data = readFileSync(descriptor)
  } finally { closeSync(descriptor) }
  const destination = resolve(checkedExistingPath(workspace), 'result.csv')
  const target = openSync(destination, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW, 0o600)
  try { writeFileSync(target, data); fsyncSync(target) } finally { closeSync(target) }
  return { native_path: nativePath, source, destination, bytes: data.length, sha256: createHash('sha256').update(data).digest('hex') }
}
