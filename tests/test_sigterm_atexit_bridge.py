"""WS-B: SIGTERM→atexit bridge + lease release on SystemExit (worklist #5).

Cloud Run Jobs stop a container by sending SIGTERM (then SIGKILL after the
grace period). Python's default SIGTERM disposition terminates the process
WITHOUT running atexit handlers — which would drop the pending token-cache
upload that refresh_and_process_user registers via atexit. main.py installs
``_install_sigterm_atexit_bridge`` to translate SIGTERM into a non-zero
``sys.exit(143)`` (128 + SIGTERM) so the interpreter unwinds normally AND the
interrupted run is marked *failed* on Cloud Run (a task succeeds only on exit
0), instead of masking a mid-send/write interruption as success.

Two properties are pinned here (previously listed under 'Unverified' in
deploy/README.md):

(a) SIGTERM with the bridge installed → atexit handlers run and the process
    exits 143 (non-zero, the conventional 128+SIGTERM code). A CONTROL run
    without the bridge proves the test discriminates: the process dies with
    signal 15 and the atexit handler never fires.

(b) A SystemExit raised mid-callback (exactly what the bridge produces if
    SIGTERM lands during the pipeline) still unwinds through the ``finally``
    in run_with_scheduler_lease, so the Firestore lease is RELEASED
    immediately instead of squatting until the 45-min TTL expiry.

Both are fully local and deterministic: (a) uses a subprocess + os.kill, (b)
uses the same in-memory FakeFirestore double as tests/test_scheduler_lease.py.
No deploy, no cloud access, no live sends.
"""

import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    "/Users/baylorharrison/Documents/GitHub/EmailAutomation/service-account.json",
)

