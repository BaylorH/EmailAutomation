"""Rail: the MSAL token cache must be persisted INLINE on the success path.

Gap: refresh_and_process_user only persisted the MSAL SerializableTokenCache via
an ``atexit``-registered ``_save_cache``. A SIGKILLed / OOM-killed / timed-out
Cloud Run worker never runs atexit handlers, so a cache that was refreshed
mid-run (new access token, ROTATED refresh token) was lost — the next run falls
back and can be forced into re-auth.

Fix (pinned here): at the natural end of a successful per-user run,
refresh_and_process_user calls the SAME ``_save_cache`` upload routine INLINE
when ``cache.has_state_changed`` is True. The atexit registration stays as a
backstop for early-return / crash paths.

Properties:
  (a) has_state_changed True at end of run → inline persist uploads for the uid,
  (b) has_state_changed False → NO upload (no unnecessary Storage PUT),
  (c) a persist exception does NOT propagate out of refresh_and_process_user
      (best-effort; the atexit backstop still covers it).

Everything below the token cache is mocked (mirrors tests/test_graph_send_health.py):
no Graph calls, no Firestore, no Storage. atexit.register/unregister are patched
so the test leaves no lingering exit-time upload handler.
"""

import atexit
import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch, MagicMock

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "service-account.json"),
)

import main


class FakeTokenCache:
    """MSAL SerializableTokenCache double with a settable has_state_changed."""

    def __init__(self, has_state_changed=False):
        self.has_state_changed = has_state_changed
        self.serialize_calls = 0

    def deserialize(self, _payload):
        return None

    def serialize(self):
        self.serialize_calls += 1
        return "{}"


class FakeMsalApp:
    def __init__(self, *args, **kwargs):
        pass

    def get_accounts(self):
        return [{"home_account_id": "account-1"}]

    def acquire_token_silent(self, *args, **kwargs):
        return {"access_token": "fake-access-token", "expires_in": 3600}


HEALTHY_INBOX = {"status": "healthy", "operation": "inbox_scan"}
HEALTHY_SENT = {"status": "healthy", "operation": "sent_items_scan"}


def _run_refresh(
    *, has_state_changed, upload_side_effect=None, uid="uid-1", invoke_backstop=False
) -> tuple[MagicMock, FakeTokenCache, MagicMock, bool]:
    """Drive refresh_and_process_user with a FakeTokenCache whose
    has_state_changed is fixed. Returns (upload_mock, fake_cache, health_mock,
    state_after_run) so cleanup cannot mask a failed cache-marker reset.

    atexit.register/unregister use local handler tracking so the exit-time
    backstop can be captured and invoked without registering it with the real
    process.
    """
    fake_cache = FakeTokenCache(has_state_changed=has_state_changed)

    with tempfile.NamedTemporaryFile("w", delete=False) as token_file:
        token_file.write("{}")
        token_path = token_file.name

    upload_mock = MagicMock(side_effect=upload_side_effect)
    health_mock = MagicMock(return_value={})
    registered_handlers = []
    state_after_run = fake_cache.has_state_changed

    def register_handler(handler) -> object:
        registered_handlers.append(handler)
        return handler

    def unregister_handler(handler) -> None:
        if handler in registered_handlers:
            registered_handlers.remove(handler)

    try:
        with patch.object(main, "TOKEN_CACHE", token_path), \
             patch.object(main, "download_token"), \
             patch.object(main, "upload_token", upload_mock), \
             patch.object(main, "SerializableTokenCache", lambda: fake_cache), \
             patch.object(main, "ConfidentialClientApplication", FakeMsalApp), \
             patch.object(main, "send_outboxes", return_value=None), \
             patch.object(main, "scan_inbox_against_index", return_value=HEALTHY_INBOX), \
             patch.object(main, "scan_sent_items_for_manual_replies", return_value=HEALTHY_SENT), \
             patch.object(main, "retry_processing_failures"), \
             patch.object(main, "process_pending_responses", return_value=0), \
             patch.object(main, "check_and_send_followups", return_value=0), \
             patch.object(main, "auto_cleanup_firestore"), \
             patch.object(main, "reconcile_stale_processing_failures"), \
             patch.object(main, "record_user_health", health_mock), \
             patch.object(atexit, "register", side_effect=register_handler), \
             patch.object(atexit, "unregister", side_effect=unregister_handler):
            main.refresh_and_process_user(uid)
            if invoke_backstop:
                self_registered = list(registered_handlers)
                if not self_registered:
                    raise AssertionError("refresh_and_process_user did not register its cache backstop")
                self_registered[-1]()
            state_after_run = fake_cache.has_state_changed
    finally:
        # Neutralize any lingering exit-time handler that closed over fake_cache.
        fake_cache.has_state_changed = False
        os.unlink(token_path)

    return upload_mock, fake_cache, health_mock, state_after_run


