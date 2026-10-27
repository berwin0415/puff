"""核验 SDK 导出、Linux 锁定文件和以非可编辑方式安装的源码包。"""

import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import re
import tomllib
from urllib.parse import unquote, urlparse


SOURCE_PINS = {
    "sdk": ("33d8aa29c3078d074799e2fbf5e847aeb2d71360", "70a8be4642612b7a6a1b43bc4dca5cc4be92fae58f98dfd10d0ff82b780b675c"),
    "canvas": ("8d39305538c1d17a30cbe477e57a0e18fcee7d95", "3296ef29e8b4690765d678917d748e54d3027660577100a570946fa9e8978028"),
}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def verify_inventory(root, entries):
    if not isinstance(entries, list) or not entries:
        raise ValueError("来源清单为空")
    seen = set()
    for entry in entries:
        relative = entry.get("path")
        if not isinstance(relative, str) or not relative or Path(relative).is_absolute() or any(part in ("", ".", "..") or part.lower() == "old" for part in relative.split("/")) or relative in seen:
            raise ValueError("来源记录路径不安全或重复")
        seen.add(relative)
        path = root / relative
        if not path.resolve().is_relative_to(root):
            raise ValueError("来源记录路径超出镜像材料目录")
        if entry.get("type") == "symlink":
            if not path.is_symlink() or str(path.readlink()) != entry.get("target"):
                raise ValueError("源码符号链接不匹配")
            content = str(path.readlink()).encode()
        elif entry.get("type") == "file":
            if not path.is_file() or path.is_symlink():
                raise ValueError("源码文件类型不匹配")
            content = path.read_bytes()
            if len(content) != entry.get("size"):
                raise ValueError("源码大小不匹配")
        else:
            raise ValueError("未知的来源记录文件类型")
        if digest(content) != entry.get("sha256"):
            raise ValueError(f"镜像材料哈希不匹配：{relative}")


def verify_lock(root):
    lab = root / "harness-lab"
    record = json.loads((lab / "LOCK_PROVENANCE.json").read_bytes())
    lock_bytes = (root / "sources/sdk/uv.lock").read_bytes()
    requirements = (lab / "requirements-linux-aarch64.txt").read_bytes()
    if record.get("schema") != "harness-lab-openhands-linux-lock/1" or record.get("target") != "CPython 3.12 Linux aarch64" or record.get("source_uv_lock_sha256") != digest(lock_bytes) or record.get("requirements_sha256") != digest(requirements):
        raise ValueError("Linux 依赖锁定文件的来源记录不匹配")
    if record.get("sdk_commit") != SOURCE_PINS["sdk"][0] or record.get("source_pyproject_sha256") != digest((root / "sources/sdk/pyproject.toml").read_bytes()) or record.get("sdk_pyproject_sha256") != digest((root / "sources/sdk/openhands-sdk/pyproject.toml").read_bytes()):
        raise ValueError("Linux 锁定文件与源码元数据不匹配")
    packages = tomllib.loads(lock_bytes.decode())["package"]
    known = {(entry["name"], entry["version"]): entry for entry in packages}
    versions = {}
    for block in requirements.decode().split("\n\n"):
        lines = block.strip().splitlines()
        if not lines:
            continue
        match = re.fullmatch(r"([a-z0-9][a-z0-9_.-]*)==([^\s;]+) \\", lines[0])
        if match is None:
            raise ValueError("Linux 依赖必须固定到精确版本且不带环境标记")
        if any(re.fullmatch(r"--hash=sha256:[a-f0-9]{64}", line.strip().removesuffix(" \\")) is None for line in lines[1:]):
            raise ValueError("Linux requirements 指令不符合预期")
        name, version = match.groups()
        entry = known.get((name, version))
        if entry is None or name in versions or not isinstance(entry.get("source", {}).get("registry"), str):
            raise ValueError("Linux 依赖不在上游冻结的注册表锁定清单中")
        allowed = {item["hash"] for item in entry.get("wheels", [])}
        actual = {"sha256:" + value for value in re.findall(r"--hash=sha256:([a-f0-9]{64})", block)}
        if not actual or not actual <= allowed:
            raise ValueError("Linux 产物哈希不在上游锁定清单中")
        versions[name] = version
    if versions != record.get("packages"):
        raise ValueError("Linux 软件包清单不匹配")
    return record


