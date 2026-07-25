#!/usr/bin/env python3
"""Run one explicitly selected EmailAutomation test level."""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
import os
import socket
import sys
import traceback
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Mapping, TextIO
from unittest.mock import MagicMock, patch


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY_PATH = (
    REPO_ROOT / "docs" / "release-safety" / "scenario-registry.json"
)
E2E_TEST_MODE_ENV = "E2E_TEST_MODE"
SENSITIVE_ENV_MARKERS = (
    "API_KEY",
    "BEARER",
    "CREDENTIAL",
    "PASSWORD",
    "PRIVATE_KEY",
    "SECRET",
    "TOKEN",
)
SENSITIVE_ENV_NAMES = {
    "AZURE_API_APP_ID",
    "CLIENT_ID",
    "FIREBASE_SERVICE_ACCOUNT_JSON",
    "FIRESTORE_EMULATOR_HOST",
    "GOOGLE_OAUTH_CLIENT_ID",
    "MICROSOFT_CLIENT_ID",
    "MS_CLIENT_ID",
}

EXIT_PASSED = 0
EXIT_FAILED = 1
EXIT_UNAVAILABLE = 3


@dataclass(frozen=True)
class LevelResult:
    level: str
    status: str
    tests_run: int = 0
    failures: int = 0
    errors: int = 0
    skipped: int = 0
    detail: str = ""

    @property
    def exit_code(self) -> int:
        if self.status == "passed":
            return EXIT_PASSED
        if self.status == "unavailable":
            return EXIT_UNAVAILABLE
        return EXIT_FAILED


class L1NetworkAccessBlocked(RuntimeError):
    pass


class _CredentialFreeSocket(socket.socket):
    def connect(self, address: object) -> None:
        raise L1NetworkAccessBlocked(f"L1 network access blocked: {address!r}")

    def connect_ex(self, address: object) -> int:
        raise L1NetworkAccessBlocked(f"L1 network access blocked: {address!r}")


def _block_l1_network(*args: object, **kwargs: object) -> None:
    raise L1NetworkAccessBlocked("L1 network access blocked")


def _is_sensitive_environment_name(name: str) -> bool:
    upper_name = name.upper()
    return upper_name in SENSITIVE_ENV_NAMES or any(
        marker in upper_name for marker in SENSITIVE_ENV_MARKERS
    )


@contextmanager
def credential_free_l1_environment() -> Iterator[MagicMock]:
    """Remove live credentials, clients, and network access from all L1 code."""

    removed_environment = {
        name: os.environ.pop(name)
        for name in list(os.environ)
        if _is_sensitive_environment_name(name)
    }
    e2e_mode_was_set = E2E_TEST_MODE_ENV in os.environ
    previous_e2e_mode = os.environ.get(E2E_TEST_MODE_ENV)
    os.environ[E2E_TEST_MODE_ENV] = "true"
    fake_client = MagicMock(name="credential_free_firestore_client")

    try:
        with patch("socket.socket", _CredentialFreeSocket), patch(
            "socket.create_connection",
            side_effect=_block_l1_network,
        ), patch(
            "socket.getaddrinfo",
            side_effect=_block_l1_network,
        ), patch(
            "google.cloud.firestore.Client",
            return_value=fake_client,
        ), patch(
            "firebase_admin.firestore.client",
            return_value=fake_client,
        ), patch(
            "firebase_admin.initialize_app",
            return_value=MagicMock(name="credential_free_firebase_app"),
        ), patch(
            "msal.PublicClientApplication",
            return_value=MagicMock(name="credential_free_public_msal_client"),
        ), patch(
            "msal.ConfidentialClientApplication",
            return_value=MagicMock(name="credential_free_confidential_msal_client"),
        ), patch(
            "openai.OpenAI",
            return_value=MagicMock(name="credential_free_openai_client"),
        ):
            yield fake_client
    finally:
        os.environ.update(removed_environment)
        if e2e_mode_was_set:
            os.environ[E2E_TEST_MODE_ENV] = previous_e2e_mode or ""
        else:
            os.environ.pop(E2E_TEST_MODE_ENV, None)


def _discover_l1_suite() -> unittest.TestSuite:
    loader = unittest.TestLoader()
    return loader.discover("tests", pattern="test*.py")


