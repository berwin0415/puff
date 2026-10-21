"""Trusted HTTP definition material for offline reconstruction, not run state."""

import hashlib
import json
import os
import stat

from detailed_support import existing_path


EXPECTED_NAMES = frozenset({"list_items", "write", "receipt", "ledger", "read_file", "write_file", "calculate_totals", "sensitive_export"})
RESPONSE_FILE = "http-tool-definitions.json"
SOURCE_FILE = "http-tool-definitions-source.json"
SOURCE_SCHEMA = "harness-lab-authenticated-tool-definitions/1"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def validate_definitions(payload):
    definitions = payload.get("tools") if isinstance(payload, dict) and set(payload) == {"tools"} else None
    if (not isinstance(definitions, list) or len(definitions) != len(EXPECTED_NAMES)
            or any(not isinstance(entry, dict) or set(entry) != {"name", "description", "input_schema"} for entry in definitions)
            or any(not isinstance(entry["name"], str) for entry in definitions)
            or {entry["name"] for entry in definitions} != EXPECTED_NAMES):
        raise ValueError("Expected the eight original trusted HTTP tool definitions")
    for entry in definitions:
        schema = entry["input_schema"]
        if (not isinstance(entry["description"], str) or not isinstance(schema, dict)
                or schema.get("type") != "object" or schema.get("additionalProperties") is not False
                or not isinstance(schema.get("properties"), dict) or not isinstance(schema.get("required"), list)):
            raise ValueError("Invalid original tool schema")
    return definitions


def read_bounded(filename, maximum=1024 * 1024):
    descriptor = os.open(existing_path(str(filename)), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
            raise ValueError("Expected a bounded regular trusted definition file")
        data = stream.read(maximum + 1)
        if len(data) > maximum:
            raise ValueError("Trusted definition file grew beyond its limit")
        return data


def source_record(identity, provenance, response_sha256):
    return {"schema": SOURCE_SCHEMA, "run_id": identity["run_id"], "session_id": identity["session_id"],
        "scope": identity["scope"], "endpoint": "http://tools:8081/tools", "http_status": 200,
        "authentication": "successful Bearer-authenticated discovery; credential omitted",
        "response_file": RESPONSE_FILE, "response_sha256": response_sha256,
        "source_export_sha256": provenance["source_export_sha256"],
        "adapter_export_sha256": provenance["adapter_export_sha256"]}


def cache_binding(raw, source):
    return {"response_sha256": digest(raw), "source_sha256": digest(source)}


def save_authenticated_definitions(state_dir, raw, identity, provenance):
    """Called only after the real authenticated GET /tools returned HTTP 200."""
    validate_definitions(json.loads(raw))
    root = existing_path(str(state_dir))
    source = (json.dumps(source_record(identity, provenance, digest(raw)), ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    for name, data in ((RESPONSE_FILE, raw), (SOURCE_FILE, source)):
        descriptor = os.open(root / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    return cache_binding(raw, source)


def load_authenticated_definitions(state_dir, identity, provenance, expected_binding):
    """Read same-scope material; no network or mutation of the state mount."""
    root = existing_path(str(state_dir))
    raw, source = read_bounded(root / RESPONSE_FILE), read_bounded(root / SOURCE_FILE)
    if cache_binding(raw, source) != expected_binding:
        raise ValueError("Tool definition cache differs from the native RunState binding")
    if json.loads(source) != source_record(identity, provenance, digest(raw)):
        raise ValueError("Tool definition source scope, provenance or HTTP origin differs")
    return validate_definitions(json.loads(raw)), {"response_path": str(root / RESPONSE_FILE),
        "source_path": str(root / SOURCE_FILE), **cache_binding(raw, source)}
