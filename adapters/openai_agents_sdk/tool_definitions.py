"""用于离线重建的可信 HTTP 定义材料，不是运行状态。"""

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
        raise ValueError("必须提供原始的八个可信 HTTP 工具定义")
    for entry in definitions:
        schema = entry["input_schema"]
        if (not isinstance(entry["description"], str) or not isinstance(schema, dict)
                or schema.get("type") != "object" or schema.get("additionalProperties") is not False
                or not isinstance(schema.get("properties"), dict) or not isinstance(schema.get("required"), list)):
            raise ValueError("原始工具结构无效")
    return definitions


def read_bounded(filename, maximum=1024 * 1024):
    descriptor = os.open(existing_path(str(filename)), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
            raise ValueError("必须提供大小受限的可信定义普通文件")
        data = stream.read(maximum + 1)
        if len(data) > maximum:
            raise ValueError("可信定义文件增长后超过大小上限")
        return data


def source_record(identity, provenance, response_sha256):
    return {"schema": SOURCE_SCHEMA, "run_id": identity["run_id"], "session_id": identity["session_id"],
        "scope": identity["scope"], "endpoint": "http://tools:8081/tools", "http_status": 200,
        "authentication": "Bearer 认证的工具发现成功；不保存凭证",
        "response_file": RESPONSE_FILE, "response_sha256": response_sha256,
        "source_export_sha256": provenance["source_export_sha256"],
        "adapter_export_sha256": provenance["adapter_export_sha256"]}


def cache_binding(raw, source):
    return {"response_sha256": digest(raw), "source_sha256": digest(source)}


def save_authenticated_definitions(state_dir, raw, identity, provenance):
    """仅在真实且已认证的 GET /tools 返回 HTTP 200 后调用。"""
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
    """读取同一作用域的材料，不访问网络或修改状态挂载。"""
    root = existing_path(str(state_dir))
    raw, source = read_bounded(root / RESPONSE_FILE), read_bounded(root / SOURCE_FILE)
    if cache_binding(raw, source) != expected_binding:
        raise ValueError("工具定义缓存与原生 RunState 绑定不一致")
    if json.loads(source) != source_record(identity, provenance, digest(raw)):
        raise ValueError("工具定义的来源作用域、来源记录或 HTTP 源站不一致")
    return validate_definitions(json.loads(raw)), {"response_path": str(root / RESPONSE_FILE),
        "source_path": str(root / SOURCE_FILE), **cache_binding(raw, source)}
