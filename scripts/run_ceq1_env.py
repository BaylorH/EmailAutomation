#!/usr/bin/env python3
"""Run CE-Q1 commands with an exact environment and sealed dependency paths."""

from __future__ import annotations

import importlib.util
import hashlib
import json
import os
from pathlib import Path
import resource
import re
import runpy
import stat
import sys
import sysconfig


CLOSED_ENV_KEYS = (
    "CEQ1_TASK_ROOT",
    "E2E_TEST_MODE",
    "FIREBASE_BUCKET",
    "FRONTEND_EMAIL_ACCESS_URL",
    "HOME",
    "LANG",
    "LC_ALL",
    "OPENAI_ASSISTANT_MODEL",
    "PATH",
    "PYTHONDONTWRITEBYTECODE",
    "PYTHONNOUSERSITE",
    "PYTEST_DISABLE_PLUGIN_AUTOLOAD",
    "SITESIFT_OUTBOUND_MODE",
    "TMPDIR",
    "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME",
)


class EnvironmentBoundaryError(RuntimeError):
    """Raised when the direct CE-Q1 launcher is not isolated."""


def validate_cf_user_text_encoding(value: str) -> None:
    """Accept only the two closed formats macOS injects at startup."""

    if not re.fullmatch(
        r"0x[0-9A-Fa-f]+:(?:0x[0-9A-Fa-f]+|0):(?:0x[0-9A-Fa-f]+|0)",
        value,
    ):
        raise EnvironmentBoundaryError("unexpected OS-injected text encoding value")


def _repo_root() -> Path:
    root = Path(__file__).resolve().parents[1]
    if (root / "scripts/run_ceq1_env.py").resolve() != Path(__file__).resolve():
        raise EnvironmentBoundaryError("launcher escaped the worktree")
    return root


def require_isolated_flags(argv: tuple[str, ...] | list[str]) -> None:
    values = tuple(argv)
    required = ("-I", "-S", "-B")
    if not all(flag in values for flag in required):
        raise EnvironmentBoundaryError("launcher requires -I -S -B")


def _private_dir(path: Path) -> None:
    path = Path(path)
    if path.is_symlink():
        raise EnvironmentBoundaryError(f"symlinked direct-run directory: {path}")
    if not path.exists():
        parent = path.parent
        parent_info = parent.lstat()
        if not stat.S_ISDIR(parent_info.st_mode) or stat.S_ISLNK(parent_info.st_mode):
            raise EnvironmentBoundaryError(f"unsafe direct-run parent: {parent}")
        path.mkdir(mode=0o700)
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o700
        or info.st_uid != os.getuid()
    ):
        raise EnvironmentBoundaryError(f"unsafe direct-run directory: {path}")


def build_child_env(task_root: Path) -> dict[str, str]:
    task = Path(task_root)
    if not task.is_absolute():
        raise EnvironmentBoundaryError("task root must be absolute")
    if task.is_symlink():
        raise EnvironmentBoundaryError("task root must not be a symlink")
    home = task / "home"
    tmp = task / "tmp"
    cache = task / "cache"
    config = task / "config"
    for path in (task, home, tmp, cache, config):
        _private_dir(path)
    env = {
        "CEQ1_TASK_ROOT": str(task),
        "E2E_TEST_MODE": "true",
        "FIREBASE_BUCKET": "demo-ceq1.invalid",
        "FRONTEND_EMAIL_ACCESS_URL": "https://ceq1.invalid/email-access",
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "OPENAI_ASSISTANT_MODEL": "ceq1-frozen-proposal",
        "PATH": "/usr/bin:/bin",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "SITESIFT_OUTBOUND_MODE": "paused",
        "TMPDIR": str(tmp),
        "XDG_CACHE_HOME": str(cache),
        "XDG_CONFIG_HOME": str(config),
    }
    if set(env) != set(CLOSED_ENV_KEYS):
        raise EnvironmentBoundaryError("closed environment implementation drift")
    return env


def _close_non_stdio_fds() -> None:
    try:
        candidates = [int(name) for name in os.listdir("/dev/fd") if name.isdecimal()]
    except OSError:
        soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
        candidates = list(range(3, min(int(soft), 65536)))
    for fd in candidates:
        if fd <= 2:
            continue
        try:
            os.close(fd)
        except OSError:
            pass


