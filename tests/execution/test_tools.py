"""文件工具不能沿 Agent 指定路径进入其他工作区。"""

import os

import pytest

from execution.tools.basic import ToolError, WorkspaceFiles, calculate_totals


def test_workspace_round_trip_and_other_user_is_inaccessible(tmp_path):
    a = WorkspaceFiles(tmp_path.resolve() / "user-a")
    b = WorkspaceFiles(tmp_path.resolve() / "user-b")
    b.write_text("private.txt", "B's private data")
    result = a.write_text("reports/summary.csv", "name,total\n办公用品,1203\n")
    assert result["bytes"] == len("name,total\n办公用品,1203\n".encode())
    assert a.read_text("reports/summary.csv") == "name,total\n办公用品,1203\n"
    a.write_text("reports/summary.csv", "updated")
    assert a.read_text("reports/summary.csv") == "updated"
    for path in ("../user-b/private.txt", str(b.root / "private.txt"), r"..\user-b\private.txt", "reports/../../user-b/private.txt", "old/private.txt"):
        with pytest.raises(ToolError):
            a.read_text(path)
        with pytest.raises(ToolError):
            a.write_text(path, "overwrite")
    assert b.read_text("private.txt") == "B's private data"


def test_existing_and_later_symlinks_cannot_read_or_overwrite_other_user(tmp_path):
    a = WorkspaceFiles(tmp_path.resolve() / "user-a")
    b = WorkspaceFiles(tmp_path.resolve() / "user-b")
    b.write_text("private.txt", "B's private data")
    (a.root / "file-link").symlink_to(b.root / "private.txt")
    (a.root / "directory-link").symlink_to(b.root, target_is_directory=True)
    (a.root / "dangling-link").symlink_to(b.root / "not-created.txt")
    for path in ("file-link", "directory-link/private.txt", "dangling-link"):
        with pytest.raises(ToolError):
            a.read_text(path)
        with pytest.raises(ToolError):
            a.write_text(path, "overwrite")
    assert b.read_text("private.txt") == "B's private data"
    assert not (b.root / "not-created.txt").exists()
    alias = tmp_path / "root-link"
    alias.symlink_to(b.root, target_is_directory=True)
    with pytest.raises(ToolError):
        WorkspaceFiles(alias)


def test_special_files_and_oversized_payload_are_rejected_without_blocking(tmp_path):
    files = WorkspaceFiles(tmp_path.resolve() / "workspace")
    os.mkfifo(files.root / "pipe")
    with pytest.raises(ToolError, match='普通文件'):
        files.read_text("pipe")
    with pytest.raises(ToolError, match="2 MiB"):
        files.write_text("too-large", "x" * (2 * 1024 * 1024 + 1))
    assert not (files.root / "too-large").exists()


def test_calculation_uses_integer_cents_and_rejects_agent_code(tmp_path):
    result = calculate_totals([{"unit_price_cents": 101, "quantity": 3}, {"unit_price_cents": 29, "quantity": 4}])
    assert result == {"total_cents": 419, "quantity": 7, "row_count": 2}
    marker = tmp_path / "should-not-exist"
    with pytest.raises(ToolError):
        calculate_totals([{"unit_price_cents": f"__import__('pathlib').Path('{marker}').touch()", "quantity": 1}])
    assert not marker.exists()
    with pytest.raises(ToolError):
        calculate_totals([{"unit_price_cents": True, "quantity": 1}])
