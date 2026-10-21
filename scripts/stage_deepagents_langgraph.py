#!/usr/bin/env python3
"""Stage experiment-owned files beside verified DeepAgents/LangGraph sources.

The host only prepares materials; this script never imports candidate packages.
--prepare-lock stages pyproject.toml only so uv can resolve a new independent
Linux lock without running candidate SDK code. Run again normally after copying
the resulting lock to adapters/deepagents_langgraph/uv.lock.
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
        raise ValueError("Cannot stage into archived materials")
    source_bytes = (context / "SOURCE_MANIFEST.json").read_bytes()
    source = json.loads(source_bytes)
    if source.get("schema") != "harness-lab-source-manifest/1" or source.get("candidate") != "deepagents_langgraph":
        raise ValueError("Expected the current DeepAgents/LangGraph context")
    records = {entry["id"]: entry for entry in source.get("sources", [])}
    if set(records) != set(COMMITS) or len(source["sources"]) != len(COMMITS):
        raise ValueError("Wrong source inventory")
    for name, commit in COMMITS.items():
        record = records[name]
        if record.get("commit") != commit:
            raise ValueError("Wrong source revision")
        if name == "deepagents":
            if record.get("kind") != "archive" or record.get("sha256") != ARCHIVE_HASH or record.get("observed_archive_sha256") != ARCHIVE_HASH or record.get("dirty") is not None:
                raise ValueError("Unverified DeepAgents source archive")
        elif record.get("kind") != "git" or record.get("observed_commit") != commit or record.get("dirty") is not False:
            raise ValueError("Unverified LangGraph source checkout")
    destination = context / "harness-lab"
    marker = context / "LAB_LOCK_PREPARATION.json"
    if destination.exists():
        if destination.is_symlink() or not ((context / "LAB_ADAPTER_MANIFEST.json").is_file() or marker.is_file()):
            raise ValueError("Refusing to replace unstaged adapter material")
        shutil.rmtree(destination)
    destination.mkdir()
    inventory = []
    for name in (("pyproject.toml",) if prepare_lock else FILES):
        data = (repository / "adapters" / "deepagents_langgraph" / name).read_bytes()
        (destination / name).write_bytes(data)
        inventory.append({"path": "harness-lab/" + name, "type": "file", "size": len(data), "sha256": digest(data)})
    if prepare_lock:
        marker.write_text(json.dumps({"candidate": "deepagents_langgraph", "purpose": "lock_material_preparation_only", "source_manifest_sha256": digest(source_bytes)}, indent=2) + "\n")
        # An incomplete context must not retain a stale usable Docker build.
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
