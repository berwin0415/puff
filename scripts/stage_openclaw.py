#!/usr/bin/env python3
"""Stage the lab launcher/plugin beside a verified upstream OpenClaw export.

Does not install dependencies or execute candidate code. Run source export first.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil

EXPECTED_COMMIT = "b6e0872bdb7228e22fac7608345e0529a35ec46e"
FILES = ("entrypoint.mjs", "native-control.mjs", "verify-source.mjs", "build.npmrc", "README.md", "tools-plugin/package.json", "tools-plugin/openclaw.plugin.json", "tools-plugin/index.mjs", "tools-plugin/browser-client.mjs", "tools-plugin/detailed-options.mjs", "tools-plugin/business-state.mjs", "BUSINESS_EXTENSION.patch")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def stage(context, repository):
    context = context.resolve()
    if "old" in context.parts:
        raise ValueError("Cannot stage into archived materials")
    source_bytes = (context / "SOURCE_MANIFEST.json").read_bytes()
    source = json.loads(source_bytes)
    sources = source.get("sources", [])
    if source.get("schema") != "harness-lab-source-manifest/1" or source.get("candidate") != "openclaw":
        raise ValueError("Expected the current OpenClaw source export")
    if len(sources) != 1 or sources[0].get("commit") != EXPECTED_COMMIT or sources[0].get("observed_commit") != EXPECTED_COMMIT or sources[0].get("dirty") is not False:
        raise ValueError("Wrong or unverified OpenClaw source revision")
    destination = context / "harness-lab"
    if destination.exists():
        if destination.is_symlink() or not (context / "LAB_ADAPTER_MANIFEST.json").is_file():
            raise ValueError("Refusing to replace unstaged adapter material")
        shutil.rmtree(destination)
    destination.mkdir()
    inventory = []
    for relative in FILES:
        origin = repository / "adapters" / "openclaw" / relative
        if origin.is_symlink() or not origin.is_file():
            raise ValueError("Expected a regular reviewed adapter file")
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        data = origin.read_bytes()
        target.write_bytes(data)
        inventory.append({"path": "harness-lab/" + relative, "type": "file", "size": len(data), "sha256": digest(data)})
    manifest = {"schema": "harness-lab-adapter-manifest/1", "candidate": "openclaw", "source_manifest_sha256": digest(source_bytes), "export_sha256": digest(json.dumps(inventory, sort_keys=True).encode()), "files": inventory}
    (context / "LAB_ADAPTER_MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    dockerfile = repository / "compose" / "candidates" / "openclaw" / "Dockerfile"
    shutil.copyfile(dockerfile, context / "Dockerfile")
    (context / ".dockerignore").write_text(".git\nold\n**/node_modules\n.env\n.env.*\n**/.venv\n", encoding="utf-8")
    return {"candidate": "openclaw", "context": str(context), "adapter_export_sha256": manifest["export_sha256"], "dockerfile_sha256": digest(dockerfile.read_bytes())}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", type=Path, default=Path("external/build-context/openclaw"))
    args = parser.parse_args()
    print(json.dumps(stage(args.context, Path(__file__).resolve().parents[1])))


if __name__ == "__main__":
    main()
