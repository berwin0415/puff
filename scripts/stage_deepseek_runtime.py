#!/usr/bin/env python3
"""Stage a NEW I24 overlay from frozen I22 bytes; never overwrite materials.

Source export must already exist. This does not start Docker, import a candidate,
run tests or access credentials. The later image verifier checks the full tree.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMMIT = "dd6322d604e00eec1ba5e0c8541159906a21094a"
GOAL_FILES = ("entrypoint.ts", "tools-plugin.mjs", "cordis.patch.yml", "verify-source.mjs",
    "detailed-options.mjs", "scoped-runtime-entrypoint.ts", "scoped-runtime.cordis.patch.yml",
    "business-extension.mjs", "goal-runtime-entrypoint.ts", "goal-runtime.ts", "goal-binding.mjs",
    "goal-runtime.cordis.patch.yml", "goal-surface-entrypoint.ts")
OVERLAYS = (
    "execution/__init__.py", "execution/worker_control.py", "execution/dsh_materials.py",
    "execution/worker_control_dsh.py", "runner/__init__.py", "runner/shared_backend.py",
    "runner/shared_supervisor.py", "runner/dsh_goal.py",
)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def source_manifest(context):
    raw = (context / "SOURCE_MANIFEST.json").read_bytes()
    value = json.loads(raw)
    sources = value.get("sources", [])
    if (value.get("schema") != "harness-lab-source-manifest/1" or value.get("candidate") != "deepseek_harness"
            or len(sources) != 1 or sources[0].get("id") != "main" or sources[0].get("commit") != COMMIT
            or sources[0].get("observed_commit") != COMMIT or sources[0].get("dirty") is not False):
        raise ValueError("Frozen clean DSH source export required")
    return raw, value


def stage(context, base_context, repository=ROOT):
    repository = Path(repository).resolve()
    raw_context, raw_base = Path(context).absolute(), Path(base_context).absolute()
    if any(path.is_symlink() for root in (raw_context, raw_base) for path in (root, *root.parents)):
        raise ValueError("Linked staging paths are forbidden")
    context, base = raw_context.resolve(), raw_base.resolve()
    allowed = repository / "external/build-context"
    if (not context.is_relative_to(allowed) or context == allowed or context == base
            or context.is_relative_to(base) or base.is_relative_to(context)
            or "old" in context.parts or "old" in base.parts
            or context.name in {"deepseek_harness", "deepseek_harness-pause", "deepseek_harness-fault"}):
        raise ValueError("Separate current I24 context required")
    if any((context / name).exists() or (context / name).is_symlink() for name in
           ("harness-lab", "Dockerfile", ".dockerignore", "LAB_ADAPTER_MANIFEST.json", "LAB_RUNTIME_MANIFEST.json")):
        raise ValueError("Runtime staging cannot overwrite existing materials")
    raw_source, source = source_manifest(context)
    base_source_raw, base_source = source_manifest(base)
    if source.get("export_sha256") != base_source.get("export_sha256") or source.get("files") != base_source.get("files"):
        raise ValueError("I24 source tree must match the frozen I22 export")
    base_manifest_raw = (base / "LAB_ADAPTER_MANIFEST.json").read_bytes()
    manifest = json.loads(base_manifest_raw)
    if (manifest.get("schema") != "harness-lab-adapter-manifest/1" or manifest.get("candidate") != "deepseek_harness"
            or manifest.get("source_manifest_sha256") != sha(base_source_raw)):
        raise ValueError("Frozen I22 adapter/source binding required")
    entries, payloads = {}, {}
    for entry in manifest.get("files", []):
        relative = Path(entry["path"])
        if (relative.is_absolute() or ".." in relative.parts or "old" in relative.parts
                or not relative.is_relative_to("harness-lab") or entry.get("type") != "file"
                or entry["path"] in entries):
            raise ValueError("Bound regular I22 adapter files required")
        origin = base / relative
        if any(path.is_symlink() for path in (origin, *origin.parents)):
            raise ValueError("Linked frozen adapter source")
        data = origin.read_bytes()
        if sha(data) != entry.get("sha256") or len(data) != entry.get("size"):
            raise ValueError("Frozen I22 adapter bytes changed")
        entries[entry["path"]], payloads[entry["path"]] = dict(entry), data
    if (set(entries) != {"harness-lab/" + name for name in GOAL_FILES}
            or sha(json.dumps(manifest["files"], sort_keys=True).encode()) != manifest.get("export_sha256")):
        raise ValueError("Verified I22 Goal adapter inventory required")
    sources = []
    for name in OVERLAYS:
        data = (repository / name).read_bytes()
        relative = "harness-lab/" + name
        if relative in entries:
            raise ValueError("I24 overlay cannot replace a frozen I22 adapter file")
        payloads[relative] = data
        entries[relative] = {"path": relative, "type": "file", "size": len(data), "sha256": sha(data)}
        sources.append({"repository_path": name, "image_material_path": relative, "sha256": sha(data), "size": len(data)})
    dockerfile = (base / "Dockerfile").read_bytes()
    dockerignore = (base / ".dockerignore").read_bytes()
    manifest = {**manifest, "source_manifest_sha256": sha(raw_source), "runtime_variant": "multi-user",
        "base_adapter_export_sha256": manifest["export_sha256"], "files": sorted(entries.values(), key=lambda item: item["path"])}
    manifest["export_sha256"] = sha(json.dumps(manifest["files"], sort_keys=True).encode())
    # All reads/validation precede writes. A partially failed context remains
    # visible and cannot be silently retried or written over.
    (context / "harness-lab").mkdir()
    for name, data in payloads.items():
        destination = context / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("xb") as stream:
            stream.write(data)
    with (context / "Dockerfile").open("xb") as stream:
        stream.write(dockerfile)
    with (context / ".dockerignore").open("xb") as stream:
        stream.write(dockerignore)
    (context / "LAB_ADAPTER_MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (context / "LAB_RUNTIME_MANIFEST.json").write_text(json.dumps({"schema": "harness-lab-runtime-manifest/1",
        "candidate": "deepseek_harness", "variant": "multi-user", "sources": sources,
        "base_adapter_manifest_sha256": sha(base_manifest_raw), "base_adapter_export_sha256": manifest["base_adapter_export_sha256"],
        "adapter_export_sha256": manifest["export_sha256"], "dockerfile_sha256": sha(dockerfile),
        "dockerignore_sha256": sha(dockerignore),
        "python_package_root": "/opt/materials/harness-lab", "runtime_verified": False, "capability_verified": False}, indent=2) + "\n")
    return {"context": str(context), "variant": "multi-user", "adapter_export_sha256": manifest["export_sha256"],
        "status": "materials_staged_not_runtime_verified"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", type=Path, required=True)
    parser.add_argument("--base-context", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(stage(args.context, args.base_context)))
