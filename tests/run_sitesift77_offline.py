#!/usr/bin/env python3
"""Run the SiteSift #77 verification modules with live effects disabled."""

from __future__ import annotations

import sys
from pathlib import Path


REPO_ROOT = str(Path(__file__).resolve().parents[1])


def _bootstrap_repo_root() -> None:
    while REPO_ROOT in sys.path:
        sys.path.remove(REPO_ROOT)
    sys.path.insert(0, REPO_ROOT)


_bootstrap_repo_root()

import importlib
import ipaddress
import os
import socket
import subprocess
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from typing import Callable, MutableMapping, TextIO


TEST_MODULE_ALLOWLIST = (
    "tests.test_campaign_capabilities",
    "tests.test_recovery_payload",
    "tests.test_recovery_config",
    "tests.test_recovery_auth",
    "tests.test_recovery_dispatch",
    "tests.test_recovery_service",
    "tests.test_inbound_automation_effect_closure",
    "tests.test_outbox_safety",
    "tests.test_action_audit_backend",
    "tests.test_outbox_reply_recipient_routing",
    "tests.test_campaign_automation_pause",
    "tests.test_followup_terminal_state",
    "tests.test_pending_responses",
    "tests.test_processing_completion_guards",
    "tests.test_processing_reply_safety",
    "tests.test_jill_june_regressions",
    "tests.test_scheduler_scope",
    "tests.test_scheduler_lease",
    "tests.test_scheduler_user_listing",
    "tests.test_graph_retry_policy",
    "tests.test_graph_send_inventory",
    "tests.test_outbound_kill_switch",
    "tests.test_dead_letter_recovery",
    "tests.test_resend_failed_responses",
    "tests.test_go_condition_send_failure_observability",
    "tests.test_process_user_service",
    "tests.test_full_campaign_e2e",
)


APPLICATION_CREDENTIAL_VARIABLES = frozenset(
    {
        "OPENAI_API_KEY",
        "OPENAI_ORG_ID",
        "OPENAI_ORGANIZATION",
        "OPENAI_PROJECT",
        "OPENAI_PROJECT_ID",
        "AZURE_API_APP_ID",
        "AZURE_API_CLIENT_SECRET",
        "AZURE_CLIENT_ID",
        "AZURE_CLIENT_SECRET",
        "AZURE_TENANT_ID",
        "CLIENT_ID",
        "CLIENT_SECRET",
        "TENANT_ID",
        "GRAPH_ACCESS_TOKEN",
        "MS_GRAPH_ACCESS_TOKEN",
        "FIREBASE_API_KEY",
        "FIREBASE_SA_KEY",
        "FIREBASE_SERVICE_ACCOUNT_JSON",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_API_KEY",
        "GOOGLE_OAUTH_CLIENT_ID",
        "GOOGLE_OAUTH_CLIENT_SECRET",
        "GOOGLE_REFRESH_TOKEN",
        "GMAIL_APP_PASSWORD",
    }
)


BLOCKED_OS_PROCESS_FUNCTIONS = (
    "system",
    "popen",
    "fork",
    "forkpty",
    "posix_spawn",
    "posix_spawnp",
    "execl",
    "execle",
    "execlp",
    "execlpe",
    "execv",
    "execve",
    "execvp",
    "execvpe",
    "spawnl",
    "spawnle",
    "spawnlp",
    "spawnlpe",
    "spawnv",
    "spawnve",
    "spawnvp",
    "spawnvpe",
)


_NETWORK_ROUTE_VARIABLES = (
    "ALL_PROXY",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "all_proxy",
    "https_proxy",
    "http_proxy",
)
_DEMO_PROJECT_ID = "demo-sitesift77"
_MAIN_EXECUTION_COUNT = 0


class OfflineNetworkAccessBlocked(RuntimeError):
    """Raised without target details when the offline boundary is crossed."""


@dataclass(frozen=True)
class ImportFailure:
    module_name: str
    error_type: str


