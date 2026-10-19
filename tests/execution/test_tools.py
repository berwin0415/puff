from __future__ import annotations

import pytest

from execution.tools.basic import execute, safe_calculate, write_workspace_file


def test_safe_calculate_and_dispatch(tmp_path):
    assert safe_calculate("2 + 3 * 4") == 14
    assert execute("calculate", {"expression": "(9 - 3) / 2"}) == 3
    assert write_workspace_file(tmp_path, "nested/result.txt", "ok") == "nested/result.txt"
    assert (tmp_path / "nested/result.txt").read_text() == "ok"


def test_tools_reject_code_and_path_traversal(tmp_path):
    with pytest.raises(ValueError):
        safe_calculate("__import__('os').getcwd()")
    with pytest.raises(ValueError):
        write_workspace_file(tmp_path, "../escape.txt", "no")
