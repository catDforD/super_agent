from pathlib import Path

import pytest

import morrow.workspace as workspace
from morrow.workspace import detect_workspace_root


def test_detects_nearest_python_project(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    nested = root / "src" / "pkg"
    nested.mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname='demo'\n")

    assert detect_workspace_root(nested) == root.resolve()


def test_falls_back_to_starting_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    nested = tmp_path / "plain" / "nested"
    nested.mkdir(parents=True)
    monkeypatch.setattr(workspace, "_is_project_root", lambda _path: False)

    assert detect_workspace_root(nested) == nested.resolve()