def sanitize_environment(
    environ: MutableMapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Remove application credentials and force the synthetic offline runtime."""

    target = os.environ if environ is None else environ
    removed = []
    for name in sorted(APPLICATION_CREDENTIAL_VARIABLES):
        if name in target:
            removed.append(name)
            target.pop(name, None)
    for name in _NETWORK_ROUTE_VARIABLES:
        target.pop(name, None)

    target.update(
        {
            "SITESIFT_OUTBOUND_MODE": "paused",
            "SITESIFT_RECOVERY_MODE": "disabled",
            "E2E_TEST_MODE": "true",
            "GOOGLE_CLOUD_PROJECT": _DEMO_PROJECT_ID,
            "GCLOUD_PROJECT": _DEMO_PROJECT_ID,
            "FIREBASE_PROJECT_ID": _DEMO_PROJECT_ID,
            "FIREBASE_CONFIG": f'{{"projectId":"{_DEMO_PROJECT_ID}"}}',
            "FIRESTORE_EMULATOR_HOST": "127.0.0.1:8080",
            "FIREBASE_AUTH_EMULATOR_HOST": "127.0.0.1:9099",
            "FIREBASE_STORAGE_EMULATOR_HOST": "127.0.0.1:9199",
            "NO_PROXY": "localhost,127.0.0.1,::1",
            "no_proxy": "localhost,127.0.0.1,::1",
        }
    )
    return tuple(removed)


def is_loopback_host(host: object) -> bool:
    """Return true only for localhost or a loopback IP literal."""

    if isinstance(host, bytes):
        try:
            host = host.decode("ascii")
        except UnicodeDecodeError:
            return False
    if not isinstance(host, str):
        return False
    candidate = host.strip()
    if not candidate:
        return False
    if candidate.rstrip(".").lower() == "localhost":
        return True
    if "%" in candidate:
        candidate = candidate.split("%", 1)[0]
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        return False
    if address.is_loopback:
        return True
    mapped = getattr(address, "ipv4_mapped", None)
    return bool(mapped and mapped.is_loopback)


def _canonical_loopback_host(host: object, family: int = 0) -> str:
    if isinstance(host, bytes):
        host = host.decode("ascii")
    candidate = str(host).strip()
    if candidate.rstrip(".").lower() == "localhost":
        return "::1" if family == socket.AF_INET6 else "127.0.0.1"
    return candidate


class NetworkTripwire:
    """Deny non-loopback network and child-process escape routes."""

    def __init__(self) -> None:
        self.activation_count = 0
        self._restorations: list[tuple[object, str, object]] = []
        self._installed = False

    def summary(self) -> dict[str, int]:
        return {"activation_count": self.activation_count}

    def _activate(self, *_args, **_kwargs):
        self.activation_count += 1
        raise OfflineNetworkAccessBlocked("offline effect tripwire activated")

    @staticmethod
    def _allowed_socket_address(sock: socket.socket, address: object) -> bool:
        if sock.family == socket.AF_UNIX:
            return isinstance(address, (str, bytes))
        if sock.family not in (socket.AF_INET, socket.AF_INET6):
            return False
        if not isinstance(address, tuple) or not address:
            return False
        return is_loopback_host(address[0])

    def _replace(self, owner: object, name: str, replacement: object) -> None:
        original = getattr(owner, name)
        self._restorations.append((owner, name, original))
        setattr(owner, name, replacement)

    def _install_socket_guards(self) -> None:
        original_connect = socket.socket.connect
        original_connect_ex = socket.socket.connect_ex
        original_bind = socket.socket.bind
        original_sendto = socket.socket.sendto
        original_sendmsg = getattr(socket.socket, "sendmsg", None)
        original_getpeername = socket.socket.getpeername
        original_create_connection = socket.create_connection
        original_getaddrinfo = socket.getaddrinfo
        original_gethostbyname = socket.gethostbyname
        original_gethostbyname_ex = socket.gethostbyname_ex
        original_getnameinfo = socket.getnameinfo

        def guarded_connect(sock, address):
            if not self._allowed_socket_address(sock, address):
                return self._activate()
            return original_connect(sock, address)

        def guarded_connect_ex(sock, address):
            if not self._allowed_socket_address(sock, address):
                return self._activate()
            return original_connect_ex(sock, address)

        def guarded_bind(sock, address):
            if not self._allowed_socket_address(sock, address):
                return self._activate()
            return original_bind(sock, address)

        def guarded_sendto(sock, data, *args):
            if not args:
                return self._activate()
            address = args[-1]
            if not self._allowed_socket_address(sock, address):
                return self._activate()
            return original_sendto(sock, data, *args)

        def guarded_sendmsg(sock, buffers, *args):
            address = args[2] if len(args) >= 3 else None
            if address is not None:
                if not self._allowed_socket_address(sock, address):
                    return self._activate()
            elif sock.family in (socket.AF_INET, socket.AF_INET6):
                try:
                    peer = original_getpeername(sock)
                except OSError:
                    # An unconnected socket cannot send without a destination.
                    # Preserve that local error from the original implementation.
                    pass
                else:
                    if not self._allowed_socket_address(sock, peer):
                        return self._activate()
            elif sock.family != socket.AF_UNIX:
                return self._activate()
            return original_sendmsg(sock, buffers, *args)

        def guarded_create_connection(address, *args, **kwargs):
            if not isinstance(address, tuple) or not address:
                return self._activate()
            if not is_loopback_host(address[0]):
                return self._activate()
            source_address = kwargs.get("source_address")
            if source_address is None and len(args) >= 2:
                source_address = args[1]
            if source_address is not None:
                if not isinstance(source_address, tuple) or not source_address:
                    return self._activate()
                if not is_loopback_host(source_address[0]):
                    return self._activate()
            return original_create_connection(address, *args, **kwargs)

        def guarded_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
            if host is None:
                return original_getaddrinfo(host, port, family, type, proto, flags)
            if not is_loopback_host(host):
                return self._activate()
            safe_host = _canonical_loopback_host(host, family)
            numeric_flags = flags | getattr(socket, "AI_NUMERICHOST", 0)
            return original_getaddrinfo(
                safe_host, port, family, type, proto, numeric_flags
            )

        def guarded_gethostbyname(host):
            if not is_loopback_host(host):
                return self._activate()
            return original_gethostbyname(_canonical_loopback_host(host))

        def guarded_gethostbyname_ex(host):
            if not is_loopback_host(host):
                return self._activate()
            return original_gethostbyname_ex(_canonical_loopback_host(host))

        def guarded_gethostbyaddr(host):
            if not is_loopback_host(host):
                return self._activate()
            safe_host = _canonical_loopback_host(host)
            return ("localhost", [], [safe_host])

        def guarded_getnameinfo(sockaddr, flags):
            if not isinstance(sockaddr, tuple) or not sockaddr:
                return self._activate()
            if not is_loopback_host(sockaddr[0]):
                return self._activate()
            numeric_flags = (
                flags
                | getattr(socket, "NI_NUMERICHOST", 0)
                | getattr(socket, "NI_NUMERICSERV", 0)
            )
            return original_getnameinfo(sockaddr, numeric_flags)

        self._replace(socket.socket, "connect", guarded_connect)
        self._replace(socket.socket, "connect_ex", guarded_connect_ex)
        self._replace(socket.socket, "bind", guarded_bind)
        self._replace(socket.socket, "sendto", guarded_sendto)
        if original_sendmsg is not None:
            self._replace(socket.socket, "sendmsg", guarded_sendmsg)
        self._replace(socket, "create_connection", guarded_create_connection)
        self._replace(socket, "getaddrinfo", guarded_getaddrinfo)
        self._replace(socket, "gethostbyname", guarded_gethostbyname)
        self._replace(socket, "gethostbyname_ex", guarded_gethostbyname_ex)
        self._replace(socket, "gethostbyaddr", guarded_gethostbyaddr)
        self._replace(socket, "getnameinfo", guarded_getnameinfo)

    def _install_process_guards(self) -> None:
        self._replace(subprocess, "Popen", self._activate)
        for name in BLOCKED_OS_PROCESS_FUNCTIONS:
            if hasattr(os, name):
                self._replace(os, name, self._activate)

    def __enter__(self) -> "NetworkTripwire":
        if self._installed:
            raise RuntimeError("offline effect tripwire is already installed")
        self._installed = True
        try:
            self._install_socket_guards()
            self._install_process_guards()
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        for owner, name, original in reversed(self._restorations):
            setattr(owner, name, original)
        self._restorations.clear()
        self._installed = False


def build_allowlisted_suite(
    *,
    import_module: Callable[[str], object] = importlib.import_module,
    loader: unittest.TestLoader = unittest.defaultTestLoader,
) -> tuple[unittest.TestSuite, list[ImportFailure]]:
    """Import exactly the literal modules and reject empty module suites."""

    aggregate = unittest.TestSuite()
    failures = []
    for module_name in TEST_MODULE_ALLOWLIST:
        try:
            module = import_module(module_name)
            module_suite = loader.loadTestsFromModule(module)
            if module_suite.countTestCases() < 1:
                failures.append(ImportFailure(module_name, "EmptyTestModule"))
                continue
            aggregate.addTest(module_suite)
        except KeyboardInterrupt:
            raise
        except BaseException as error:
            failures.append(ImportFailure(module_name, type(error).__name__))
    return aggregate, failures


def exit_status(
    result: unittest.TestResult,
    *,
    import_failures: tuple[ImportFailure, ...] | list[ImportFailure],
    tripwire_activation_count: int,
) -> int:
    """Return zero only for a nonempty, entirely ordinary PASS."""

    failed = any(
        (
            result.testsRun < 1,
            bool(result.failures),
            bool(result.errors),
            bool(getattr(result, "skipped", ())),
            bool(getattr(result, "expectedFailures", ())),
            bool(getattr(result, "unexpectedSuccesses", ())),
            bool(import_failures),
            tripwire_activation_count != 0,
        )
    )
    return 1 if failed else 0


def _safe_count(result: unittest.TestResult, name: str) -> int:
    return len(getattr(result, name, ()))


class _DiscardingTextStream:
    """Keep unittest tracebacks, endpoints, and fixture secrets out of diagnostics."""

    def write(self, value: str) -> int:
        return len(value)

    def flush(self) -> None:
        return None

    def isatty(self) -> bool:
        return False


def run_offline_tests(
    *,
    environ: MutableMapping[str, str] | None = None,
    import_module: Callable[[str], object] = importlib.import_module,
    loader: unittest.TestLoader = unittest.defaultTestLoader,
    stream: TextIO | None = None,
) -> int:
    """Sanitize, guard, import, run, and return a fail-closed exit status."""

    output = sys.stdout if stream is None else stream
    sanitize_environment(environ)
    tripwire = NetworkTripwire()
    import_failures: list[ImportFailure] = []
    internal_errors = 0
    result = unittest.TestResult()
    discarded = _DiscardingTextStream()

    try:
        with tripwire, redirect_stdout(discarded), redirect_stderr(discarded):
            suite, import_failures = build_allowlisted_suite(
                import_module=import_module,
                loader=loader,
            )
            if not import_failures:
                result = unittest.TextTestRunner(
                    stream=discarded,
                    verbosity=0,
                ).run(suite)
    except KeyboardInterrupt:
        raise
    except BaseException:
        internal_errors = 1

    status = exit_status(
        result,
        import_failures=import_failures,
        tripwire_activation_count=tripwire.activation_count,
    )
    if internal_errors:
        status = 1
    label = "PASS" if status == 0 else "FAIL"
    output.write(
        "SiteSift77 offline result: "
        f"status={label} "
        f"tests={result.testsRun} "
        f"failures={len(result.failures)} "
        f"errors={len(result.errors)} "
        f"skipped={_safe_count(result, 'skipped')} "
        f"expected_failures={_safe_count(result, 'expectedFailures')} "
        f"unexpected_successes={_safe_count(result, 'unexpectedSuccesses')} "
        f"import_errors={len(import_failures)} "
        f"internal_errors={internal_errors} "
        f"network_tripwire_activations={tripwire.activation_count}\n"
    )
    return status


def suite_execution_count() -> int:
    """Expose whether the command entry point ran; importing leaves this at zero."""

    return _MAIN_EXECUTION_COUNT


def main() -> int:
    global _MAIN_EXECUTION_COUNT
    _MAIN_EXECUTION_COUNT += 1
    return run_offline_tests()


if __name__ == "__main__":
    raise SystemExit(main())
