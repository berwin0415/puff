#!/usr/bin/env python3
"""Freeze official npm release materials without executing any candidate code."""

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import shutil
import tarfile
from urllib.parse import quote, urlparse
from urllib.request import urlopen


COMMIT = "83086188ae0614bd882818ff1aa23f3194962012"
ARCHIVE_HASH = "e57f1d4f914c71c3532a12d5c14f740909fca4e7e041fa1b3db71618c88e5567"
EXPORT_HASH = "c3c767a93ef93cdc22666039a48be5a2292b0929c48bd933d39500e097fcd156"
PACKAGES = (
    ("sdk", "@openai/codex-sdk", "0.163.0", "@openai/codex-sdk", "sha512-o10d8dqxFjhzoWp75MUrhaN8C6QrXPQqUJGwVnySj++ztAnrA322UuFRzHZvSa903lFgjjaimuf5rkqVsj0RHg=="),
    ("cli", "@openai/codex", "0.163.0", "@openai/codex", "sha512-HNd7vR9oTK6M2ikxFnvNDCdYUgAsyo9VVnd5KnEd2ON0GYI++7MiX9XNPUkhtdgGKFPsWS+CEppQ1UU63uVhUw=="),
    ("linux-arm64", "@openai/codex", "0.163.0-linux-arm64", "@openai/codex-linux-arm64", "sha512-IH2/R+4OarjGvsAf1ll3fdyLwrPgYLl3uzp6u9CyD9sj2Lw+ViDSe3QBZVHdcHJs0eIiZFg6Wprcuy4CRZaUGw=="),
)
ADAPTER_FILES = ("entrypoint.mjs", "http-mcp-bridge.mjs", "verify-source.mjs", "README.md")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fetch(url: str) -> bytes:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.netloc != "registry.npmjs.org":
        raise ValueError("Package materials must come from the official npm registry")
    with urlopen(url, timeout=60) as response:
        return response.read()


