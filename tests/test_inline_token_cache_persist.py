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
import os
import tempfile
import unittest
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


def _run_refresh(*, has_state_changed, upload_side_effect=None, uid="uid-1"):
    """Drive refresh_and_process_user with a FakeTokenCache whose
    has_state_changed is fixed. Returns (upload_mock, fake_cache, health_mock).

    atexit.register/unregister are patched to no-ops so the exit-time backstop
    handler is not left registered against the (now torn-down) test doubles.
    """
    fake_cache = FakeTokenCache(has_state_changed=has_state_changed)

    with tempfile.NamedTemporaryFile("w", delete=False) as token_file:
        token_file.write("{}")
        token_path = token_file.name

    upload_mock = MagicMock(side_effect=upload_side_effect)
    health_mock = MagicMock(return_value={})

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
             patch.object(atexit, "register"), \
             patch.object(atexit, "unregister"):
            main.refresh_and_process_user(uid)
    finally:
        # Neutralize any lingering exit-time handler that closed over fake_cache.
        fake_cache.has_state_changed = False
        os.unlink(token_path)

    return upload_mock, fake_cache, health_mock


class InlineTokenCachePersistTests(unittest.TestCase):
    def test_changed_cache_is_persisted_inline_for_the_uid(self):
        """(a) has_state_changed True → inline upload_token called with the uid."""
        upload_mock, fake_cache, _ = _run_refresh(has_state_changed=True)

        self.assertTrue(
            upload_mock.called,
            "a token cache refreshed mid-run (has_state_changed True) must be "
            "persisted INLINE, not left to an atexit handler that a SIGKILLed "
            "Cloud Run worker never runs",
        )
        # Persisted for THIS user's cache.
        _, kwargs = upload_mock.call_args
        self.assertEqual("uid-1", kwargs.get("user_id"))
        self.assertTrue(fake_cache.serialize_calls >= 1)

    def test_unchanged_cache_skips_the_storage_put(self):
        """(b) has_state_changed False → no upload (idempotent no-op)."""
        upload_mock, _, _ = _run_refresh(has_state_changed=False)

        self.assertFalse(
            upload_mock.called,
            "an unchanged cache must NOT trigger a Storage PUT every run",
        )

    def test_persist_failure_does_not_propagate(self):
        """(c) a persist exception is swallowed — the run still completes and the
        health record is still written (the atexit backstop covers the upload)."""
        upload_mock, _, health_mock = _run_refresh(
            has_state_changed=True,
            upload_side_effect=RuntimeError("Storage 503"),
        )

        self.assertTrue(upload_mock.called, "inline persist must have been attempted")
        self.assertTrue(
            health_mock.called,
            "a best-effort persist failure must not abort the user's run; "
            "record_user_health must still fire",
        )


if __name__ == "__main__":
    unittest.main()
