"""Stdlib-only trusted detailed inputs and original-evidence bookkeeping."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat

SCOPE_FIELDS = frozenset(("experiment_id", "variant", "user_id", "session_id"))
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
BROWSER_TOOLS = frozenset((
    "browser_navigate", "browser_navigate_back", "browser_snapshot", "browser_click",
    "browser_fill_form", "browser_type", "browser_press_key", "browser_select_option",
    "browser_wait_for", "browser_tabs", "browser_handle_dialog",
))
REQUIRED_BROWSER_TOOLS = frozenset(("browser_navigate", "browser_snapshot", "browser_click", "browser_fill_form"))


def secret_forms(secrets):
    # Credentials can occur in JSON tool arguments inside an SSE JSON string,
    # or in a serialized RunState string inside an evidence JSON document.
    forms = {secret for secret in secrets if secret}
    frontier = forms.copy()
    for _ in range(3):
        frontier = {json.dumps(form, ensure_ascii=ascii_only)[1:-1]
                    for form in frontier for ascii_only in (False, True)} - forms
        forms.update(frontier)
    return sorted(forms, key=len, reverse=True)


class StreamingRedactor:
    """Redact complete secret bytes even when transport chunks split a secret.

    Only the stored evidence uses this projection; the live SDK receives the
    original chunks. Retain sufficient lookahead for the longest secret form.
    """
    def __init__(self, secrets):
        self.patterns = sorted({form.encode() for form in secret_forms(secrets)}, key=len, reverse=True)
        self.maximum = max(map(len, self.patterns), default=0)
        self.pending = b""

    def feed(self, chunk, *, final=False):
        if not self.patterns:
            return chunk
        self.pending += chunk
        limit = len(self.pending) if final else max(0, len(self.pending) - self.maximum + 1)
        position, output = 0, bytearray()
        while position < limit:
            match = next((pattern for pattern in self.patterns if self.pending.startswith(pattern, position)), None)
            if match:
                output.extend(b"[redacted-local-token]")
                position += len(match)
            else:
                output.append(self.pending[position])
                position += 1
        self.pending = self.pending[position:]
        return bytes(output)


def fields(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= value.keys() or value.keys() - set(required) - set(optional):
        raise ValueError("Invalid trusted detailed fields")


def validate_options(value):
    fields(value, {"case", "trusted_scope_file"}, {"browser", "business_extension"})
    if value["case"] not in {"tools", "browser", "sensitive", "extension"} or value["trusted_scope_file"] != "/config/scope.json":
        raise ValueError("Unexpected detailed case or scope mount")
    if value["case"] == "browser":
        if value.get("browser") != {"url": "http://browser:8931/mcp", "credentials_file": "/config/browser-credentials.json", "artifacts_dir": "/browser-artifacts"}:
            raise ValueError("Unexpected trusted browser endpoint or mounts")
    elif "browser" in value:
        raise ValueError("Browser configuration is only valid for the browser case")
    if value["case"] == "extension":
        extension = value.get("business_extension")
        fields(extension, {"quantity_limit"})
        if type(extension["quantity_limit"]) is not int or extension["quantity_limit"] != 3:
            raise ValueError("Expected the frozen business quantity limit of 3")
    elif "business_extension" in value:
        raise ValueError("Business extension is only valid for the extension case")
    return value


def existing_path(value):
    if not isinstance(value, str) or not value.startswith("/") or any(part == ".." or part.lower() == "old" for part in value.split("/")):
        raise ValueError("Expected an absolute non-archive path")
    path, current = Path(value), Path("/")
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            raise ValueError("Trusted paths must not follow symbolic links")
    if path.resolve(strict=True) != path:
        raise ValueError("Trusted path resolution changed")
    return path


def read_json(value):
    descriptor = os.open(existing_path(value), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
            raise ValueError("Expected a bounded trusted JSON file")
        return json.load(stream)


def read_scope(value, session_id):
    scope = read_json(value)
    fields(scope, SCOPE_FIELDS)
    if any(not isinstance(item, str) or not IDENTIFIER.fullmatch(item) or item.lower() == "old" for item in scope.values()) or scope["session_id"] != session_id:
        raise ValueError("Trusted scope/session identity mismatch")
    return scope


def read_credentials(value, user_id):
    credentials = read_json(value)
    fields(credentials, {"url", "username", "password"})
    password = credentials["password"]
    if credentials["url"] != "http://site:8081" or credentials["username"] != user_id or not isinstance(password, str) or not 0 < len(password) <= 1024 or any(ch in password for ch in "\r\n\0"):
        raise ValueError("Invalid trusted browser test credentials")
    return credentials


def denial_from_http(name, status, body):
    details = body.get("details", {}) if isinstance(body, dict) else {}
    return (isinstance(body, dict) and name == "sensitive_export" and status == 403 and body.get("fixture_status") == 403
            and isinstance(details, dict) and details.get("reason") == "sensitive_operation_denied"
            and details.get("approval_required") is False)


def native_download_paths(result):
    paths = []
    for block in result.get("content", []):
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
            paths.extend(re.findall(r'^- Downloaded file [^\r\n]+ to "([^"\r\n]+)"\s*$', block["text"], re.MULTILINE))
    return list(dict.fromkeys(paths))


def copy_download(native_path, artifact_root, workspace):
    root = existing_path(str(artifact_root))
    relative = native_path.removeprefix("/data/browser-artifacts/").removeprefix("./")
    if not relative or relative.startswith("/") or "\\" in relative or any(part in ("", ".", "..") or part.lower() == "old" for part in relative.split("/")):
        raise ValueError("MCP download escapes the per-run browser artifact directory")
    source = existing_path(str(root / relative))
    if not source.is_relative_to(root):
        raise ValueError("MCP download escapes the read-only artifact mount")
    if source.suffix.lower() != ".csv":
        raise ValueError("Only an actual downloaded CSV can become the task output")
    descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 2 * 1024 * 1024:
            raise ValueError("MCP download is not a bounded regular file")
        content = stream.read(2 * 1024 * 1024 + 1)
        if len(content) > 2 * 1024 * 1024:
            raise ValueError("MCP download grew beyond the limit")
    destination = existing_path(str(workspace)) / "result.csv"
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    return {"native_path": native_path, "source_file": str(source), "destination": str(destination),
            "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}


class Evidence:
    def __init__(self, directory, encode, scope):
        self.directory, self.encode, self.scope = Path(directory), encode, scope
        self.lines = {}

    def append(self, filename, value):
        blob = self.encode(value).encode()
        with (self.directory / filename).open("ab") as stream:
            stream.write(blob)
            stream.flush()
        number = self.lines.get(filename, 0) + 1
        self.lines[filename] = number
        return {"path": filename, "line": number, "line_sha256": hashlib.sha256(blob).hexdigest()}

    def save(self, filename, value):
        (self.directory / filename).write_text(self.encode(value))

    def finalize(self):
        self.save("evidence-index.json", {"scope": self.scope, "files": {
            name: {"lines": number, "sha256": hashlib.sha256((self.directory / name).read_bytes()).hexdigest()}
            for name, number in self.lines.items()}})

    @staticmethod
    def now():
        return datetime.now(timezone.utc).isoformat()