def run_l1(
    *,
    suite_factory: Callable[[], unittest.TestSuite] | None = None,
    output: TextIO | None = None,
) -> LevelResult:
    """Discover and run the credential-free suite under one bootstrap boundary."""

    output = output or sys.stdout
    suite_factory = suite_factory or _discover_l1_suite
    runner_details = io.StringIO()
    application_output = io.StringIO()
    previous_cwd = Path.cwd()

    try:
        os.chdir(REPO_ROOT)
        if str(REPO_ROOT) not in sys.path:
            sys.path.insert(0, str(REPO_ROOT))

        with credential_free_l1_environment():
            with redirect_stdout(application_output), redirect_stderr(
                application_output
            ):
                suite = suite_factory()
                unittest_result = unittest.TextTestRunner(
                    stream=runner_details,
                    verbosity=1,
                ).run(suite)
    except Exception as exc:
        detail = "".join(traceback.format_exception(exc))
        result = LevelResult(level="L1", status="failed", errors=1, detail=detail)
        print("L1 FAILED tests=0 failures=0 errors=1 skipped=0", file=output)
        print(detail.rstrip(), file=output)
        return result
    finally:
        os.chdir(previous_cwd)

    status = "passed" if unittest_result.wasSuccessful() else "failed"
    result = LevelResult(
        level="L1",
        status=status,
        tests_run=unittest_result.testsRun,
        failures=len(unittest_result.failures),
        errors=len(unittest_result.errors),
        skipped=len(unittest_result.skipped),
        detail=runner_details.getvalue(),
    )
    print(
        "L1 "
        f"{status.upper()} "
        f"tests={result.tests_run} "
        f"failures={result.failures} "
        f"errors={result.errors} "
        f"skipped={result.skipped}",
        file=output,
    )

    if status == "failed":
        print(runner_details.getvalue().rstrip(), file=output)

    return result


def _load_registry(registry_path: Path) -> Mapping[str, object]:
    return json.loads(registry_path.read_text(encoding="utf-8"))


def _missing_python_modules(module_names: list[str]) -> list[str]:
    missing = []
    for module_name in module_names:
        try:
            available = importlib.util.find_spec(module_name) is not None
        except (ImportError, ModuleNotFoundError, ValueError):
            available = False
        if not available:
            missing.append(module_name)
    return missing


def _unavailable(
    level: str,
    reason: str,
    *,
    output: TextIO,
) -> LevelResult:
    result = LevelResult(level=level, status="unavailable", detail=reason)
    print(f"{level} UNAVAILABLE: {reason}", file=output)
    return result


def run_level(
    level: str,
    *,
    registry_path: Path = DEFAULT_REGISTRY_PATH,
    output: TextIO | None = None,
) -> LevelResult:
    """Run L1 or explain exactly why a higher-level environment is unavailable."""

    output = output or sys.stdout
    normalized_level = level.upper()

    try:
        registry = _load_registry(registry_path)
        profile = registry["levels"][normalized_level]  # type: ignore[index]
        if normalized_level == "L1":
            missing_modules = _missing_python_modules(
                profile.get("requiredPythonModules", [])  # type: ignore[union-attr]
            )
            if missing_modules:
                return _unavailable(
                    normalized_level,
                    "missing required Python modules: " + ", ".join(missing_modules),
                    output=output,
                )
            return run_l1(output=output)

        required_environment = profile.get(  # type: ignore[union-attr]
            "requiredEnvironment",
            [],
        )
        missing_environment = [
            name for name in required_environment if not os.environ.get(name)
        ]
        if missing_environment:
            return _unavailable(
                normalized_level,
                "missing required environment: "
                + ", ".join(missing_environment),
                output=output,
            )

        reason = profile.get(  # type: ignore[union-attr]
            "unavailableReason",
            "No executable suite is registered for this level.",
        )
        return _unavailable(normalized_level, str(reason), output=output)
    except (
        AttributeError,
        KeyError,
        OSError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        result = LevelResult(
            level=normalized_level,
            status="failed",
            errors=1,
            detail=str(exc),
        )
        print(f"{normalized_level} FAILED configuration={exc}", file=output)
        return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one EmailAutomation test level.",
    )
    parser.add_argument(
        "--level",
        required=True,
        type=str.upper,
        choices=("L1", "L2", "L3", "L4"),
    )
    parser.add_argument(
        "--registry",
        type=Path,
        default=DEFAULT_REGISTRY_PATH,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return run_level(args.level, registry_path=args.registry).exit_code


if __name__ == "__main__":
    raise SystemExit(main())
