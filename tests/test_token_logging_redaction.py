"""Regression coverage for production access-token log redaction."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
TOKEN_ENTRYPOINTS = (REPO_ROOT / "main.py", REPO_ROOT / "scheduler_runner.py")
TOKEN_LOG_SURFACES = TOKEN_ENTRYPOINTS + (REPO_ROOT / "tests" / "e2e_helpers.py",)


@pytest.mark.parametrize("entrypoint", TOKEN_LOG_SURFACES, ids=lambda path: path.name)
def test_entrypoint_never_prints_access_token_material(entrypoint: Path) -> None:
    """Neither the active worker nor the legacy runner may log token bytes."""

    source = entrypoint.read_text(encoding="utf-8")
    tree = ast.parse(source)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Name) or node.func.id != "print":
            continue

        rendered = ast.get_source_segment(source, node) or ""
        assert "access_token" not in rendered, (
            f"{entrypoint.name} prints access-token material: {rendered}"
        )


@pytest.mark.parametrize("entrypoint", TOKEN_ENTRYPOINTS, ids=lambda path: path.name)
def test_token_status_log_has_no_preview_label(entrypoint: Path) -> None:
    """The safe status log may report source/expiry, never a token preview."""

    source = entrypoint.read_text(encoding="utf-8")
    token_status_lines = [
        line for line in source.splitlines() if "Using {token_source}" in line
    ]

    assert token_status_lines, f"expected a token status log in {entrypoint.name}"
    assert all("preview" not in line.lower() for line in token_status_lines)
