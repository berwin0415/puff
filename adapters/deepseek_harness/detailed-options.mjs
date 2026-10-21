/** DSH-only trusted configuration. This module imports no candidate runtime. */
import { constants, closeSync, fstatSync, lstatSync, openSync, readFileSync, realpathSync, writeFileSync, fsyncSync } from 'node:fs'
import { createHash } from 'node:crypto'
import { isAbsolute, resolve, sep } from 'node:path'

const id = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/u
const scopeFields = ['experiment_id', 'variant', 'user_id', 'session_id']

function objectFields(value, required, optional = []) {
  if (!value || typeof value !== 'object' || Array.isArray(value) || required.some(key => !Object.hasOwn(value, key)) || Object.keys(value).some(key => !required.includes(key) && !optional.includes(key))) throw new Error('Invalid DSH detailed configuration fields')
}

export function validateDetailedOptions(value) {
  objectFields(value, ['case', 'trusted_scope_file'], ['browser', 'business_extension', 'session_action', 'followup'])
  if (!['tools', 'browser', 'sensitive', 'extension'].includes(value.case) || value.trusted_scope_file !== '/config/scope.json') throw new Error('Invalid DSH detailed case or scope binding path')
  if (Object.hasOwn(value, 'session_action') && !['create', 'resume'].includes(value.session_action)) throw new Error('Expected an explicit create or resume session action')
  if (Object.hasOwn(value, 'followup') && (value.session_action !== 'resume' || typeof value.followup !== 'string' || value.followup.trim().length === 0 || value.followup.length > 65536)) throw new Error('Only resume accepts a bounded, explicit new followup')
  if (value.case === 'browser') {
    objectFields(value.browser, ['url', 'credentials_file', 'artifacts_dir'])
    if (value.browser.url !== 'http://browser:8931/mcp' || value.browser.credentials_file !== '/config/browser-credentials.json' || value.browser.artifacts_dir !== '/browser-artifacts') throw new Error('Unexpected browser endpoint or trusted mount')
  } else if (value.browser !== undefined) throw new Error('Browser configuration belongs only to the browser case')
  if (value.case === 'extension') {
    objectFields(value.business_extension, ['quantity_limit'])
    if (value.business_extension.quantity_limit !== 3) throw new Error('Expected the frozen business-extension quantity limit')
  } else if (value.business_extension !== undefined) throw new Error('Business extension configuration belongs only to the extension case')
  return structuredClone(value)
}

/** A resumed session never implicitly replays the common request prompt. */
export function selectedSessionInput(options, initialPrompt) {
  if ((options.session_action ?? 'create') === 'create') return initialPrompt
  if (options.session_action === 'resume') return options.followup
  throw new Error('Invalid session action')
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

export function sameScope(left, right) {
  return !!left && typeof left === 'object' && scopeFields.every(key => left[key] === right[key]) && Object.keys(left).length === scopeFields.length
}

export function readBrowserCredentials(filename) {
  const value = readTrustedJson(filename)
  objectFields(value, ['url', 'username', 'password'])
  if (value.url !== 'http://site:8081' || typeof value.username !== 'string' || !id.test(value.username) || typeof value.password !== 'string' || value.password.length < 1 || value.password.length > 1024 || /[\r\n\u0000]/u.test(value.password)) throw new Error('Invalid trusted browser test credentials')
  return value
}

/** Only a completed native MCP download event may name a file to export. */
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
  // The MCP server prints a path relative to --output-dir, or the absolute
  // path in its own container. Both map to this read-only, per-run mount.
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
  // This browser case has no file-writing Agent tools. Refuse to overwrite an
  // existing destination so stale files cannot silently replace provenance.
  const target = openSync(destination, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW, 0o600)
  try { writeFileSync(target, data); fsyncSync(target) } finally { closeSync(target) }
  return { native_path: nativePath, source_file: source, destination, bytes: data.length, sha256: createHash('sha256').update(data).digest('hex') }
}
