#!/usr/bin/env python3
"""Make the certification build runnable on a fresh machine, in one command.

This exists because of a measured failure, not a hypothetical one. The build
carried a per-module sweep baseline of 165 PASS / 28 FAIL for days and treated
those 28 as code defects. Installing the dependencies the repo ALREADY DECLARES
in requirements.txt moved it to 185 PASS / 10 FAIL with zero source changes:

  * flask / flask-cors  -> tests/test_process_user_service.py could not even be
                           IMPORTED, so the whole service contract was silently
                           uncollectable rather than failing visibly.
  * pdfplumber / PyMuPDF -> Task 4's scanned-PDF gate was recorded as
                           INSTRUMENT_BLOCKED "on missing fitz". It was not
                           blocked. It was uninstalled.

An unprovisioned environment does not announce itself. It reports as a red test
suite, which is indistinguishable from a broken product until someone checks --
and "already failing at baseline" then quietly becomes permission to ignore it.

    python3 scripts/provision_local_certification_env.py --credential-out PATH

Installs into the SAME interpreter that already resolves the project's other
dependencies. Nothing here writes to the repo.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Declared in requirements.txt, but easy to miss locally because their absence
# degrades quietly instead of erroring.
QUIET_FAILURE_PACKAGES = ("flask", "flask-cors", "pdfplumber", "PyMuPDF")

# Test-only. Not a runtime dependency, so deliberately NOT added to
# requirements.txt or the image lock: the twin's deployment contract test parses
# the manifest, and a pin that skips when a parser is missing is not a pin.
TEST_ONLY_PACKAGES = ("pyyaml",)

IMPORT_NAMES = {
    "flask": "flask",
    "flask-cors": "flask_cors",
    "pdfplumber": "pdfplumber",
    "PyMuPDF": "fitz",
    "pyyaml": "yaml",
}


def missing(packages):
    absent = []
    for package in packages:
        module = IMPORT_NAMES[package]
        probe = subprocess.run(
            [sys.executable, "-c", f"import {module}"],
            capture_output=True,
        )
        if probe.returncode != 0:
            absent.append(package)
    return absent


def install(packages):
    if not packages:
        return 0
    # --user matches where this project's other dependencies already live;
    # --break-system-packages is required by PEP 668 on a Homebrew interpreter.
    command = [sys.executable, "-m", "pip", "install", "--user",
               "--break-system-packages", *packages]
    print("installing:", " ".join(packages))
    return subprocess.run(command).returncode


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credential-out", type=Path,
                        help="also emit a synthetic service account here "
                             "(must be outside the repo)")
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args(argv)

    wanted = list(QUIET_FAILURE_PACKAGES) + list(TEST_ONLY_PACKAGES)
    absent = missing(wanted)

    if args.check_only:
        print("missing:", ", ".join(absent) if absent else "none")
        return 1 if absent else 0

    if absent:
        code = install(absent)
        if code:
            return code
        still = missing(wanted)
        if still:
            print(f"still missing after install: {still}", file=sys.stderr)
            return 1
    print(f"dependencies satisfied ({len(wanted)} checked)")

    if args.credential_out:
        generator = REPO_ROOT / "scripts" / "make_synthetic_credential.py"
        return subprocess.run(
            [sys.executable, str(generator), str(args.credential_out)]
        ).returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