from email_automation import scheduler_lease


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Child snippet: registers an atexit handler that writes a sentinel file (the
# stand-in for the token-cache upload), optionally installs the bridge, prints
# READY, then sleeps waiting for the parent's SIGTERM.
_CHILD_SNIPPET = textwrap.dedent(
    """
    import atexit, sys, time

    sentinel_path = sys.argv[1]
    install_bridge = sys.argv[2] == "bridge"

    from main import _install_sigterm_atexit_bridge

    def _upload_stand_in():
        with open(sentinel_path, "w") as f:
            f.write("atexit-ran")

    atexit.register(_upload_stand_in)

    if install_bridge:
        _install_sigterm_atexit_bridge()

    print("READY", flush=True)
    time.sleep(120)  # parent SIGTERMs long before this expires
    """
)


class SigtermAtexitBridgeSubprocessTests(unittest.TestCase):
    """Property (a): SIGTERM → atexit handlers run, iff the bridge is installed."""

    def _run_child_and_sigterm(self, install_bridge: bool):
        env = dict(os.environ)
        env["E2E_TEST_MODE"] = "true"  # importing main must not need live env
        env["PYTHONHASHSEED"] = "0"
        # Defense-in-depth: even though the child never touches the lease,
        # make any accidental Firestore RPC fail fast locally.
        env["FIRESTORE_EMULATOR_HOST"] = "127.0.0.1:1"

        with tempfile.TemporaryDirectory() as tmp:
            sentinel = os.path.join(tmp, "token-cache-uploaded")
            mode = "bridge" if install_bridge else "no-bridge"
            proc = subprocess.Popen(
                [sys.executable, "-c", _CHILD_SNIPPET, sentinel, mode],
                cwd=REPO_ROOT,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            try:
                # Importing main emits benign warnings (e.g. optional PDF
                # libs missing); skip lines until READY, bounded so a broken
                # child can't stall the suite.
                seen = []
                for _ in range(200):
                    line = proc.stdout.readline()
                    if not line:  # EOF: child died during import
                        break
                    seen.append(line)
                    if line.strip() == "READY":
                        break
                self.assertTrue(
                    seen and seen[-1].strip() == "READY",
                    f"child failed before READY:\n{''.join(seen)}",
                )
                os.kill(proc.pid, signal.SIGTERM)
                returncode = proc.wait(timeout=30)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=10)
                proc.stdout.close()
            return returncode, os.path.exists(sentinel)

    def test_sigterm_with_bridge_runs_atexit_and_exits_nonzero(self):
        returncode, sentinel_written = self._run_child_and_sigterm(
            install_bridge=True
        )
        self.assertEqual(
            128 + signal.SIGTERM, returncode,
            "bridge must convert SIGTERM into a clean but NON-ZERO "
            "SystemExit(143) unwind, so Cloud Run marks the interrupted task "
            "failed (exit 0 would mask a timeout/cancel as success)",
        )
        self.assertTrue(
            sentinel_written,
            "atexit handler (token-cache upload stand-in) must run on SIGTERM "
            "when the bridge is installed",
        )

    def test_control_without_bridge_dies_on_signal_and_skips_atexit(self):
        """Falsification control: proves the assertion above is load-bearing.
        Without the bridge, Python's default SIGTERM disposition kills the
        process (returncode -SIGTERM) and atexit handlers never run."""
        returncode, sentinel_written = self._run_child_and_sigterm(
            install_bridge=False
        )
        self.assertEqual(
            -signal.SIGTERM, returncode,
            "control child should die from raw SIGTERM (default disposition)",
        )
        self.assertFalse(
            sentinel_written,
            "without the bridge the atexit handler must NOT have run — if it "
            "did, the bridge test proves nothing",
        )


# --- In-memory Firestore double (mirrors tests/test_scheduler_lease.py) -------

class FakeSnapshot:
    def __init__(self, data=None):
        self._data = data
        self.exists = data is not None

    def to_dict(self):
        return dict(self._data or {})


class FakeDocRef:
    def __init__(self, data=None):
        self.data = data
        self.id = "emailAutomation"

    def get(self, transaction=None):
        return FakeSnapshot(self.data)


class FakeTransaction:
    def set(self, ref, data, merge=False):
        ref.data = {**(ref.data or {}), **data}

    def update(self, ref, data):
        ref.data = {**(ref.data or {}), **data}


class FakeFirestore:
    def __init__(self, existing=None):
        self.doc_ref = FakeDocRef(existing)

    def transaction(self):
        # Fresh transaction per call: acquire and release each get their own.
        return FakeTransaction()

    def collection(self, name):
        return self

    def document(self, name):
        return self.doc_ref


class LeaseReleasedOnSystemExitTests(unittest.TestCase):
    """Property (b): SystemExit mid-callback still releases the lease."""

    def test_systemexit_in_callback_releases_lease_via_finally(self):
        fs = FakeFirestore()  # no lease yet

        def callback():
            # What the SIGTERM bridge raises if the signal lands mid-pipeline:
            # a non-zero SystemExit(143) (128 + SIGTERM).
            raise SystemExit(128 + signal.SIGTERM)

        with patch.object(scheduler_lease, "transactional", lambda fn: fn):
            with self.assertRaises(SystemExit):
                scheduler_lease.run_with_scheduler_lease(
                    callback,
                    fs_client=fs,
                    owner="cloudrun-host:123",
                )

        data = fs.doc_ref.data or {}
        self.assertEqual(
            "released", data.get("status"),
            "lease must be RELEASED in the finally block, not left 'running' "
            "to squat until the 45-min TTL expiry",
        )
        self.assertEqual("cloudrun-host:123", data.get("owner"))
        self.assertIn("releasedAt", data)

    def test_nonzero_systemexit_also_releases_lease(self):
        """Crash-shaped exits (SystemExit(1), e.g. a scope violation raised
        inside the callback) must release the lease too."""
        fs = FakeFirestore()

        def callback():
            raise SystemExit("scope blocked")

        with patch.object(scheduler_lease, "transactional", lambda fn: fn):
            with self.assertRaises(SystemExit):
                scheduler_lease.run_with_scheduler_lease(
                    callback,
                    fs_client=fs,
                    owner="cloudrun-host:123",
                )

        self.assertEqual("released", (fs.doc_ref.data or {}).get("status"))


if __name__ == "__main__":
    unittest.main()
