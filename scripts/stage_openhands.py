#!/usr/bin/env python3
"""仅在已核验的 SDK 导出旁准备实验文件。"""

import argparse
import hashlib
import json
from pathlib import Path
import shutil


FILES = (
    "entrypoint.py", "http_tools.py", "verify_source.py", "requirements-linux-aarch64.txt",
    "bootstrap-requirements.txt", "LOCK_PROVENANCE.json", "README.md",
    # 详细用例接入：可信输入校验、浏览器 MCP 桥与业务扩展。
    "detailed_support.py", "browser_tools.py", "browser_specs.py", "business_extension.py",
)
PINS = {
    "sdk": ("33d8aa29c3078d074799e2fbf5e847aeb2d71360", "70a8be4642612b7a6a1b43bc4dca5cc4be92fae58f98dfd10d0ff82b780b675c"),
    "canvas": ("8d39305538c1d17a30cbe477e57a0e18fcee7d95", "3296ef29e8b4690765d678917d748e54d3027660577100a570946fa9e8978028"),
}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def stage(context, repository):
    context = context.resolve()
    if any(part.lower() == "old" for part in context.parts):
        raise ValueError('不能向归档材料目录准备文件')
    source_bytes = (context / "SOURCE_MANIFEST.json").read_bytes()
    source = json.loads(source_bytes)
    records = source.get("sources", [])
    if source.get("schema") != "harness-lab-source-manifest/1" or source.get("candidate") != "openhands" or len(records) != len(PINS) or {entry.get("id") for entry in records} != set(PINS):
        raise ValueError('需要当前已核验的 OpenHands 导出')
    for entry in records:
        commit, archive_hash = PINS[entry["id"]]
        if entry.get("kind") != "archive" or entry.get("commit") != commit or entry.get("sha256") != archive_hash or entry.get("observed_archive_sha256") != archive_hash or entry.get("dirty") is not None:
            raise ValueError('源码归档不匹配或未经核验')
    destination = context / "harness-lab"
    if destination.exists():
        if destination.is_symlink() or not (context / "LAB_ADAPTER_MANIFEST.json").is_file():
            raise ValueError('拒绝替换未经准备登记的适配材料')
        shutil.rmtree(destination)
    destination.mkdir()
    inventory = []
    for name in FILES:
        data = (repository / "adapters/openhands" / name).read_bytes()
        (destination / name).write_bytes(data)
        inventory.append({"path": "harness-lab/" + name, "type": "file", "size": len(data), "sha256": digest(data)})
    manifest = {
        "schema": "harness-lab-adapter-manifest/1", "candidate": "openhands",
        "source_manifest_sha256": digest(source_bytes),
        "export_sha256": digest(json.dumps(inventory, sort_keys=True).encode()), "files": inventory,
    }
    (context / "LAB_ADAPTER_MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n")
    dockerfile = repository / "compose/candidates/openhands/Dockerfile"
    shutil.copyfile(dockerfile, context / "Dockerfile")
    (context / ".dockerignore").write_text(".git\nold\nsources/canvas\n**/node_modules\n.env\n.env.*\n**/.venv\n**/__pycache__\n")
    return {"candidate": "openhands", "context": str(context), "adapter_export_sha256": manifest["export_sha256"], "dockerfile_sha256": digest(dockerfile.read_bytes())}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", type=Path, default=Path("external/build-context/openhands"))
    args = parser.parse_args()
    print(json.dumps(stage(args.context, Path(__file__).resolve().parents[1])))