def _open_fds() -> list[int]:
    result: list[int] = []
    for fd in range(0, 256):
        try:
            os.fstat(fd)
        except OSError:
            continue
        result.append(fd)
    return result


def _bundle_paths(root: Path) -> tuple[Path, Path]:
    bundle = root / ".ceq1-venv"
    info = bundle.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o555
        or info.st_uid != os.getuid()
    ):
        raise EnvironmentBoundaryError("sealed bundle root is unsafe")
    bundle = bundle.resolve()
    executable = Path(sys.executable).resolve()
    if executable != bundle / "python/bin/python3.12":
        raise EnvironmentBoundaryError("launcher is not the sealed copied Python")
    site = bundle / "venv/lib/python3.12/site-packages"
    if not site.is_dir() or site.is_symlink():
        raise EnvironmentBoundaryError("sealed site-packages is absent")
    for path in (Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve(), Path(sysconfig.get_path("stdlib")).resolve()):
        if path != bundle and bundle not in path.parents:
            raise EnvironmentBoundaryError(f"Python path escaped sealed bundle: {path}")
    return bundle, site


def build_execve_contract(
    root: Path,
    task_root: Path,
    arguments: list[str],
) -> tuple[Path, list[str], dict[str, str]]:
    root = Path(root).resolve()
    bundle = root / ".ceq1-venv"
    info = bundle.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise EnvironmentBoundaryError("sealed bundle is absent or symlinked")
    executable = bundle / "python/bin/python3.12"
    executable_info = executable.lstat()
    if not stat.S_ISREG(executable_info.st_mode) or stat.S_ISLNK(executable_info.st_mode):
        raise EnvironmentBoundaryError("sealed Python entry is unsafe")
    env = build_child_env(task_root)
    argv = [
        str(executable),
        "-I",
        "-S",
        "-B",
        str(root / "scripts/run_ceq1_env.py"),
        "--ceq1-exec-child",
        *arguments,
    ]
    return executable, argv, env


def _install_projection_paths(root: Path, site: Path) -> None:
    expected = [str(site), str(root)]
    sys.path[:] = expected + [path for path in sys.path if path and Path(path).resolve().is_relative_to(root / ".ceq1-venv")]
    if sys.path[:2] != expected:
        raise EnvironmentBoundaryError("projection path installation failed")


def _validate_sealed_boundary(root: Path) -> None:
    bootstrap_path = root / "scripts/bootstrap_ceq1_runtime.py"
    toolchain_path = root / "docs/release-safety/ceq1-toolchain-manifest.json"
    toolchain = json.loads(toolchain_path.read_text(encoding="utf-8"))
    expected_bootstrap_hash = toolchain.get("bootstrapSha256")
    if not isinstance(expected_bootstrap_hash, str) or not re.fullmatch(
        r"[0-9a-f]{64}", expected_bootstrap_hash
    ):
        raise EnvironmentBoundaryError("toolchain bootstrap hash is absent")
    bootstrap_hash = hashlib.sha256(bootstrap_path.read_bytes()).hexdigest()
    if bootstrap_hash != expected_bootstrap_hash:
        raise EnvironmentBoundaryError("bootstrap validator source drift")
    builder_path = root / "scripts/build_ceq1_wheelhouse.py"
    expected_builder_hash = toolchain.get("builderSha256")
    if (
        not isinstance(expected_builder_hash, str)
        or hashlib.sha256(builder_path.read_bytes()).hexdigest() != expected_builder_hash
    ):
        raise EnvironmentBoundaryError("wheel builder source drift")
    spec = importlib.util.spec_from_file_location("ceq1_runtime_boundary", bootstrap_path)
    if spec is None or spec.loader is None:
        raise EnvironmentBoundaryError("cannot load sealed-runtime validator")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.validate_committed_toolchain(root, toolchain)
    module.validate_runtime_receipt(
        root,
        root / ".ceq1-runtime/bootstrap-receipt.json",
    )
    wheel_manifest = module._validate_static_inputs(root)
    module._validate_wheelhouse(
        root / ".ceq1-runtime/wheelhouse",
        wheel_manifest,
        require_sealed=True,
    )