class InlineTokenCachePersistTests(unittest.TestCase):
    def test_changed_cache_persists_without_invoking_the_atexit_backstop(self):
        """The success path itself must upload; this test deliberately leaves
        the registered exit handler untouched so it cannot mask a missing inline
        persist."""
        upload_mock, fake_cache, _, state_after_run = _run_refresh(
            has_state_changed=True,
            invoke_backstop=False,
        )

        self.assertEqual(upload_mock.call_count, 1)
        self.assertEqual(fake_cache.serialize_calls, 1)
        self.assertFalse(state_after_run)

    def test_changed_cache_is_persisted_inline_for_the_uid(self):
        """(a) has_state_changed True → inline upload_token called with the uid."""
        upload_mock, fake_cache, _, state_after_run = _run_refresh(
            has_state_changed=True,
            invoke_backstop=True,
        )

        self.assertTrue(
            upload_mock.called,
            "a token cache refreshed mid-run (has_state_changed True) must be "
            "persisted INLINE, not left to an atexit handler that a SIGKILLed "
            "Cloud Run worker never runs",
        )
        # Persisted for THIS user's cache.
        _, kwargs = upload_mock.call_args
        self.assertEqual("uid-1", kwargs.get("user_id"))
        self.assertEqual(upload_mock.call_count, 1)
        self.assertEqual(fake_cache.serialize_calls, 1)
        self.assertFalse(state_after_run)

    def test_unchanged_cache_skips_the_storage_put(self):
        """(b) has_state_changed False → no upload (idempotent no-op)."""
        upload_mock, _, _, _ = _run_refresh(has_state_changed=False)

        self.assertFalse(
            upload_mock.called,
            "an unchanged cache must NOT trigger a Storage PUT every run",
        )

    def test_persist_failure_does_not_propagate(self):
        """(c) a persist exception is swallowed — the run still completes and the
        health record is still written (the atexit backstop covers the upload)."""
        upload_mock, fake_cache, health_mock, state_after_run = _run_refresh(
            has_state_changed=True,
            upload_side_effect=[RuntimeError("Storage 503"), None],
            invoke_backstop=True,
        )

        self.assertEqual(upload_mock.call_count, 2)
        self.assertEqual(fake_cache.serialize_calls, 2)
        self.assertFalse(state_after_run)
        self.assertTrue(
            health_mock.called,
            "a best-effort persist failure must not abort the user's run; "
            "record_user_health must still fire before the backstop retry",
        )

    def test_inline_failure_warning_is_emitted_before_backstop_retry(self):
        output = io.StringIO()
        warning_seen_before_retry = []
        attempts = 0

        def upload_side_effect(*_args, **_kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("Storage 503")
            warning_seen_before_retry.append(
                "Inline token cache persist failed" in output.getvalue()
            )

        with redirect_stdout(output):
            _run_refresh(
                has_state_changed=True,
                upload_side_effect=upload_side_effect,
                invoke_backstop=True,
            )

        self.assertEqual(attempts, 2)
        self.assertEqual(warning_seen_before_retry, [True])


if __name__ == "__main__":
    unittest.main()
