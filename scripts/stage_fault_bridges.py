#!/usr/bin/env python3
"""Prepare a NEW I23 build context with a thin prewrite bridge overlay.

Host materials work only. Candidate packages, models, tools and scoring never
run here. Existing stagers and I22 bridge files remain completely unchanged.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys


OVERLAYS = {
    "deepagents_langgraph": ("http_tools.py", "prewrite_gate.py"),
    "deepseek_harness": ("tools-plugin.mjs", "prewrite-gate.mjs"),
}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def stage(context, repository, candidate, *, goal_runtime=False):
    repository, context = repository.resolve(), context.resolve()
    allowed = repository / "external/build-context"
    if "old" in context.parts or not context.is_relative_to(allowed) or context == allowed:
        raise ValueError("I23 must use a new current candidate source context")
    if any((context / name).exists() or (context / name).is_symlink()
            for name in ("harness-lab", "LAB_ADAPTER_MANIFEST.json", "LAB_FAULT_BRIDGE_MANIFEST.json", "Dockerfile")):
        raise ValueError("I23 overlay must never overwrite any existing staged material")
    if candidate not in OVERLAYS or goal_runtime and candidate != "deepseek_harness":
        raise ValueError("Unexpected candidate/Goal runtime combination")
    payloads = {name: (repository / "adapters" / candidate / "fault" / name).read_bytes()
        for name in OVERLAYS[candidate]}
    origins = {name: f"adapters/{candidate}/fault/{name}" for name in payloads}
    if candidate == 'deepseek_harness':
        if not goal_runtime:
            raise ValueError('DSH I23 requires the separately frozen fault Goal runtime for both A and B')
        for name in ('fault-goal-binding.mjs', 'fault-goal-runtime.ts', 'fault-goal-runtime.cordis.patch.yml',
                     'fault-goal-entrypoint.ts', 'fault-scoped-runtime-entrypoint.ts'):
            payloads[name] = (repository / 'adapters' / candidate / name).read_bytes()
            origins[name] = f'adapters/{candidate}/{name}'
    if candidate == 'deepagents_langgraph':
        for name in ('fault_recovery.py', 'fault_entrypoint.py'):
            payloads[name] = (repository / 'adapters' / candidate / name).read_bytes()
            origins[name] = f'adapters/{candidate}/{name}'
    sys.path.insert(0, str(repository))
    if candidate == "deepagents_langgraph":
        from scripts.stage_deepagents_langgraph import stage as base_stage
        base_stage(context, repository)
    else:
        from scripts.stage_deepseek_harness import stage as base_stage
        base_stage(context, repository, goal_runtime=goal_runtime)
    manifest_path = context / "LAB_ADAPTER_MANIFEST.json"
    try:
        manifest = json.loads(manifest_path.read_bytes())
        base_export = manifest["export_sha256"]
        by_path = {entry["path"]: entry for entry in manifest["files"]}
        sources = []
        for name, data in payloads.items():
            path = "harness-lab/" + name
            (context / path).write_bytes(data)
            by_path[path] = {"path": path, "type": "file", "size": len(data), "sha256": sha(data)}
            sources.append({"repository_path": origins[name], "image_material_path": path,
                "size": len(data), "sha256": sha(data)})
        manifest["files"] = sorted(by_path.values(), key=lambda entry: entry["path"])
        manifest["export_sha256"] = sha(json.dumps(manifest["files"], sort_keys=True).encode())
        manifest["variant"] = "fault-prewrite-bridge"
        manifest["base_adapter_export_sha256"] = base_export
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
        overlay = {"schema": "harness-lab-fault-bridge-manifest/1", "candidate": candidate,
            "variant": "fault-prewrite-bridge", "goal_runtime_materials_included": goal_runtime,
            "base_adapter_export_sha256": base_export, "adapter_export_sha256": manifest["export_sha256"],
            "sources": sources, "runtime_verified": False, "candidate_crash_recovery_verified": False}
        overlay['native_crash_entry'] = 'fault_entrypoint.py' if candidate == 'deepagents_langgraph' else 'fault-goal-entrypoint.ts'
        (context / "LAB_FAULT_BRIDGE_MANIFEST.json").write_text(json.dumps(overlay, indent=2) + "\n")
        return {"candidate": candidate, "context": str(context), "adapter_export_sha256": manifest["export_sha256"],
            "status": "materials_staged_not_runtime_verified"}
    except BaseException:
        # Partial overlay bytes must not leave a usable-looking build context.
        (context / "Dockerfile").unlink(missing_ok=True)
        manifest_path.unlink(missing_ok=True)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", choices=tuple(OVERLAYS), required=True)
    parser.add_argument("--context", type=Path, required=True)
    parser.add_argument("--goal-runtime", action="store_true")
    args = parser.parse_args()
    print(json.dumps(stage(args.context, Path(__file__).resolve().parents[1], args.candidate,
        goal_runtime=args.goal_runtime)))


if __name__ == "__main__":
    main()