def _runtime_receipt(root: Path, bundle: Path, site: Path) -> dict[str, object]:
    extensions: list[str] = []
    for module_name in ("_ssl", "_hashlib", "_sqlite3"):
        spec = importlib.util.find_spec(module_name)
        if spec is None or not spec.origin:
            raise EnvironmentBoundaryError(f"missing extension module: {module_name}")
        if spec.origin in {"built-in", "frozen"}:
            continue
        extensions.append(str(Path(spec.origin).resolve()))
    loaded = sorted({str(Path(path).resolve()) for path in (*sys.path, *extensions) if path})
    for path in loaded:
        resolved = Path(path)
        if resolved == root:
            continue
        if resolved != bundle and bundle not in resolved.parents:
            raise EnvironmentBoundaryError(f"loaded path escaped sealed bundle: {path}")
    return {
        "environmentKeys": sorted(os.environ),
        "fds": _open_fds(),
        "executable": str(Path(sys.executable).resolve()),
        "prefix": str(Path(sys.prefix).resolve()),
        "basePrefix": str(Path(sys.base_prefix).resolve()),
        "stdlib": str(Path(sysconfig.get_path("stdlib")).resolve()),
        "platstdlib": str(Path(sysconfig.get_path("platstdlib")).resolve()),
        "loadedPaths": [path for path in loaded if path != str(root)],
        "sitePackages": str(site),
    }


def _run_target(arguments: list[str]) -> int:
    if not arguments:
        raise EnvironmentBoundaryError("missing target command")
    if arguments[0] == "-m":
        if len(arguments) < 2:
            raise EnvironmentBoundaryError("missing module name")
        module = arguments[1]
        sys.argv = [module, *arguments[2:]]
        runpy.run_module(module, run_name="__main__", alter_sys=True)
        return 0
    target = Path(arguments[0])
    if not target.is_absolute():
        target = (_repo_root() / target).resolve()
    if target != _repo_root() and _repo_root() not in target.parents:
        raise EnvironmentBoundaryError("target escaped worktree")
    sys.argv = [str(target), *arguments[1:]]
    runpy.run_path(str(target), run_name="__main__")
    return 0


def _child_main(root: Path, arguments: list[str]) -> int:
    cf_encoding = os.environ.pop("__CF_USER_TEXT_ENCODING", None)
    if cf_encoding is not None:
        validate_cf_user_text_encoding(cf_encoding)
    if set(os.environ) != set(CLOSED_ENV_KEYS):
        raise EnvironmentBoundaryError(
            f"exec child environment keys drifted: {sorted(os.environ)}"
        )
    task = Path(os.environ["CEQ1_TASK_ROOT"])
    expected = build_child_env(task)
    if dict(os.environ) != expected:
        raise EnvironmentBoundaryError("exec child environment values drifted")
    _validate_sealed_boundary(root)
    bundle, site = _bundle_paths(root)
    _install_projection_paths(root, site)
    if arguments == ["--inspect-runtime"]:
        receipt = _runtime_receipt(root, bundle, site)
        print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
        return 0
    return _run_target(arguments)


def main(argv: list[str] | None = None) -> int:
    require_isolated_flags(tuple(getattr(sys, "orig_argv", ())))
    if not (sys.flags.isolated and sys.flags.no_site and sys.flags.dont_write_bytecode):
        raise EnvironmentBoundaryError("interpreter flags are not isolated")
    os.umask(0o077)
    root = _repo_root()
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:1] == ["--ceq1-exec-child"]:
        _close_non_stdio_fds()
        return _child_main(root, arguments[1:])
    runtime_root = root / ".ceq1-runtime"
    _private_dir(runtime_root)
    _close_non_stdio_fds()
    executable, child_argv, environment = build_execve_contract(
        root,
        runtime_root / "direct",
        arguments,
    )
    os.execve(executable, child_argv, environment)
    raise EnvironmentBoundaryError("os.execve unexpectedly returned")


if __name__ == "__main__":
    raise SystemExit(main())
