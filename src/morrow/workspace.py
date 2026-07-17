from __future__ import annotations

from pathlib import Path


def detect_workspace_root(cwd: Path | None = None) -> Path:
    """Return the nearest recognizable project root, falling back to the cwd."""

    start = (cwd or Path.cwd()).resolve()
    for candidate in (start, *start.parents):
        if _is_project_root(candidate):
            return candidate
    return start


def _is_project_root(path: Path) -> bool:
    if (path / ".git").exists() or (path / "pyproject.toml").is_file():
        return True

    manifest = path / "Cargo.toml"
    if not manifest.is_file():
        return False
    try:
        return any(line.strip() == "[workspace]" for line in manifest.read_text().splitlines())
    except (OSError, UnicodeError):
        return False
