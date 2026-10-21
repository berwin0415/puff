"""Stdlib-only trusted configuration, evidence and native terminal checks."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat


IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
SCOPE_FIELDS = frozenset(("experiment_id", "variant", "user_id", "session_id"))
BROWSER_TOOLS = frozenset((
    "browser_navigate", "browser_navigate_back", "browser_snapshot", "browser_click",
    "browser_fill_form", "browser_type", "browser_press_key", "browser_select_option",
    "browser_wait_for", "browser_tabs", "browser_handle_dialog",
))
REQUIRED_BROWSER_TOOLS = frozenset(("browser_navigate", "browser_snapshot", "browser_click", "browser_fill_form"))


def fields(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= set(value) or set(value) - set(required) - set(optional):
        raise ValueError("Invalid detailed configuration fields")


def validate_options(value):
    fields(value, {"case", "trusted_scope_file"}, {"browser", "business_extension"})
    if value["case"] not in {"tools", "browser", "sensitive", "extension"} or value["trusted_scope_file"] != "/config/scope.json":
        raise ValueError("Unexpected detailed case or trusted scope path")
    if value["case"] == "browser":
        fields(value.get("browser"), {"url", "credentials_file", "artifacts_dir"})
        if value["browser"] != {"url": "http://browser:8931/mcp", "credentials_file": "/config/browser-credentials.json", "artifacts_dir": "/browser-artifacts"}:
            raise ValueError("Unexpected browser endpoint or trusted mount")
    elif "browser" in value:
        raise ValueError("Browser options belong only to browser case")
    if value["case"] == "extension":
        fields(value.get("business_extension"), {"quantity_limit"})
        if type(value["business_extension"]["quantity_limit"]) is not int or value["business_extension"]["quantity_limit"] != 3:
            raise ValueError("Expected the frozen business-extension rule")
    elif "business_extension" in value:
        raise ValueError("Business extension belongs only to extension case")
    return value


def existing_path(value) -> Path:
    if not isinstance(value, str) or not value.startswith("/") or any(part == ".." or part.lower() == "old" for part in value.split("/")):
        raise ValueError("Expected an absolute non-archive path")
    path = Path(value)
    current = Path("/")
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            raise ValueError("Trusted path must not follow symbolic links")
    if path.resolve(strict=True) != path:
        raise ValueError("Trusted path changed during resolution")
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
    if credentials["url"] != "http://site:8081" or credentials["username"] != user_id or not isinstance(password, str) or not 0 < len(password) <= 1024 or any(character in password for character in "\r\n\0"):
        raise ValueError("Invalid trusted browser test credentials")
    return credentials


def completion(*, last_is_ai, tool_calls, invalid_tool_calls, metadata, pending, task_errors=(), interrupts=()):
    """A closed graph is insufficient: require the native model's normal stop."""
    metadata = metadata if isinstance(metadata, dict) else {}
    finish = metadata.get("finish_reason")
    result = {"native_status": "incomplete", "finish_reason": finish, "response_metadata": metadata}
    if task_errors or metadata.get("error"):
        return {**result, "native_status": "failed", "stop_reason": "native_task_error"}
    if pending or interrupts or not last_is_ai or tool_calls or invalid_tool_calls:
        return {**result, "stop_reason": "pending_graph_state"}
    if finish == "stop":
        return {**result, "native_status": "completed", "stop_reason": "graph_end"}
    if finish == "length":
        return {**result, "stop_reason": "length"}
    if finish in {"error", "failed", "timeout", "cancelled", "canceled", "aborted"}:
        return {**result, "native_status": "failed", "stop_reason": str(finish)}
    return {**result, "stop_reason": "missing_finish_reason" if finish is None else "non_success_finish_reason"}


def denial_from_http(name, status, body):
    """Only a real execution response can prove the simulated policy denial."""
    details = body.get("details", {}) if isinstance(body, dict) else {}
    return (isinstance(body, dict) and name == "sensitive_export" and status == 403 and body.get("fixture_status") == 403
            and isinstance(details, dict) and details.get("reason") == "sensitive_operation_denied"
            and details.get("approval_required") is False)


def native_download_paths(result):
    paths = []
    for block in result.get("content", []):
        if not isinstance(block, dict) or block.get("type") != "text" or not isinstance(block.get("text"), str):
            continue
        # Restrict matching to the MCP renderer's Events section, excluding
        # page snapshots and quoted page text that happens to resemble a log.
        sections = re.findall(r'^### Events\s*\n(.*?)(?=^### |\Z)', block["text"], re.MULTILINE | re.DOTALL)
        for section in sections:
            paths.extend(re.findall(r'^- Downloaded file [^\r\n]+ to "([^"\r\n]+)"\s*$', section, re.MULTILINE))
    return list(dict.fromkeys(paths))


def copy_download(native_path, artifact_root, workspace):
    root = existing_path(str(artifact_root))
    relative = native_path.removeprefix("/data/browser-artifacts/").removeprefix("./")
    if not relative or relative.startswith("/") or "\\" in relative or any(part in ("", ".", "..") or part.lower() == "old" for part in relative.split("/")):
        raise ValueError("MCP download path escapes the browser output directory")
    source = existing_path(str(root / relative))
    if not source.is_relative_to(root):
        raise ValueError("MCP download escapes its read-only artifact mount")
    descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 2 * 1024 * 1024:
            raise ValueError("MCP download is not a bounded regular file")
        content = stream.read(2 * 1024 * 1024 + 1)
        if len(content) > 2 * 1024 * 1024:
            raise ValueError("MCP download grew beyond the file limit")
    destination = existing_path(str(workspace)) / "result.csv"
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    return {"native_path": native_path, "source_file": str(source), "destination": str(destination), "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}


class Evidence:
    def __init__(self, directory, encode, scope=None):
        self.directory = Path(directory)
        self.encode = encode
        self.scope = scope
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
            for name, number in self.lines.items()
        }})

    @staticmethod
    def now():
        return datetime.now(timezone.utc).isoformat()
