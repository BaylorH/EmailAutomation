"""Credential-free adapter for the email-admin-ui SEC-01 emulator suite."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import tarfile
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence


REQUIRED_REPOSITORY_FILES = (
    "package.json",
    "firebase.json",
    "firestore.rules",
    "firestore.indexes.json",
    "tests/firestore-rules/firestore.rules.test.js",
)
SNAPSHOT_SOURCE_PATHS = REQUIRED_REPOSITORY_FILES
REQUIRED_COMMANDS = ("node", "npm", "git")
SAFE_COMMAND_SEARCH_DIRS = (
    Path("/opt/homebrew/bin"),
    Path("/usr/local/bin"),
    Path("/usr/bin"),
    Path("/bin"),
    Path("/usr/sbin"),
    Path("/sbin"),
)
ESSENTIAL_SYSTEM_DIRS = (
    Path("/usr/bin"),
    Path("/bin"),
    Path("/usr/sbin"),
    Path("/sbin"),
)
JAVA_FALLBACK_DIRS = (
    Path("/opt/homebrew/opt/openjdk/bin"),
    Path("/usr/local/opt/openjdk/bin"),
)
PROBE_TIMEOUT_SECONDS = 10
GIT_TIMEOUT_SECONDS = 10
EMULATOR_TIMEOUT_SECONDS = 600
TERMINATE_GRACE_SECONDS = 5
NORMAL_EXIT_QUIESCENCE_SECONDS = 2
NORMAL_EXIT_MAX_SETTLE_SECONDS = 5
PROCESS_GROUP_POLL_INTERVAL_SECONDS = 0.05
LIFECYCLE_QUIESCENCE_SECONDS = 2
LIFECYCLE_MAX_SETTLE_SECONDS = 5
LIFECYCLE_POLL_INTERVAL_SECONDS = 0.05
EMULATOR_PORTS = (8080, 9150)
PROCESS_INSPECTOR_PATH = Path("/bin/ps")
PORT_INSPECTOR_PATH = Path("/usr/sbin/lsof")
DEMO_PROJECT_ID = "demo-sitesift-sec-l2"
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
BLOCKED_FIREBASE_ENV_NAMES = {
    "CLOUDSDK_CORE_PROJECT",
    "FIREBASE_CONFIG",
    "FIREBASE_PROJECT",
    "FIRESTORE_EMULATOR_HOST",
    "GCLOUD_PROJECT",
    "GOOGLE_CLOUD_PROJECT",
}
TAP_COUNTS = (
    "tests",
    "pass",
    "fail",
    "cancelled",
    "skipped",
    "todo",
)


@dataclass(frozen=True)
class SecL2Result:
    status: str
    tests_run: int = 0
    failures: int = 0
    errors: int = 0
    skipped: int = 0
    duration_ms: int = 0
    admin_ui_commit: str = ""
    detail: str = ""


@dataclass(frozen=True)
class _EmulatorProcessResult:
    returncode: int = 1
    stdout: str = ""
    timed_out: bool = False
    start_failed: bool = False
    cleanup_failed: bool = False
    descendants_remained: bool = False


@dataclass(frozen=True)
class _ObservedEmulatorProcess:
    pid: int
    process_group_id: int
    command: str


@dataclass(frozen=True)
class _LifecycleResult:
    violation: bool = False
    cleanup_failed: bool = False


def _safe_which(name: str) -> str | None:
    search_path = os.pathsep.join(str(path) for path in SAFE_COMMAND_SEARCH_DIRS)
    return shutil.which(name, path=search_path)


def _resolve_command(
    name: str,
    which: Callable[[str], str | None],
) -> Path | None:
    command = which(name)
    if not command:
        return None
    path = Path(command).expanduser()
    if not path.is_absolute():
        return None
    return path.resolve()


def _validate_package_script(package_path: Path) -> bool:
    try:
        package = json.loads(package_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeError):
        return False
    if not isinstance(package, dict):
        return False

    scripts = package.get("scripts")
    if not isinstance(scripts, dict):
        return False
    script = scripts.get("test:firestore-rules")
    if not isinstance(script, str) or not script.strip():
        return False

    try:
        tokens = shlex.split(script)
    except ValueError:
        return False
    if len(tokens) != 7 or tokens[:2] != ["firebase", "emulators:exec"]:
        return False
    if any(token in {"&&", "||", ";", "|", ">", "<"} for token in tokens):
        return False
    if tokens[2:5] != ["--only", "firestore", "--project"]:
        return False
    if re.fullmatch(r"demo-[a-z0-9][a-z0-9-]*", tokens[5]) is None:
        return False

    try:
        test_command = shlex.split(tokens[6])
    except ValueError:
        return False
    return test_command == [
        "node",
        "--test",
        "--test-reporter=tap",
        "tests/firestore-rules/firestore.rules.test.js",
    ]


def _java_candidates(
    which: Callable[[str], str | None],
    fallback_dirs: Sequence[Path],
) -> list[Path]:
    candidates: list[Path] = []
    primary = _resolve_command("java", which)
    if primary is not None:
        candidates.append(primary)
    for fallback_dir in fallback_dirs:
        fallback = (Path(fallback_dir) / "java").expanduser()
        if fallback.is_file():
            resolved = fallback.resolve()
            if resolved not in candidates:
                candidates.append(resolved)
    return candidates


def _controlled_environment(
    runtime_paths: Sequence[Path],
    sandbox_root: Path,
) -> dict[str, str]:
    home = sandbox_root / "home"
    npm_cache = sandbox_root / "npm-cache"
    temporary = sandbox_root / "tmp"
    for directory in (home, npm_cache, temporary):
        directory.mkdir(parents=True, exist_ok=True)

    path_dirs: list[Path] = []
    for directory in (
        *(runtime_path.parent for runtime_path in runtime_paths),
        *ESSENTIAL_SYSTEM_DIRS,
    ):
        resolved = directory.resolve()
        if resolved not in path_dirs:
            path_dirs.append(resolved)

    return {
        "CI": "true",
        "HOME": str(home),
        "NPM_CONFIG_CACHE": str(npm_cache),
        "PATH": os.pathsep.join(str(path) for path in path_dirs),
        "TMPDIR": str(temporary),
    }


def _parse_tap_counts(output: object) -> dict[str, int] | None:
    if not isinstance(output, str):
        return None

    lines = output.splitlines()
    if any(
        re.fullmatch(
            r"Bail out!(?:[ \t].*)?",
            line,
            flags=re.IGNORECASE,
        )
        for line in lines
    ):
        return None
    if any(
        re.match(r"(?:not ok|ok)[ \t]+\d+(?:[ \t]|$)", line)
        and re.search(
            r"[ \t]#[ \t]*(?:skip|todo)(?:[ \t]|$)",
            line,
            flags=re.IGNORECASE,
        )
        for line in lines
    ):
        return None

    plans = re.findall(r"^1\.\.(\d+)\s*$", output, flags=re.MULTILINE)
    if len(plans) != 1:
        return None

    counts: dict[str, int] = {}
    for label in TAP_COUNTS:
        matches = re.findall(
            rf"^# {re.escape(label)} (\d+)\s*$",
            output,
            flags=re.MULTILINE,
        )
        if len(matches) != 1:
            return None
        counts[label] = int(matches[0])

    if counts["tests"] != int(plans[0]):
        return None

    plan = int(plans[0])
    assertions = re.findall(
        r"^(not ok|ok)\s+(\d+)(?:\s+-.*)?$",
        output,
        flags=re.MULTILINE,
    )
    assertion_numbers = [int(number) for _status, number in assertions]
    if len(assertions) != plan:
        return None
    if assertion_numbers != list(range(1, plan + 1)):
        return None
    if any(status != "ok" for status, _number in assertions):
        return None
    if (
        counts["pass"] != plan
        or counts["fail"] != 0
        or counts["cancelled"] != 0
        or counts["skipped"] != 0
        or counts["todo"] != 0
    ):
        return None
    return counts


def _tap_is_completely_passing(counts: dict[str, int]) -> bool:
    return (
        counts["tests"] > 0
        and counts["pass"] == counts["tests"]
        and counts["fail"] == 0
        and counts["cancelled"] == 0
        and counts["skipped"] == 0
        and counts["todo"] == 0
    )


def _run(
    runner: Callable[..., subprocess.CompletedProcess[str]],
    command: Sequence[str],
    *,
    timeout: int,
    **kwargs: object,
) -> subprocess.CompletedProcess[str]:
    return runner(
        list(command),
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        **kwargs,
    )


def _runtime_is_available(
    runner: Callable[..., subprocess.CompletedProcess[str]],
    command: Sequence[str],
    *,
    environment: dict[str, str],
) -> bool:
    try:
        process = _run(
            runner,
            command,
            timeout=PROBE_TIMEOUT_SECONDS,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return process.returncode == 0


def _run_git(
    runner: Callable[..., subprocess.CompletedProcess[str]],
    git_path: Path,
    arguments: Sequence[str],
    *,
    root: Path,
    environment: dict[str, str],
) -> subprocess.CompletedProcess[str] | None:
    try:
        return _run(
            runner,
            [str(git_path), *arguments],
            timeout=GIT_TIMEOUT_SECONDS,
            cwd=root,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _stdout(process: subprocess.CompletedProcess[str]) -> str | None:
    return process.stdout if isinstance(process.stdout, str) else None


def _extract_git_archive(
    archive_path: Path,
    snapshot_root: Path,
) -> bool:
    seen_paths: set[Path] = set()
    try:
        snapshot_root.mkdir(mode=0o700)
        with tarfile.open(archive_path, mode="r:") as archive:
            for member in archive:
                member_path = Path(member.name)
                if (
                    member_path.is_absolute()
                    or ".." in member_path.parts
                    or member_path in seen_paths
                ):
                    return False
                seen_paths.add(member_path)
                destination = snapshot_root / member_path
                if member.isdir():
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                if not member.isfile():
                    return False

                source = archive.extractfile(member)
                if source is None:
                    return False
                destination.parent.mkdir(parents=True, exist_ok=True)
                with source, destination.open("wb") as output:
                    shutil.copyfileobj(source, output)
                destination.chmod(
                    0o555 if member.mode & 0o111 else 0o444
                )
    except (OSError, tarfile.TarError):
        return False
    return True


def _materialize_committed_snapshot(
    runner: Callable[..., subprocess.CompletedProcess[str]],
    git_path: Path,
    *,
    root: Path,
    environment: dict[str, str],
    commit: str,
    sandbox_root: Path,
    node_modules: Path,
) -> Path | None:
    archive_path = sandbox_root / "committed-source.tar"
    snapshot_root = sandbox_root / "source"
    archive_process = _run_git(
        runner,
        git_path,
        [
            "archive",
            "--format=tar",
            f"--output={archive_path}",
            commit,
            "--",
            *SNAPSHOT_SOURCE_PATHS,
        ],
        root=root,
        environment=environment,
    )
    if archive_process is None or archive_process.returncode != 0:
        return None
    if not archive_path.is_file():
        return None
    if not _extract_git_archive(archive_path, snapshot_root):
        return None
    try:
        archive_path.unlink()
    except OSError:
        return None

    if any(
        not (snapshot_root / relative_path).is_file()
        for relative_path in SNAPSHOT_SOURCE_PATHS
    ):
        return None

    dependency_link = snapshot_root / "node_modules"
    if dependency_link.exists() or dependency_link.is_symlink():
        return None
    try:
        dependency_link.symlink_to(node_modules, target_is_directory=True)
        directories = sorted(
            (
                path
                for path in snapshot_root.rglob("*")
                if path.is_dir() and not path.is_symlink()
            ),
            key=lambda path: len(path.parts),
            reverse=True,
        )
        for directory in directories:
            directory.chmod(0o555)
        snapshot_root.chmod(0o700)
    except OSError:
        return None
    return snapshot_root


def _signal_process_group(
    process: subprocess.Popen[str],
    signal_number: int,
) -> bool:
    try:
        os.killpg(process.pid, signal_number)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return True


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _command_has_option(command: str, name: str, value: str) -> bool:
    return (
        re.search(
            rf"(?:^|\s){re.escape(name)}(?:=|\s+)"
            rf"{re.escape(value)}(?:\s|$)",
            command,
        )
        is not None
    )


def _is_demo_firestore_emulator(command: str) -> bool:
    return (
        re.search(
            r"(?:^|\s)\S*cloud-firestore-emulator-v[^\s/]*\.jar(?:\s|$)",
            command,
        )
        is not None
        and _command_has_option(command, "--port", "8080")
        and _command_has_option(command, "--websocket_port", "9150")
        and _command_has_option(command, "--project_id", DEMO_PROJECT_ID)
    )


def _capture_emulator_processes(
    runner: Callable[..., subprocess.CompletedProcess[str]],
    *,
    environment: dict[str, str],
) -> list[_ObservedEmulatorProcess] | None:
    try:
        process = _run(
            runner,
            [
                str(PROCESS_INSPECTOR_PATH),
                "-axo",
                "pid=,pgid=,command=",
            ],
            timeout=PROBE_TIMEOUT_SECONDS,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    output = _stdout(process)
    if process.returncode != 0 or output is None:
        return None

    observed: list[_ObservedEmulatorProcess] = []
    for line in output.splitlines():
        fields = line.strip().split(None, 2)
        if len(fields) != 3:
            continue
        try:
            pid = int(fields[0])
            process_group_id = int(fields[1])
        except ValueError:
            continue
        command = fields[2]
        if _is_demo_firestore_emulator(command):
            observed.append(
                _ObservedEmulatorProcess(
                    pid=pid,
                    process_group_id=process_group_id,
                    command=command,
                )
            )
    return observed


def _emulator_ports_are_free() -> bool:
    for port in EMULATOR_PORTS:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.setsockopt(
                    socket.SOL_SOCKET,
                    socket.SO_REUSEADDR,
                    1,
                )
                probe.bind(("127.0.0.1", port))
            except OSError:
                return False
    return True


def _emulator_ports_are_closed(
    runner: Callable[..., subprocess.CompletedProcess[str]],
    *,
    environment: dict[str, str],
) -> bool | None:
    for port in EMULATOR_PORTS:
        try:
            process = _run(
                runner,
                [
                    str(PORT_INSPECTOR_PATH),
                    "-nP",
                    "-a",
                    f"-iTCP:{port}",
                    "-sTCP:LISTEN",
                ],
                timeout=PROBE_TIMEOUT_SECONDS,
                env=environment,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        output = _stdout(process)
        if output is None:
            return None
        if process.returncode == 0 and output.strip():
            return False
        if process.returncode != 1 or output.strip():
            return None
    return True


def _signal_observed_processes(
    processes: Sequence[_ObservedEmulatorProcess],
    signal_number: int,
) -> bool:
    signaled_groups: set[int] = set()
    current_group = os.getpgrp()
    succeeded = True
    for process in processes:
        if (
            process.process_group_id > 1
            and process.process_group_id != current_group
            and process.process_group_id not in signaled_groups
        ):
            try:
                os.killpg(process.process_group_id, signal_number)
            except ProcessLookupError:
                pass
            except OSError:
                succeeded = False
            signaled_groups.add(process.process_group_id)
        else:
            try:
                os.kill(process.pid, signal_number)
            except ProcessLookupError:
                pass
            except OSError:
                succeeded = False
    return succeeded


def _reap_observed_processes(
    processes: Sequence[_ObservedEmulatorProcess],
) -> None:
    for process in processes:
        try:
            os.waitpid(process.pid, os.WNOHANG)
        except (ChildProcessError, ProcessLookupError):
            pass


def _new_emulator_processes(
    processes: Sequence[_ObservedEmulatorProcess],
    baseline_pids: frozenset[int],
) -> list[_ObservedEmulatorProcess]:
    return [
        process
        for process in processes
        if process.pid not in baseline_pids
    ]


def _terminate_new_emulator_processes(
    runner: Callable[..., subprocess.CompletedProcess[str]],
    processes: Sequence[_ObservedEmulatorProcess],
    *,
    environment: dict[str, str],
    baseline_pids: frozenset[int],
    grace_seconds: int | float,
) -> bool:
    cleanup_failed = not _signal_observed_processes(
        processes,
        signal.SIGTERM,
    )
    target_groups = {
        process.process_group_id
        for process in processes
        if process.process_group_id > 1
        and process.process_group_id != os.getpgrp()
    }
    terminate_deadline = time.monotonic() + grace_seconds
    remaining: list[_ObservedEmulatorProcess] | None = list(processes)
    while time.monotonic() < terminate_deadline:
        _reap_observed_processes(processes)
        observed = _capture_emulator_processes(
            runner,
            environment=environment,
        )
        if observed is None:
            return True
        remaining = _new_emulator_processes(observed, baseline_pids)
        groups_remain = any(
            _process_group_exists(process_group_id)
            for process_group_id in target_groups
        )
        if not remaining and not groups_remain:
            return cleanup_failed
        time.sleep(PROCESS_GROUP_POLL_INTERVAL_SECONDS)

    if remaining and not _signal_observed_processes(
        remaining,
        signal.SIGKILL,
    ):
        cleanup_failed = True
    for process_group_id in target_groups:
        if _process_group_exists(process_group_id):
            try:
                os.killpg(process_group_id, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError:
                cleanup_failed = True

    kill_deadline = time.monotonic() + grace_seconds
    while time.monotonic() < kill_deadline:
        _reap_observed_processes(processes)
        observed = _capture_emulator_processes(
            runner,
            environment=environment,
        )
        if observed is None:
            return True
        remaining = _new_emulator_processes(observed, baseline_pids)
        groups_remain = any(
            _process_group_exists(process_group_id)
            for process_group_id in target_groups
        )
        if not remaining and not groups_remain:
            return cleanup_failed
        time.sleep(PROCESS_GROUP_POLL_INTERVAL_SECONDS)
    return True


def _verify_emulator_lifecycle(
    runner: Callable[..., subprocess.CompletedProcess[str]],
    *,
    environment: dict[str, str],
    baseline_pids: frozenset[int],
    quiescence_seconds: int | float,
    max_settle_seconds: int | float,
    poll_interval_seconds: int | float,
    terminate_grace_seconds: int | float,
) -> _LifecycleResult:
    settle_deadline = time.monotonic() + max_settle_seconds
    quiet_since: float | None = None
    violation = False
    cleanup_failed = False

    while True:
        observed = _capture_emulator_processes(
            runner,
            environment=environment,
        )
        if observed is None:
            return _LifecycleResult(
                violation=True,
                cleanup_failed=True,
            )
        new_processes = _new_emulator_processes(
            observed,
            baseline_pids,
        )
        ports_are_closed = _emulator_ports_are_closed(
            runner,
            environment=environment,
        )
        if ports_are_closed is None:
            return _LifecycleResult(
                violation=True,
                cleanup_failed=True,
            )

        if new_processes:
            violation = True
            if _terminate_new_emulator_processes(
                runner,
                new_processes,
                environment=environment,
                baseline_pids=baseline_pids,
                grace_seconds=terminate_grace_seconds,
            ):
                cleanup_failed = True
            quiet_since = None
        elif not ports_are_closed:
            violation = True
            quiet_since = None
        else:
            now = time.monotonic()
            if quiet_since is None:
                quiet_since = now
            if now - quiet_since >= quiescence_seconds:
                return _LifecycleResult(
                    violation=violation,
                    cleanup_failed=cleanup_failed,
                )

        now = time.monotonic()
        if now >= settle_deadline:
            final_observed = _capture_emulator_processes(
                runner,
                environment=environment,
            )
            if final_observed is None:
                return _LifecycleResult(
                    violation=True,
                    cleanup_failed=True,
                )
            final_new_processes = _new_emulator_processes(
                final_observed,
                baseline_pids,
            )
            if final_new_processes and _terminate_new_emulator_processes(
                runner,
                final_new_processes,
                environment=environment,
                baseline_pids=baseline_pids,
                grace_seconds=terminate_grace_seconds,
            ):
                cleanup_failed = True
            final_observed = _capture_emulator_processes(
                runner,
                environment=environment,
            )
            return _LifecycleResult(
                violation=violation or bool(final_new_processes),
                cleanup_failed=(
                    cleanup_failed
                    or final_observed is None
                    or bool(
                        _new_emulator_processes(
                            final_observed or [],
                            baseline_pids,
                        )
                    )
                    or _emulator_ports_are_closed(
                        runner,
                        environment=environment,
                    )
                    is not True
                ),
            )
        time.sleep(
            min(
                poll_interval_seconds,
                max(0.001, settle_deadline - now),
            )
        )


def _wait_for_process_group_exit(
    process_group_id: int,
    *,
    deadline: float,
) -> bool:
    while (
        _process_group_exists(process_group_id)
        and time.monotonic() < deadline
    ):
        time.sleep(0.05)
    return not _process_group_exists(process_group_id)


def _terminate_process_group(
    process: subprocess.Popen[str],
    *,
    grace_seconds: int | float,
) -> bool:
    cleanup_failed = False
    terminate_deadline = time.monotonic() + grace_seconds
    if not _signal_process_group(process, signal.SIGTERM):
        cleanup_failed = True

    try:
        process.communicate(
            timeout=max(0.001, terminate_deadline - time.monotonic())
        )
    except subprocess.TimeoutExpired:
        pass
    except OSError:
        cleanup_failed = True

    group_exited = _wait_for_process_group_exit(
        process.pid,
        deadline=terminate_deadline,
    )
    if not group_exited:
        if not _signal_process_group(process, signal.SIGKILL):
            cleanup_failed = True
        kill_deadline = time.monotonic() + grace_seconds
        try:
            process.communicate(
                timeout=max(0.001, kill_deadline - time.monotonic())
            )
        except (OSError, subprocess.TimeoutExpired):
            cleanup_failed = True
        if not _wait_for_process_group_exit(
            process.pid,
            deadline=kill_deadline,
        ):
            cleanup_failed = True

    if process.poll() is None:
        try:
            process.communicate(timeout=grace_seconds)
        except (OSError, subprocess.TimeoutExpired):
            cleanup_failed = True

    return (
        cleanup_failed
        or process.poll() is None
        or _process_group_exists(process.pid)
    )


def _settle_normal_exit_process_group(
    process: subprocess.Popen[str],
    *,
    quiescence_seconds: int | float,
    max_settle_seconds: int | float,
    poll_interval_seconds: int | float,
    terminate_grace_seconds: int | float,
) -> tuple[bool, bool]:
    settle_deadline = time.monotonic() + max_settle_seconds
    quiet_since: float | None = None

    while True:
        now = time.monotonic()
        group_exists = _process_group_exists(process.pid)
        if group_exists:
            quiet_since = None
        elif quiet_since is None:
            quiet_since = now
        elif now - quiet_since >= quiescence_seconds:
            return False, False

        if now >= settle_deadline:
            cleanup_failed = False
            if group_exists:
                cleanup_failed = _terminate_process_group(
                    process,
                    grace_seconds=terminate_grace_seconds,
                )
            return True, cleanup_failed

        wait_seconds = min(
            poll_interval_seconds,
            max(0.001, settle_deadline - now),
        )
        if quiet_since is not None:
            wait_seconds = min(
                wait_seconds,
                max(0.001, quiescence_seconds - (now - quiet_since)),
            )
        time.sleep(wait_seconds)


def _run_emulator_process_group(
    command: Sequence[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    timeout_seconds: int | float,
    terminate_grace_seconds: int | float = TERMINATE_GRACE_SECONDS,
    normal_exit_quiescence_seconds: int | float = (
        NORMAL_EXIT_QUIESCENCE_SECONDS
    ),
    normal_exit_max_settle_seconds: int | float = (
        NORMAL_EXIT_MAX_SETTLE_SECONDS
    ),
    process_group_poll_interval_seconds: int | float = (
        PROCESS_GROUP_POLL_INTERVAL_SECONDS
    ),
) -> _EmulatorProcessResult:
    try:
        process = subprocess.Popen(
            list(command),
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except OSError:
        return _EmulatorProcessResult(start_failed=True)

    try:
        stdout, _stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        cleanup_failed = _terminate_process_group(
            process,
            grace_seconds=terminate_grace_seconds,
        )
        return _EmulatorProcessResult(
            returncode=process.returncode if process.returncode is not None else 1,
            timed_out=True,
            cleanup_failed=cleanup_failed,
        )
    except OSError:
        cleanup_failed = _terminate_process_group(
            process,
            grace_seconds=terminate_grace_seconds,
        )
        return _EmulatorProcessResult(
            returncode=process.returncode if process.returncode is not None else 1,
            start_failed=True,
            cleanup_failed=cleanup_failed,
        )

    descendants_remained, cleanup_failed = (
        _settle_normal_exit_process_group(
            process,
            quiescence_seconds=normal_exit_quiescence_seconds,
            max_settle_seconds=normal_exit_max_settle_seconds,
            poll_interval_seconds=process_group_poll_interval_seconds,
            terminate_grace_seconds=terminate_grace_seconds,
        )
    )

    return _EmulatorProcessResult(
        returncode=process.returncode if process.returncode is not None else 1,
        stdout=stdout if isinstance(stdout, str) else "",
        cleanup_failed=cleanup_failed,
        descendants_remained=descendants_remained,
    )


def _duration_ms(clock: Callable[[], float], start: float) -> int:
    return max(0, int(round((clock() - start) * 1000)))


def _repository_matches_after_execution(
    runner: Callable[..., subprocess.CompletedProcess[str]],
    git_path: Path,
    *,
    root: Path,
    environment: dict[str, str],
    expected_commit: str,
) -> bool:
    status_process = _run_git(
        runner,
        git_path,
        ["status", "--porcelain"],
        root=root,
        environment=environment,
    )
    status_output = _stdout(status_process) if status_process is not None else None
    if (
        status_process is None
        or status_process.returncode != 0
        or status_output is None
        or status_output.strip()
    ):
        return False

    commit_process = _run_git(
        runner,
        git_path,
        ["rev-parse", "HEAD"],
        root=root,
        environment=environment,
    )
    commit_output = _stdout(commit_process) if commit_process is not None else None
    return (
        commit_process is not None
        and commit_process.returncode == 0
        and commit_output is not None
        and commit_output.strip() == expected_commit
    )


def run_sec_l2(
    admin_ui_root: Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    which: Callable[[str], str | None] | None = None,
    java_fallback_dirs: Sequence[Path] = JAVA_FALLBACK_DIRS,
    clock: Callable[[], float] | None = None,
    lifecycle_quiescence_seconds: int | float | None = None,
    lifecycle_max_settle_seconds: int | float | None = None,
    lifecycle_poll_interval_seconds: int | float = (
        LIFECYCLE_POLL_INTERVAL_SECONDS
    ),
    lifecycle_terminate_grace_seconds: int | float | None = None,
) -> SecL2Result:
    runner_is_injected = runner is not None
    runner = runner or subprocess.run
    which = which or _safe_which
    clock = clock or time.monotonic
    if lifecycle_quiescence_seconds is None:
        lifecycle_quiescence_seconds = (
            0 if runner_is_injected else LIFECYCLE_QUIESCENCE_SECONDS
        )
    if lifecycle_max_settle_seconds is None:
        lifecycle_max_settle_seconds = (
            0.1 if runner_is_injected else LIFECYCLE_MAX_SETTLE_SECONDS
        )
    if lifecycle_terminate_grace_seconds is None:
        lifecycle_terminate_grace_seconds = TERMINATE_GRACE_SECONDS
    root = admin_ui_root.expanduser().resolve()

    if not root.is_dir():
        return SecL2Result(
            status="unavailable",
            detail="email-admin-ui repository is unavailable",
        )

    missing_files = [
        relative_path
        for relative_path in REQUIRED_REPOSITORY_FILES
        if not (root / relative_path).is_file()
    ]
    if missing_files:
        return SecL2Result(
            status="unavailable",
            detail="email-admin-ui repository is missing required files",
        )

    firebase_binary = root / "node_modules" / ".bin" / "firebase"
    if not firebase_binary.is_file():
        return SecL2Result(
            status="unavailable",
            detail="email-admin-ui dependencies are unavailable; run npm ci",
        )
    if not os.access(firebase_binary, os.X_OK):
        return SecL2Result(
            status="unavailable",
            detail="local Firebase CLI is unavailable or not executable",
        )
    firebase_path = firebase_binary.resolve()

    for inspector_path in (
        PROCESS_INSPECTOR_PATH,
        PORT_INSPECTOR_PATH,
    ):
        if (
            not inspector_path.is_absolute()
            or not inspector_path.is_file()
            or not os.access(inspector_path, os.X_OK)
        ):
            return SecL2Result(
                status="unavailable",
                detail="SEC-01 emulator lifecycle inspection is unavailable",
            )

    resolved_commands: dict[str, Path] = {}
    for name in REQUIRED_COMMANDS:
        command_path = _resolve_command(name, which)
        if command_path is None:
            return SecL2Result(
                status="unavailable",
                detail=f"missing or non-running required command: {name}",
            )
        resolved_commands[name] = command_path

    java_paths = _java_candidates(which, java_fallback_dirs)
    if not java_paths:
        return SecL2Result(
            status="unavailable",
            detail="missing or non-running required command: java",
        )

    with tempfile.TemporaryDirectory(prefix="sitesift-sec-l2-") as sandbox:
        sandbox_root = Path(sandbox).resolve()
        probe_environment = _controlled_environment(
            [
                *resolved_commands.values(),
                firebase_path,
                *java_paths,
            ],
            sandbox_root,
        )

        for name in REQUIRED_COMMANDS:
            if not _runtime_is_available(
                runner,
                [str(resolved_commands[name]), "--version"],
                environment=probe_environment,
            ):
                return SecL2Result(
                    status="unavailable",
                    detail=f"missing or non-running required command: {name}",
                )

        java_path = next(
            (
                candidate
                for candidate in java_paths
                if _runtime_is_available(
                    runner,
                    [str(candidate), "-version"],
                    environment=probe_environment,
                )
            ),
            None,
        )
        if java_path is None:
            return SecL2Result(
                status="unavailable",
                detail="missing or non-running required command: java",
            )

        environment = _controlled_environment(
            [
                java_path,
                resolved_commands["node"],
                resolved_commands["npm"],
                resolved_commands["git"],
                firebase_path,
            ],
            sandbox_root,
        )
        if not _runtime_is_available(
            runner,
            [str(firebase_path), "--version"],
            environment=environment,
        ):
            return SecL2Result(
                status="unavailable",
                detail="local Firebase CLI is unavailable or non-running",
            )

        top_level_process = _run_git(
            runner,
            resolved_commands["git"],
            ["rev-parse", "--show-toplevel"],
            root=root,
            environment=environment,
        )
        if top_level_process is None or top_level_process.returncode != 0:
            return SecL2Result(
                status="unavailable",
                detail="email-admin-ui repository root is unavailable",
            )
        top_level_output = _stdout(top_level_process)
        if not top_level_output:
            return SecL2Result(
                status="unavailable",
                detail="email-admin-ui repository root is unavailable",
            )
        try:
            top_level = Path(top_level_output.strip()).expanduser().resolve()
        except (OSError, RuntimeError, ValueError):
            top_level = None
        if top_level != root:
            return SecL2Result(
                status="unavailable",
                detail="email-admin-ui repository root does not match supplied root",
            )

        status_process = _run_git(
            runner,
            resolved_commands["git"],
            ["status", "--porcelain"],
            root=root,
            environment=environment,
        )
        status_output = _stdout(status_process) if status_process is not None else None
        if (
            status_process is None
            or status_process.returncode != 0
            or status_output is None
            or status_output.strip()
        ):
            return SecL2Result(
                status="unavailable",
                detail="email-admin-ui worktree must be clean",
            )

        commit_process = _run_git(
            runner,
            resolved_commands["git"],
            ["rev-parse", "HEAD"],
            root=root,
            environment=environment,
        )
        commit_output = _stdout(commit_process) if commit_process is not None else None
        commit = commit_output.strip() if commit_output is not None else ""
        if (
            commit_process is None
            or commit_process.returncode != 0
            or re.fullmatch(r"[0-9a-f]{40}", commit) is None
        ):
            return SecL2Result(
                status="unavailable",
                detail="email-admin-ui commit identity is unavailable",
            )

        snapshot_root = _materialize_committed_snapshot(
            runner,
            resolved_commands["git"],
            root=root,
            environment=environment,
            commit=commit,
            sandbox_root=sandbox_root,
            node_modules=(root / "node_modules").resolve(),
        )
        if snapshot_root is None:
            return SecL2Result(
                status="unavailable",
                detail="email-admin-ui committed source snapshot is unavailable",
            )
        if not _validate_package_script(snapshot_root / "package.json"):
            return SecL2Result(
                status="unavailable",
                detail=(
                    "email-admin-ui committed package test script "
                    "is invalid or not demo-pinned"
                ),
            )

        baseline_processes = _capture_emulator_processes(
            runner,
            environment=environment,
        )
        if baseline_processes is None:
            return SecL2Result(
                status="unavailable",
                detail="SEC-01 emulator lifecycle preflight is unavailable",
            )
        baseline_ports_closed = _emulator_ports_are_closed(
            runner,
            environment=environment,
        )
        if baseline_ports_closed is None:
            return SecL2Result(
                status="unavailable",
                detail="SEC-01 emulator lifecycle preflight is unavailable",
            )
        if (
            baseline_processes
            or not baseline_ports_closed
            or not _emulator_ports_are_free()
        ):
            return SecL2Result(
                status="unavailable",
                detail="SEC-01 emulator lifecycle preflight is occupied",
            )
        baseline_pids = frozenset(
            process.pid for process in baseline_processes
        )

        emulator_command = [
            str(resolved_commands["npm"]),
            "run",
            "test:firestore-rules",
            "--silent",
        ]
        start = clock()
        test_process: subprocess.CompletedProcess[str] | None = None
        process_result: _EmulatorProcessResult | None = None
        execution_detail = ""
        if runner_is_injected:
            try:
                test_process = _run(
                    runner,
                    emulator_command,
                    timeout=EMULATOR_TIMEOUT_SECONDS,
                    cwd=snapshot_root,
                    env=environment,
                )
            except subprocess.TimeoutExpired:
                execution_detail = "SEC-01 emulator suite timed out"
            except OSError:
                execution_detail = "SEC-01 emulator suite could not start"
        else:
            process_result = _run_emulator_process_group(
                emulator_command,
                cwd=snapshot_root,
                environment=environment,
                timeout_seconds=EMULATOR_TIMEOUT_SECONDS,
            )
            test_process = subprocess.CompletedProcess(
                emulator_command,
                process_result.returncode,
                stdout=process_result.stdout,
                stderr="",
            )

        lifecycle_result = _verify_emulator_lifecycle(
            runner,
            environment=environment,
            baseline_pids=baseline_pids,
            quiescence_seconds=lifecycle_quiescence_seconds,
            max_settle_seconds=lifecycle_max_settle_seconds,
            poll_interval_seconds=lifecycle_poll_interval_seconds,
            terminate_grace_seconds=lifecycle_terminate_grace_seconds,
        )
        duration_ms = _duration_ms(clock, start)

        if lifecycle_result.cleanup_failed:
            return SecL2Result(
                status="failed",
                errors=1,
                duration_ms=duration_ms,
                admin_ui_commit=commit,
                detail="SEC-01 emulator lifecycle cleanup failed",
            )
        if lifecycle_result.violation:
            return SecL2Result(
                status="failed",
                errors=1,
                duration_ms=duration_ms,
                admin_ui_commit=commit,
                detail="SEC-01 emulator lifecycle escaped npm containment",
            )
        if process_result is not None:
            if process_result.cleanup_failed:
                return SecL2Result(
                    status="failed",
                    errors=1,
                    duration_ms=duration_ms,
                    admin_ui_commit=commit,
                    detail="SEC-01 emulator process cleanup failed",
                )
            if process_result.descendants_remained:
                return SecL2Result(
                    status="failed",
                    errors=1,
                    duration_ms=duration_ms,
                    admin_ui_commit=commit,
                    detail=(
                        "SEC-01 emulator descendants remained "
                        "after parent exit"
                    ),
                )
            if process_result.timed_out:
                return SecL2Result(
                    status="failed",
                    errors=1,
                    duration_ms=duration_ms,
                    admin_ui_commit=commit,
                    detail="SEC-01 emulator suite timed out",
                )
            if process_result.start_failed:
                return SecL2Result(
                    status="failed",
                    errors=1,
                    duration_ms=duration_ms,
                    admin_ui_commit=commit,
                    detail="SEC-01 emulator suite could not start",
                )
        if execution_detail:
            return SecL2Result(
                status="failed",
                errors=1,
                duration_ms=duration_ms,
                admin_ui_commit=commit,
                detail=execution_detail,
            )
        if test_process is None:
            return SecL2Result(
                status="failed",
                errors=1,
                duration_ms=duration_ms,
                admin_ui_commit=commit,
                detail="SEC-01 emulator suite produced no result",
            )

        counts = _parse_tap_counts(test_process.stdout)
        if test_process.returncode != 0:
            return SecL2Result(
                status="failed",
                tests_run=counts["tests"] if counts else 0,
                failures=counts["fail"] if counts else 0,
                errors=1 if counts is None or counts["fail"] == 0 else 0,
                skipped=counts["skipped"] if counts else 0,
                duration_ms=duration_ms,
                admin_ui_commit=commit,
                detail=f"SEC-01 emulator suite exited {test_process.returncode}",
            )

        if counts is None:
            return SecL2Result(
                status="failed",
                errors=1,
                duration_ms=duration_ms,
                admin_ui_commit=commit,
                detail="SEC-01 emulator suite returned no complete TAP summary",
            )

        if not _tap_is_completely_passing(counts):
            return SecL2Result(
                status="failed",
                tests_run=counts["tests"],
                failures=counts["fail"],
                errors=1,
                skipped=counts["skipped"],
                duration_ms=duration_ms,
                admin_ui_commit=commit,
                detail="SEC-01 TAP summary is not completely passing",
            )

        if not _repository_matches_after_execution(
            runner,
            resolved_commands["git"],
            root=root,
            environment=environment,
            expected_commit=commit,
        ):
            return SecL2Result(
                status="failed",
                tests_run=counts["tests"],
                failures=0,
                errors=1,
                skipped=counts["skipped"],
                duration_ms=duration_ms,
                admin_ui_commit=commit,
                detail="email-admin-ui repository changed during emulator execution",
            )

        return SecL2Result(
            status="passed",
            tests_run=counts["tests"],
            failures=0,
            errors=0,
            skipped=counts["skipped"],
            duration_ms=duration_ms,
            admin_ui_commit=commit,
        )
