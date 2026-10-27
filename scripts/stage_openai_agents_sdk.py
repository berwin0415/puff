#!/usr/bin/env python3
"""准备固定版本的 OpenAI Agents SDK 镜像材料，不导入 SDK。"""

import argparse
import hashlib
import json
from pathlib import Path
import shutil


FILES = ("pyproject.toml", "uv.lock", "bootstrap-requirements.txt", "entrypoint.py", "http_tools.py", "browser_tools.py", "business_extension.py", "tool_definitions.py", "cancel_control.py", "detailed_support.py", "model_transport.py", "verify_source.py", "README.md")
COMMIT = "64cd5dfc5a1579870dcbed0ea42b27aa84033fee"
ARCHIVE_HASH = "8bfb125e5d9343e2740a502aec764afaa63cf55dc17d5df068b7d3680a213321"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def stage(context: Path, repository: Path, *, prepare_lock=False) -> dict:
    context = context.resolve()
    if "old" in context.parts:
        raise ValueError('不能向归档材料目录准备文件')
    source_bytes = (context / "SOURCE_MANIFEST.json").read_bytes()
    source = json.loads(source_bytes)
    if source.get("schema") != "harness-lab-source-manifest/1" or source.get("candidate") != "openai_agents_sdk":
        raise ValueError('需要当前 OpenAI Agents SDK 上下文')
    sources = source.get("sources", [])
    if len(sources) != 1:
        raise ValueError('源码清单不匹配')
    record = sources[0]
    if record.get("id") != "main" or record.get("kind") != "archive" or record.get("commit") != COMMIT or record.get("sha256") != ARCHIVE_HASH or record.get("observed_archive_sha256") != ARCHIVE_HASH or record.get("dirty") is not None:
        raise ValueError('OpenAI Agents SDK 源码归档未经核验')
    destination = context / "harness-lab"
    marker = context / "LAB_LOCK_PREPARATION.json"
    if destination.exists():
        if destination.is_symlink() or not ((context / "LAB_ADAPTER_MANIFEST.json").is_file() or marker.is_file()):
            raise ValueError('拒绝替换未经准备登记的适配材料')
        shutil.rmtree(destination)
    destination.mkdir()
    inventory = []
    for name in (("pyproject.toml",) if prepare_lock else FILES):
        data = (repository / "adapters" / "openai_agents_sdk" / name).read_bytes()
        (destination / name).write_bytes(data)
        inventory.append({"path": "harness-lab/" + name, "type": "file", "size": len(data), "sha256": digest(data)})
    if prepare_lock:
        marker.write_text(json.dumps({"candidate": "openai_agents_sdk", "purpose": "lock_material_preparation_only", "source_manifest_sha256": digest(source_bytes)}, indent=2) + "\n")
        (context / "Dockerfile").unlink(missing_ok=True)
        (context / "LAB_ADAPTER_MANIFEST.json").unlink(missing_ok=True)
        return {"candidate": "openai_agents_sdk", "context": str(context), "status": "lock_material_only_not_buildable"}
    manifest = {
        "schema": "harness-lab-adapter-manifest/1", "candidate": "openai_agents_sdk",
        "source_manifest_sha256": digest(source_bytes),
        "export_sha256": digest(json.dumps(inventory, sort_keys=True).encode()), "files": inventory,
    }
    (context / "LAB_ADAPTER_MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n")
    dockerfile = repository / "compose" / "candidates" / "openai_agents_sdk" / "Dockerfile"
    shutil.copyfile(dockerfile, context / "Dockerfile")
    (context / ".dockerignore").write_text(".git\nold\n**/node_modules\n.env\n.env.*\n**/.venv\nLAB_LOCK_PREPARATION.json\n", encoding="utf-8")
    marker.unlink(missing_ok=True)
    return {"candidate": "openai_agents_sdk", "context": str(context), "adapter_export_sha256": manifest["export_sha256"], "dockerfile_sha256": digest(dockerfile.read_bytes())}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", type=Path, default=Path("external/build-context/openai_agents_sdk"))
    parser.add_argument("--prepare-lock", action="store_true")
    args = parser.parse_args()
    print(json.dumps(stage(args.context, Path(__file__).resolve().parents[1], prepare_lock=args.prepare_lock)))