def save_json(path: Path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def inventory(root: Path, entries) -> list[dict]:
    result = []
    for path in sorted(entries):
        if not path.is_file() or path.is_symlink():
            raise ValueError("Registry export contains an unexpected file kind")
        data = path.read_bytes()
        result.append({"path": path.relative_to(root).as_posix(), "type": "file", "size": len(data), "sha256": digest(data), "mode": path.stat().st_mode & 0o777})
    return result


def prepare_packages(context: Path):
    registry, packages = context / "registry", context / "packages"
    for path in (registry, packages):
        if path.is_symlink():
            raise ValueError("Registry staging must not follow links")
        if path.exists():
            if not (context / "REGISTRY_MANIFEST.json").is_file():
                raise ValueError("Refusing to replace unrecognized registry material")
            shutil.rmtree(path)
        path.mkdir()
    records = []
    for label, name, version, install_name, integrity in PACKAGES:
        metadata_url = "https://registry.npmjs.org/" + quote(name, safe="@") + "/" + version
        metadata_bytes = fetch(metadata_url)
        metadata = json.loads(metadata_bytes)
        if metadata.get("name") != name or metadata.get("version") != version or metadata.get("dist", {}).get("integrity") != integrity:
            raise ValueError("Registry metadata does not match frozen release")
        attestation_url = metadata["dist"]["attestations"]["url"]
        attestation_bytes = fetch(attestation_url)
        attestations = json.loads(attestation_bytes)
        tarball_url = metadata["dist"]["tarball"]
        tarball = fetch(tarball_url)
        sha512 = hashlib.sha512(tarball).digest()
        if "sha512-" + base64.b64encode(sha512).decode() != integrity or hashlib.sha1(tarball).hexdigest() != metadata["dist"]["shasum"]:
            raise ValueError("Published tarball integrity mismatch")
        provenance = []
        for attestation in attestations["attestations"]:
            statement = json.loads(base64.b64decode(attestation["bundle"]["dsseEnvelope"]["payload"], validate=True))
            if not any(subject.get("digest", {}).get("sha512") == sha512.hex() for subject in statement.get("subject", [])):
                raise ValueError("Attestation subject does not match exact tarball")
            if statement.get("predicateType") == "https://slsa.dev/provenance/v1":
                definition = statement["predicate"]["buildDefinition"]
                workflow = definition["externalParameters"]["workflow"]
                if workflow.get("repository") != "https://github.com/openai/codex" or workflow.get("ref") != "refs/tags/rust-v0.163.0":
                    raise ValueError("Wrong official release workflow")
                if not any(item.get("uri", "").startswith("git+https://github.com/openai/codex@") and item.get("digest", {}).get("gitCommit") == COMMIT for item in definition["resolvedDependencies"]):
                    raise ValueError("Published release provenance uses a different source commit")
                provenance.append(statement)
        if not provenance:
            raise ValueError("Missing source-linked SLSA provenance")
        (registry / f"{label}.metadata.json").write_bytes(metadata_bytes)
        (registry / f"{label}.attestations.json").write_bytes(attestation_bytes)
        (registry / f"{label}.tgz").write_bytes(tarball)
        target = packages / install_name
        target.mkdir(parents=True)
        seen = set()
        with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as archive:
            for member in archive.getmembers():
                relative = PurePosixPath(member.name)
                if relative.is_absolute() or not relative.parts or relative.parts[0] != "package" or any(part in (".", "..") or part.lower() == "old" for part in relative.parts):
                    raise ValueError("Unsafe package tar path")
                if member.isdir():
                    continue
                if not member.isfile() or member.name in seen or len(relative.parts) < 2:
                    raise ValueError("Unexpected package member kind or duplicate")
                seen.add(member.name)
                destination = target.joinpath(*relative.parts[1:])
                destination.parent.mkdir(parents=True, exist_ok=True)
                content = archive.extractfile(member).read()
                if len(content) != member.size:
                    raise ValueError("Truncated package member")
                destination.write_bytes(content)
                destination.chmod(0o755 if member.mode & 0o111 else 0o644)
        package = json.loads((target / "package.json").read_bytes())
        if package.get("name") != name or package.get("version") != version or package.get("dependencies", {}) != metadata.get("dependencies", {}):
            raise ValueError("Exported package metadata mismatch")
        if label == "sdk" and package.get("dependencies") != {"@openai/codex": "0.163.0"}:
            raise ValueError("Unexpected SDK runtime dependency closure")
        if label == "cli" and (package.get("dependencies") or package.get("optionalDependencies", {}).get("@openai/codex-linux-arm64") != "npm:@openai/codex@0.163.0-linux-arm64"):
            raise ValueError("Unexpected CLI platform dependency closure")
        if label == "linux-arm64" and (package.get("dependencies") or package.get("optionalDependencies") or package.get("os") != ["linux"] or package.get("cpu") != ["arm64"]):
            raise ValueError("Unexpected platform runtime dependency closure")
        records.append({"id": label, "name": name, "version": version, "install_name": install_name, "integrity": integrity, "tarball_sha256": digest(tarball), "metadata_url": metadata_url, "tarball_url": tarball_url, "attestation_url": attestation_url, "source_commit": COMMIT, "provenance_check": "HTTPS_registry_material_and_subject_and_workflow_and_commit;_Sigstore_signature_chain_not_independently_verified"})
    files = inventory(context, list(registry.rglob("*.*")) + [path for path in packages.rglob("*") if path.is_file()])
    manifest = {"schema": "harness-lab-npm-release-manifest/1", "candidate": "codex_sdk", "created_at": datetime.now(timezone.utc).isoformat(), "source_commit": COMMIT, "target": "linux-arm64", "packages": records, "files": files, "export_sha256": digest(json.dumps(files, sort_keys=True).encode())}
    save_json(context / "REGISTRY_MANIFEST.json", manifest)


def stage(context: Path, repository: Path, *, fetch_packages=False) -> dict:
    context = context.resolve()
    if "old" in context.parts:
        raise ValueError("Cannot stage archived materials")
    source_bytes = (context / "SOURCE_MANIFEST.json").read_bytes()
    source = json.loads(source_bytes)
    pins = source.get("sources", [])
    if source.get("schema") != "harness-lab-source-manifest/1" or source.get("candidate") != "codex_sdk" or source.get("export_sha256") != EXPORT_HASH or len(pins) != 1:
        raise ValueError("Wrong official source context")
    pin = pins[0]
    if pin.get("commit") != COMMIT or pin.get("sha256") != ARCHIVE_HASH or pin.get("observed_archive_sha256") != ARCHIVE_HASH or pin.get("dirty") is not None:
        raise ValueError("Wrong or unverified source archive")
    if fetch_packages:
        prepare_packages(context)
    release_bytes = (context / "REGISTRY_MANIFEST.json").read_bytes()
    release = json.loads(release_bytes)
    if release.get("schema") != "harness-lab-npm-release-manifest/1" or release.get("source_commit") != COMMIT or len(release.get("packages", [])) != 3:
        raise ValueError("Prepare the frozen three-package closure with --fetch-packages")
    destination = context / "harness-lab"
    if destination.exists():
        if destination.is_symlink() or not (context / "LAB_ADAPTER_MANIFEST.json").is_file():
            raise ValueError("Refusing to replace unstaged adapter")
        shutil.rmtree(destination)
    destination.mkdir()
    for name in ADAPTER_FILES:
        shutil.copyfile(repository / "adapters/codex_sdk" / name, destination / name)
    files = inventory(context, [destination / name for name in ADAPTER_FILES])
    manifest = {"schema": "harness-lab-adapter-manifest/1", "candidate": "codex_sdk", "source_manifest_sha256": digest(source_bytes), "registry_manifest_sha256": digest(release_bytes), "files": files, "export_sha256": digest(json.dumps(files, sort_keys=True).encode())}
    save_json(context / "LAB_ADAPTER_MANIFEST.json", manifest)
    dockerfile = repository / "compose/candidates/codex_sdk/Dockerfile"
    shutil.copyfile(dockerfile, context / "Dockerfile")
    (context / ".dockerignore").write_text(".git\nold\n.env\n.env.*\n**/.venv\n")
    return {"candidate": "codex_sdk", "context": str(context), "adapter_export_sha256": manifest["export_sha256"], "registry_export_sha256": release["export_sha256"], "dockerfile_sha256": digest(dockerfile.read_bytes())}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", type=Path, default=Path("external/build-context/codex_sdk"))
    parser.add_argument("--fetch-packages", action="store_true")
    args = parser.parse_args()
    print(json.dumps(stage(args.context, Path(__file__).resolve().parents[1], fetch_packages=args.fetch_packages)))
