#!/usr/bin/env python3
"""在已核验源码旁准备当前实验的轻量 Pi 入口。"""

import argparse
import hashlib
import json
from pathlib import Path
import shutil


FILES = ("entrypoint.mjs", "build-bundle.mjs", "verify-source.mjs", "README.md")
COMMIT = "73f8de80bc87021375f8b525292ac07492253002"
ARCHIVE_HASH = "ce969e2917f93ed49d8be62fc7e7a0a02caa02959f40c28c2fb87f2a62639fd8"
EXPORT_HASH = "493304f15f7b2f15789a00353eb16215dd27346948a99e51372c4369c3dbda59"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def stage(context: Path, repository: Path) -> dict:
    context = context.resolve()
    if "old" in context.parts:
        raise ValueError('不能向归档材料目录准备文件')
    source_bytes = (context / "SOURCE_MANIFEST.json").read_bytes()
    source = json.loads(source_bytes)
    if source.get("schema") != "harness-lab-source-manifest/1" or source.get("candidate") != "pi_agent_core" or source.get("export_sha256") != EXPORT_HASH:
        raise ValueError('需要当前 Pi 导出上下文')
    sources = source.get("sources", [])
    if len(sources) != 1:
        raise ValueError('源码清单不匹配')
    pin = sources[0]
    if pin.get("id") != "main" or pin.get("kind") != "archive" or pin.get("commit") != COMMIT or pin.get("sha256") != ARCHIVE_HASH or pin.get("observed_archive_sha256") != ARCHIVE_HASH or pin.get("dirty") is not None:
        raise ValueError('Pi 源码归档不匹配或未经核验')
    destination = context / "harness-lab"
    if destination.exists():
        if destination.is_symlink() or not (context / "LAB_ADAPTER_MANIFEST.json").is_file():
            raise ValueError('拒绝替换未经准备登记的适配材料')
        shutil.rmtree(destination)
    destination.mkdir()
    inventory = []
    for name in FILES:
        data = (repository / "adapters" / "pi_agent_core" / name).read_bytes()
        (destination / name).write_bytes(data)
        inventory.append({"path": "harness-lab/" + name, "type": "file", "size": len(data), "sha256": digest(data)})
    manifest = {
        "schema": "harness-lab-adapter-manifest/1", "candidate": "pi_agent_core",
        "source_manifest_sha256": digest(source_bytes),
        "export_sha256": digest(json.dumps(inventory, sort_keys=True).encode()), "files": inventory,
    }
    (context / "LAB_ADAPTER_MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n")
    dockerfile = repository / "compose" / "candidates" / "pi_agent_core" / "Dockerfile"
    shutil.copyfile(dockerfile, context / "Dockerfile")
    (context / ".dockerignore").write_text(".git\nold\n**/node_modules\n.env\n.env.*\n**/.venv\n", encoding="utf-8")
    return {"candidate": "pi_agent_core", "context": str(context), "adapter_export_sha256": manifest["export_sha256"], "dockerfile_sha256": digest(dockerfile.read_bytes())}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", type=Path, default=Path("external/build-context/pi_agent_core"))
    args = parser.parse_args()
    print(json.dumps(stage(args.context, Path(__file__).resolve().parents[1])))
