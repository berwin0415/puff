"""Source preparation rejects stale pins and excludes runtime/private material."""

import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import tarfile

import pytest
import yaml


spec = importlib.util.spec_from_file_location(
    "prepare_candidate_source", Path(__file__).parents[1] / "scripts/prepare_candidate_source.py"
)
exporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exporter)


def archive_config(tmp_path, entries):
    archive = tmp_path / "source.tar.gz"
    with tarfile.open(archive, "w:gz") as stream:
        for name, value in entries.items():
            item = tarfile.TarInfo("sample-" + "a" * 40 + "/" + name)
            data = value.encode()
            item.size = len(data)
            stream.addfile(item, io.BytesIO(data))
    source = {
        "id": "main", "kind": "archive", "path": str(archive), "commit": "a" * 40,
        "official_url": "https://github.com/example/sample",
        "archive_prefix": "sample-" + "a" * 40,
        "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
    }
    config = tmp_path / "candidate.yaml"
    config.write_text(yaml.safe_dump({"candidate": "sample", "sources": [source]}))
    return config, source


def test_exports_source_license_and_lock_without_host_state(tmp_path):
    config, _ = archive_config(tmp_path, {
        "main.py": "print('hello')", "LICENSE": "MIT", "uv.lock": "locked",
        ".env": "credential", "node_modules/a.js": "host binary", ".venv/bin/python": "host python",
        "old/run.json": "history", "workspaces/user/private.txt": "private",
    })
    result = exporter.prepare(config, tmp_path / "out")
    destination = Path(result["destination"])
    manifest = json.loads((destination / "SOURCE_MANIFEST.json").read_text())
    assert {x["path"] for x in manifest["files"]} == {
        "sources/main/main.py", "sources/main/LICENSE", "sources/main/uv.lock"
    }
    assert manifest["sources"][0]["dirty"] is None
    assert manifest["sources"][0]["observed_archive_sha256"]
    exporter.prepare(config, tmp_path / "out")  # Rebuilding our generated context is supported.


def test_modified_archive_is_not_exported(tmp_path):
    config, source = archive_config(tmp_path, {"main.py": "hello"})
    Path(source["path"]).write_bytes(b"changed")
    with pytest.raises(ValueError, match="hash mismatch"):
        exporter.prepare(config, tmp_path / "out")
    assert not (tmp_path / "out/sample").exists()


def test_archive_cannot_escape_context(tmp_path):
    config, _ = archive_config(tmp_path, {"../../escape.txt": "bad"})
    with pytest.raises(ValueError, match="unsafe archive"):
        exporter.prepare(config, tmp_path / "out")
    assert not (tmp_path / "escape.txt").exists()


def test_export_does_not_replace_unowned_directory(tmp_path):
    config, _ = archive_config(tmp_path, {"main.py": "hello"})
    destination = tmp_path / "out/sample"
    destination.mkdir(parents=True)
    (destination / "keep.txt").write_text("user content")
    with pytest.raises(ValueError, match="not generated"):
        exporter.prepare(config, tmp_path / "out")
    assert (destination / "keep.txt").read_text() == "user content"


def test_absolute_symlink_is_rejected(tmp_path):
    output = tmp_path / "export"
    output.mkdir()
    with pytest.raises(ValueError, match="escapes"):
        exporter.write_link(output, Path("link"), "/etc/passwd")


@pytest.mark.skipif(shutil.which("git") is None, reason="Git is required for Git-source verification")
def test_dirty_git_checkout_is_not_exported(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    (repo / "main.py").write_text("initial")
    subprocess.run(["git", "-C", str(repo), "add", "main.py"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "fixture"], check=True, capture_output=True)
    commit = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    (repo / "main.py").write_text("modified")
    with pytest.raises(ValueError, match="dirty source"):
        exporter.export_git({"id": "main", "path": str(repo), "commit": commit}, tmp_path / "out")
