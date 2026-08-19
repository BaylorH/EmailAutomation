"""Regression coverage for production access-token log redaction.

Converted from pytest to unittest on 2026-08-19. It was the only pytest module
among the 166 test modules here, and pytest is installed nowhere and declared in
neither requirements.txt nor requirements.lock, so `python -m unittest` failed at
`import pytest` and these assertions had never executed in any recorded sweep.
Installing pytest would not have fixed it either: unittest collects TestCase
subclasses, so bare parametrized functions would still have reported NO TESTS RAN.

The assertions are unchanged -- same surfaces, same AST walk, same predicates.
@pytest.mark.parametrize becomes subTest, which keeps per-entrypoint reporting so
a failure still names the offending file.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
TOKEN_ENTRYPOINTS = (REPO_ROOT / "main.py", REPO_ROOT / "scheduler_runner.py")
TOKEN_LOG_SURFACES = TOKEN_ENTRYPOINTS + (REPO_ROOT / "tests" / "e2e_helpers.py",)


class TokenLoggingRedactionTests(unittest.TestCase):
    def test_entrypoint_never_prints_access_token_material(self) -> None:
        """Neither the active worker nor the legacy runner may log token bytes."""

        for entrypoint in TOKEN_LOG_SURFACES:
            with self.subTest(entrypoint=entrypoint.name):
                source = entrypoint.read_text(encoding="utf-8")
                tree = ast.parse(source)

                for node in ast.walk(tree):
                    if not isinstance(node, ast.Call):
                        continue
                    if not isinstance(node.func, ast.Name) or node.func.id != "print":
                        continue

                    rendered = ast.get_source_segment(source, node) or ""
                    self.assertNotIn(
                        "access_token",
                        rendered,
                        f"{entrypoint.name} prints access-token material: {rendered}",
                    )

    def test_token_status_log_has_no_preview_label(self) -> None:
        """The safe status log may report source/expiry, never a token preview."""

        for entrypoint in TOKEN_ENTRYPOINTS:
            with self.subTest(entrypoint=entrypoint.name):
                source = entrypoint.read_text(encoding="utf-8")
                token_status_lines = [
                    line for line in source.splitlines() if "Using {token_source}" in line
                ]

                self.assertTrue(
                    token_status_lines,
                    f"expected a token status log in {entrypoint.name}",
                )
                self.assertTrue(
                    all("preview" not in line.lower() for line in token_status_lines)
                )


if __name__ == "__main__":
    unittest.main()