def verify_source(materials=None, *, source_only=False):
    root = (materials or Path(__file__).resolve().parents[1]).resolve()
    source_bytes = (root / "SOURCE_MANIFEST.json").read_bytes()
    source = json.loads(source_bytes)
    if source.get("schema") != "harness-lab-source-manifest/1" or source.get("candidate") != "openhands":
        raise ValueError("OpenHands 源码清单错误")
    records = source.get("sources", [])
    if len(records) != len(SOURCE_PINS) or {entry.get("id") for entry in records} != set(SOURCE_PINS):
        raise ValueError("OpenHands 源码文件清单错误")
    for entry in records:
        commit, archive_hash = SOURCE_PINS[entry["id"]]
        if entry.get("kind") != "archive" or entry.get("commit") != commit or entry.get("sha256") != archive_hash or entry.get("observed_archive_sha256") != archive_hash or entry.get("dirty") is not None:
            raise ValueError("OpenHands 源码归档尚未验证")
    files = source.get("files")
    if not isinstance(files, list) or digest(json.dumps(files, sort_keys=True).encode()) != source.get("export_sha256"):
        raise ValueError("源码清单摘要不匹配")
    used = [entry for entry in files if entry.get("path", "").startswith("sources/sdk/")]
    verify_inventory(root, used)
    lock = verify_lock(root)
    provenance = {
        "candidate": "openhands", "sdk_commit": SOURCE_PINS["sdk"][0],
        "source_export_sha256": source["export_sha256"], "verified_sdk_source_file_count": len(used),
        "used_source_ids": ["sdk"], "unused_source_ids": ["canvas"],
        "linux_dependency_count": len(lock["packages"]), "linux_requirements_sha256": lock["requirements_sha256"],
    }
    if source_only:
        return provenance
    adapter = json.loads((root / "LAB_ADAPTER_MANIFEST.json").read_bytes())
    if adapter.get("schema") != "harness-lab-adapter-manifest/1" or adapter.get("candidate") != "openhands" or adapter.get("source_manifest_sha256") != digest(source_bytes):
        raise ValueError("适配器与源码清单不匹配")
    verify_inventory(root, adapter.get("files"))
    if digest(json.dumps(adapter["files"], sort_keys=True).encode()) != adapter.get("export_sha256"):
        raise ValueError("适配器清单摘要不匹配")
    distribution = importlib.metadata.distribution("openhands-sdk")
    direct = json.loads(distribution.read_text("direct_url.json") or "{}")
    location = urlparse(direct.get("url", ""))
    if distribution.version != "1.56.0" or location.scheme != "file" or location.netloc or Path(unquote(location.path)).resolve() != (root / "sources/sdk/openhands-sdk").resolve() or direct.get("dir_info", {}).get("editable") is True:
        raise ValueError("已安装 SDK 源码发行包的来源记录不匹配")
    for name, version in lock["packages"].items():
        if importlib.metadata.version(name) != version:
            raise ValueError(f"已安装 Linux 依赖版本不匹配：{name}")
    for name in ("openhands-tools", "openhands-workspace", "openhands-agent-server"):
        try:
            importlib.metadata.distribution(name)
        except importlib.metadata.PackageNotFoundError:
            continue
        raise ValueError(f"安装了非预期的 OpenHands 可选产品：{name}")
    return {
        **provenance, "adapter_export_sha256": adapter["export_sha256"],
        "installed_source_distributions": {"openhands-sdk": {"version": distribution.version, "editable": False}},
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-only", action="store_true")
    args = parser.parse_args()
    print(json.dumps(verify_source(source_only=args.source_only), sort_keys=True))
