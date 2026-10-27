#!/usr/bin/env python3
"""在已核验的 DeepAgents/LangGraph 源码旁准备实验文件。

宿主仅准备材料；本脚本不导入候选包。--prepare-lock 只准备 pyproject.toml，供 uv 解析新的独立 Linux 锁文件，不运行候选 SDK。将生成的锁文件复制到 adapters/deepagents_langgraph/uv.lock 后，再正常运行本脚本。
"""

import argparse
import hashlib
import json
from pathlib import Path
import shutil


FILES = ("pyproject.toml", "uv.lock", "bootstrap-requirements.txt", "entrypoint.py", "http_tools.py", "verify_source.py", "README.md",
         "detailed_support.py", "browser_tools.py", "business_extension.py", "run_control.py", "pause_recovery.py", "LOCK_I19_CHANGES.json")
COMMITS = {"deepagents": "0089e635dce525055272883b53bc222f385beac1", "langgraph": "98043c37abc60374030f960a03a6e0d4c0112643"}
ARCHIVE_HASH = "2b0cb8cd05f7faeb8e8745845958056a9e974658fc85f4682643874c99d8a548"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def stage(context: Path, repository: Path, *, prepare_lock=False) -> dict:
    context = context.resolve()
    if "old" in context.parts:
        raise ValueError('不能向归档材料目录准备文件')
    source_bytes = (context / "SOURCE_MANIFEST.json").read_bytes()
    source = json.loads(source_bytes)
    if source.get("schema") != "harness-lab-source-manifest/1" or source.get("candidate") != "deepagents_langgraph":
        raise ValueError('需要当前 DeepAgents/LangGraph 上下文')
    records = {entry["id"]: entry for entry in source.get("sources", [])}
    if set(records) != set(COMMITS) or len(source["sources"]) != len(COMMITS):
        raise ValueError('源码清单不匹配')
    for name, commit in COMMITS.items():
        record = records[name]
        if record.get("commit") != commit:
            raise ValueError('源码版本不匹配')
        if name == "deepagents":
            if record.get("kind") != "archive" or record.get("sha256") != ARCHIVE_HASH or record.get("observed_archive_sha256") != ARCHIVE_HASH or record.get("dirty") is not None:
                raise ValueError('DeepAgents 源码归档未经核验')
        elif record.get("kind") != "git" or record.get("observed_commit") != commit or record.get("dirty") is not False:
            raise ValueError('LangGraph 源码检出未经核验')
    destination = context / "harness-lab"
    marker = context / "LAB_LOCK_PREPARATION.json"
    if destination.exists():
        if destination.is_symlink() or not ((context / "LAB_ADAPTER_MANIFEST.json").is_file() or marker.is_file()):
            raise ValueError('拒绝替换未经准备登记的适配材料')
        shutil.rmtree(destination)
    destination.mkdir()
    inventory = []
    for name in (("pyproject.toml",) if prepare_lock else FILES):
        data = (repository / "adapters" / "deepagents_langgraph" / name).read_bytes()
        (destination / name).write_bytes(data)
        inventory.append({"path": "harness-lab/" + name, "type": "file", "size": len(data), "sha256": digest(data)})
    if prepare_lock:
        marker.write_text(json.dumps({"candidate": "deepagents_langgraph", "purpose": "lock_material_preparation_only", "source_manifest_sha256": digest(source_bytes)}, indent=2) + "\n")
        # 不完整上下文不能保留可用的旧 Docker 构建材料。
        (context / "Dockerfile").unlink(missing_ok=True)
        (context / "LAB_ADAPTER_MANIFEST.json").unlink(missing_ok=True)
        return {"candidate": "deepagents_langgraph", "context": str(context), "status": "lock_material_only_not_buildable"}
    manifest = {
        "schema": "harness-lab-adapter-manifest/1", "candidate": "deepagents_langgraph",
        "source_manifest_sha256": digest(source_bytes),
        "export_sha256": digest(json.dumps(inventory, sort_keys=True).encode()), "files": inventory,
    }
    (context / "LAB_ADAPTER_MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n")
    dockerfile = repository / "compose" / "candidates" / "deepagents_langgraph" / "Dockerfile"
    shutil.copyfile(dockerfile, context / "Dockerfile")
    (context / ".dockerignore").write_text(".git\nold\n**/node_modules\n.env\n.env.*\n**/.venv\nLAB_LOCK_PREPARATION.json\n", encoding="utf-8")
    marker.unlink(missing_ok=True)
    return {"candidate": "deepagents_langgraph", "context": str(context), "adapter_export_sha256": manifest["export_sha256"], "dockerfile_sha256": digest(dockerfile.read_bytes())}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", type=Path, default=Path("external/build-context/deepagents_langgraph"))
    parser.add_argument("--prepare-lock", action="store_true")
    args = parser.parse_args()
    print(json.dumps(stage(args.context, Path(__file__).resolve().parents[1], prepare_lock=args.prepare_lock)))
