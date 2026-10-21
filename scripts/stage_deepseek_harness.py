#!/usr/bin/env python3
"""Add experiment-owned entry files to an already verified DSH build context.

Run prepare_candidate_source.py first. This script changes neither the external
repository nor the exported sources/main tree. No candidate code is executed.
"""

import argparse
import hashlib
import json
from pathlib import Path
import shutil


FILES = ("entrypoint.ts", "tools-plugin.mjs", "cordis.patch.yml", "verify-source.mjs",
         "detailed-options.mjs", "scoped-runtime-entrypoint.ts", "scoped-runtime.cordis.patch.yml",
         "business-extension.mjs")
GOAL_FILES = ("goal-runtime-entrypoint.ts", "goal-runtime.ts", "goal-binding.mjs", "goal-runtime.cordis.patch.yml", "goal-surface-entrypoint.ts")
EXPECTED_COMMIT = "dd6322d604e00eec1ba5e0c8541159906a21094a"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def stage(context: Path, repository: Path, *, goal_runtime: bool = False) -> dict:
    context = context.resolve()
    if "old" in context.parts:
        raise ValueError("Cannot stage into archived materials")
    if goal_runtime and context == (repository / "external/build-context/deepseek_harness").resolve():
        raise ValueError("I22 must use a separate context; the existing detailed build material is frozen")
    source_bytes = (context / "SOURCE_MANIFEST.json").read_bytes()
    source = json.loads(source_bytes)
    if source.get("schema") != "harness-lab-source-manifest/1" or source.get("candidate") != "deepseek_harness":
        raise ValueError("Expected the current DeepSeek Harness exported context")
    sources = source.get("sources", [])
    if len(sources) != 1 or sources[0].get("commit") != EXPECTED_COMMIT or sources[0].get("observed_commit") != EXPECTED_COMMIT or sources[0].get("dirty") is not False:
        raise ValueError("Wrong or unverified DSH source revision")
    destination = context / "harness-lab"
    if destination.exists():
        if destination.is_symlink() or not (context / "LAB_ADAPTER_MANIFEST.json").is_file():
            raise ValueError("Refusing to replace unstaged adapter material")
        shutil.rmtree(destination)
    destination.mkdir()
    inventory = []
    for name in FILES + (GOAL_FILES if goal_runtime else ()):
        origin = repository / "adapters" / "deepseek_harness" / name
        target = destination / name
        data = origin.read_bytes()
        target.write_bytes(data)
        inventory.append({"path": "harness-lab/" + name, "type": "file", "size": len(data), "sha256": digest(data)})
    manifest = {
        "schema": "harness-lab-adapter-manifest/1",
        "candidate": "deepseek_harness",
        "source_manifest_sha256": digest(source_bytes),
        "export_sha256": digest(json.dumps(inventory, sort_keys=True).encode()),
        "files": inventory,
    }
    (context / "LAB_ADAPTER_MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    dockerfile = repository / "compose" / "candidates" / "deepseek_harness" / "Dockerfile"
    shutil.copyfile(dockerfile, context / "Dockerfile")
    (context / ".dockerignore").write_text(".git\nold\n**/node_modules\n.env\n.env.*\n**/.venv\n", encoding="utf-8")
    return {"candidate": "deepseek_harness", "context": str(context), "adapter_export_sha256": manifest["export_sha256"], "dockerfile_sha256": digest(dockerfile.read_bytes())}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", type=Path, default=Path("external/build-context/deepseek_harness"))
    parser.add_argument("--goal-runtime", action="store_true", help="Include the separately frozen I22 Goal entry; do not overwrite an existing detailed context")
    arguments = parser.parse_args()
    print(json.dumps(stage(arguments.context, Path(__file__).resolve().parents[1], goal_runtime=arguments.goal_runtime)))


if __name__ == "__main__":
    main()
