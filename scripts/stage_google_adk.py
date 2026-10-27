#!/usr/bin/env python3
"""准备 Google ADK 源码和适配材料；不导入候选 SDK 代码。"""

import argparse
import hashlib
import json
from pathlib import Path
import shutil


FILES = ("entrypoint.py", "http_tools.py", "verify_source.py", "pyproject.toml", "uv.lock", "bootstrap-requirements.txt", "README.md")
COMMIT = "365084db7b14e796f36ad2929f987e8d83874961"
ARCHIVE_HASH = "1738a04484fb362e3cc7891b6b3593eadc64a86df1b63839e34e2df1e6317571"
EXPORT_HASH = "b02725196e3e362f0e389c68a0ff35ba83ee163932f9cd564c66e919aa5ac905"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def stage(context: Path, repository: Path, *, prepare_lock=False) -> dict:
    context = context.resolve()
    if "old" in context.parts:
        raise ValueError('不能向归档材料目录准备文件')
    source_bytes = (context / "SOURCE_MANIFEST.json").read_bytes()
    source = json.loads(source_bytes)
    if source.get("schema") != "harness-lab-source-manifest/1" or source.get("candidate") != "google_adk" or source.get("export_sha256") != EXPORT_HASH:
        raise ValueError('需要当前 Google ADK 上下文')
    pins = source.get("sources", [])
    if len(pins) != 1:
        raise ValueError('源码清单不匹配')
    pin = pins[0]
    if pin.get("id") != "main" or pin.get("kind") != "archive" or pin.get("commit") != COMMIT or pin.get("sha256") != ARCHIVE_HASH or pin.get("observed_archive_sha256") != ARCHIVE_HASH or pin.get("dirty") is not None:
        raise ValueError('Google ADK 源码归档不匹配或未经核验')
    destination = context / "harness-lab"
    marker = context / "LAB_LOCK_PREPARATION.json"
    if destination.exists():
        if destination.is_symlink() or not ((context / "LAB_ADAPTER_MANIFEST.json").is_file() or marker.is_file()):
            raise ValueError('拒绝替换未经准备登记的适配材料')
        shutil.rmtree(destination)
    destination.mkdir()
    inventory = []
    for name in (("pyproject.toml",) if prepare_lock else FILES):
        data = (repository / "adapters" / "google_adk" / name).read_bytes()
        (destination / name).write_bytes(data)
        inventory.append({"path": "harness-lab/" + name, "type": "file", "size": len(data), "sha256": digest(data)})
    if prepare_lock:
        marker.write_text(json.dumps({"candidate": "google_adk", "purpose": "lock_material_only", "source_manifest_sha256": digest(source_bytes)}, indent=2) + "\n")
        (context / "Dockerfile").unlink(missing_ok=True)
        (context / "LAB_ADAPTER_MANIFEST.json").unlink(missing_ok=True)
        return {"candidate": "google_adk", "status": "lock_material_only_not_buildable", "context": str(context)}
    manifest = {"schema": "harness-lab-adapter-manifest/1", "candidate": "google_adk", "source_manifest_sha256": digest(source_bytes), "export_sha256": digest(json.dumps(inventory, sort_keys=True).encode()), "files": inventory}
    (context / "LAB_ADAPTER_MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n")
    dockerfile = repository / "compose" / "candidates" / "google_adk" / "Dockerfile"
    shutil.copyfile(dockerfile, context / "Dockerfile")
    (context / ".dockerignore").write_text(".git\nold\n**/node_modules\n.env\n.env.*\n**/.venv\nLAB_LOCK_PREPARATION.json\n")
    marker.unlink(missing_ok=True)
    return {"candidate": "google_adk", "context": str(context), "adapter_export_sha256": manifest["export_sha256"], "dockerfile_sha256": digest(dockerfile.read_bytes())}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", type=Path, default=Path("external/build-context/google_adk"))
    parser.add_argument("--prepare-lock", action="store_true")
    args = parser.parse_args()
    print(json.dumps(stage(args.context, Path(__file__).resolve().parents[1], prepare_lock=args.prepare_lock)))
