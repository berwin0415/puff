"""Verify the fixed ADK source export, adapter and noneditable installed wheel."""

import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import tomllib
from urllib.parse import unquote, urlparse


COMMIT = "365084db7b14e796f36ad2929f987e8d83874961"
ARCHIVE_HASH = "1738a04484fb362e3cc7891b6b3593eadc64a86df1b63839e34e2df1e6317571"
EXPORT_HASH = "b02725196e3e362f0e389c68a0ff35ba83ee163932f9cd564c66e919aa5ac905"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def verify_source(*, source_only=False) -> dict:
    materials = Path(__file__).resolve().parents[1]
    source_bytes = (materials / "SOURCE_MANIFEST.json").read_bytes()
    source = json.loads(source_bytes)
    pins = source.get("sources", [])
    if source.get("schema") != "harness-lab-source-manifest/1" or source.get("candidate") != "google_adk" or source.get("export_sha256") != EXPORT_HASH or len(pins) != 1:
        raise ValueError("Wrong Google ADK source manifest")
    pin = pins[0]
    if pin.get("id") != "main" or pin.get("kind") != "archive" or pin.get("commit") != COMMIT or pin.get("sha256") != ARCHIVE_HASH or pin.get("observed_archive_sha256") != ARCHIVE_HASH or pin.get("dirty") is not None:
        raise ValueError("Wrong or unverified ADK source archive")

    def inventory(entries):
        if not isinstance(entries, list) or not entries:
            raise ValueError("Empty provenance inventory")
        seen = set()
        for entry in entries:
            relative = entry.get("path")
            if not isinstance(relative, str) or Path(relative).is_absolute() or relative in seen or any(part in ("", ".", "..") or part.lower() == "old" for part in relative.split("/")):
                raise ValueError("Unsafe or duplicate provenance path")
            seen.add(relative)
            path = materials / relative
            if not path.resolve().is_relative_to(materials):
                raise ValueError("Provenance link escapes materials")
            if entry.get("type") == "symlink":
                if not path.is_symlink() or str(path.readlink()) != entry.get("target"):
                    raise ValueError("Source symbolic link mismatch")
                content = str(path.readlink()).encode()
            elif entry.get("type") == "file":
                if not path.is_file() or path.is_symlink():
                    raise ValueError("Source file kind mismatch")
                content = path.read_bytes()
                if len(content) != entry.get("size"):
                    raise ValueError("Source file size mismatch")
            else:
                raise ValueError("Unknown provenance file kind")
            if digest(content) != entry.get("sha256"):
                raise ValueError(f"Image material hash mismatch: {relative}")

    inventory(source.get("files"))
    if digest(json.dumps(source["files"], sort_keys=True).encode()) != EXPORT_HASH:
        raise ValueError("Source inventory digest mismatch")
    provenance = {"candidate": "google_adk", "commit": COMMIT, "archive_sha256": ARCHIVE_HASH, "source_export_sha256": EXPORT_HASH, "source_file_count": len(source["files"])}
    if source_only:
        return provenance
    adapter = json.loads((materials / "LAB_ADAPTER_MANIFEST.json").read_bytes())
    if adapter.get("schema") != "harness-lab-adapter-manifest/1" or adapter.get("candidate") != "google_adk" or adapter.get("source_manifest_sha256") != digest(source_bytes):
        raise ValueError("Adapter/source manifest mismatch")
    inventory(adapter.get("files"))
    if digest(json.dumps(adapter["files"], sort_keys=True).encode()) != adapter.get("export_sha256"):
        raise ValueError("Adapter inventory digest mismatch")
    original = tomllib.loads((materials / "sources/main/pyproject.toml").read_text())
    project = tomllib.loads((materials / "harness-lab/pyproject.toml").read_text())
    metadata = project["tool"]["uv"]["dependency-metadata"][0]
    if metadata["name"] != "google-adk" or metadata["version"] != "2.12.0" or metadata["requires-dist"] != original["project"]["dependencies"]:
        raise ValueError("Dynamic-version resolution metadata differs from pinned source dependencies")
    distribution = importlib.metadata.distribution("google-adk")
    direct = json.loads(distribution.read_text("direct_url.json") or "{}")
    url = urlparse(direct.get("url", ""))
    if distribution.version != "2.12.0" or url.scheme != "file" or url.netloc or Path(unquote(url.path)).resolve() != materials / "sources/main" or direct.get("dir_info", {}).get("editable") is True:
        raise ValueError("Installed ADK source wheel provenance mismatch")
    installed = {}
    for name, expected in (("google-adk", "2.12.0"), ("litellm", "1.85.7"), ("openai", "2.46.0"), ("google-genai", "2.30.0"), ("flit-core", "3.12.0")):
        version = importlib.metadata.version(name)
        if version != expected:
            raise ValueError(f"Wrong frozen dependency installation: {name}")
        installed[name] = version
    return {**provenance, "adapter_export_sha256": adapter["export_sha256"], "adapter_file_count": len(adapter["files"]), "installed_distributions": installed, "adk_installation": "noneditable_wheel_built_from_verified_source"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-only", action="store_true")
    args = parser.parse_args()
    print(json.dumps(verify_source(source_only=args.source_only), sort_keys=True))
