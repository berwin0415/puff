"""Unrun source-only staging guards; synthetic files are not runtime evidence."""
from copy import deepcopy
import json

import pytest

from scripts import stage_deepseek_runtime as stager


def staging_fixture(tmp_path):
    repository = tmp_path / "repo"
    root = repository / "external/build-context"
    base, context = root / "deepseek_harness-pause", root / "deepseek_harness-multi-user"
    base.mkdir(parents=True)
    context.mkdir()
    source = {"schema": "harness-lab-source-manifest/1", "candidate": "deepseek_harness", "export_sha256": "1" * 64,
        "sources": [{"id": "main", "commit": stager.COMMIT, "observed_commit": stager.COMMIT, "dirty": False}], "files": []}
    raw = json.dumps(source).encode()
    for directory in (base, context):
        (directory / "SOURCE_MANIFEST.json").write_bytes(raw)
    (base / "harness-lab").mkdir()
    data = b"// synthetic, never executed\n"
    entries = []
    for name in stager.GOAL_FILES:
        (base / "harness-lab" / name).write_bytes(data)
        entries.append({"path": "harness-lab/" + name, "type": "file", "size": len(data), "sha256": stager.sha(data)})
    manifest = {"schema": "harness-lab-adapter-manifest/1", "candidate": "deepseek_harness",
        "source_manifest_sha256": stager.sha(raw), "files": entries,
        "export_sha256": stager.sha(json.dumps(entries, sort_keys=True).encode())}
    (base / "LAB_ADAPTER_MANIFEST.json").write_text(json.dumps(manifest))
    (base / "Dockerfile").write_text("# synthetic packaging fixture only\n")
    (base / ".dockerignore").write_text(".git\nold\n**/node_modules\n.env*\n")
    for name in stager.OVERLAYS:
        destination = repository / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("# synthetic overlay\n")
    return repository, base, context, manifest


def test_new_runtime_copies_frozen_bytes_and_refuses_overwrite(tmp_path):
    repository, base, context, original = staging_fixture(tmp_path)
    before = (base / "LAB_ADAPTER_MANIFEST.json").read_bytes()
    result = stager.stage(context, base, repository)
    assert result["status"] == "materials_staged_not_runtime_verified"
    assert (base / "LAB_ADAPTER_MANIFEST.json").read_bytes() == before
    current = json.loads((context / "LAB_ADAPTER_MANIFEST.json").read_text())
    assert current["base_adapter_export_sha256"] == original["export_sha256"]
    assert current["export_sha256"] != original["export_sha256"]
    runtime = json.loads((context / "LAB_RUNTIME_MANIFEST.json").read_text())
    assert runtime["runtime_verified"] is runtime["capability_verified"] is False
    assert len(runtime["sources"]) == len(stager.OVERLAYS)
    assert (context / ".dockerignore").read_bytes() == (base / ".dockerignore").read_bytes()
    with pytest.raises(ValueError, match="cannot overwrite"):
        stager.stage(context, base, repository)


def test_stager_rejects_modified_i22_bytes_before_writing(tmp_path):
    repository, base, context, _ = staging_fixture(tmp_path)
    (base / "harness-lab/goal-runtime-entrypoint.ts").write_text("changed")
    with pytest.raises(ValueError, match="bytes changed"):
        stager.stage(context, base, repository)
    assert not (context / "harness-lab").exists()


def test_stager_cannot_target_i22_or_an_archived_context(tmp_path):
    repository, base, context, _ = staging_fixture(tmp_path)
    for forbidden in (base, repository / "external/build-context/old/new-runtime"):
        with pytest.raises(ValueError, match="Separate current I24"):
            stager.stage(forbidden, base, repository)


def test_stager_rejects_different_export_and_linked_context(tmp_path):
    repository, base, context, _ = staging_fixture(tmp_path)
    source = json.loads((context / "SOURCE_MANIFEST.json").read_text())
    source["export_sha256"] = "2" * 64
    (context / "SOURCE_MANIFEST.json").write_text(json.dumps(source))
    with pytest.raises(ValueError, match="match the frozen I22"):
        stager.stage(context, base, repository)
    linked = repository / "external/build-context/linked"
    linked.symlink_to(context, target_is_directory=True)
    with pytest.raises(ValueError, match="Linked staging"):
        stager.stage(linked, base, repository)
