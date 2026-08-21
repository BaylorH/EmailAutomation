"""The advisory half: Tier 2 lives here, and it can never touch a send.

``_record_signature_website_advisory`` runs once per ``send_outboxes`` call,
writes a JSON advisory onto the user profile for the settings UI, and is
swallowed whole on any error. The most important assertion in this file is the
last one: when the advisory blows up, ``send_outboxes`` still runs the outbox.
"""

from __future__ import annotations

import os
import socket
import unittest
from unittest import mock

# Importing email_automation.email pulls in app_config, which refuses to load
# without real credentials unless the suite declares itself a test run. Same
# bootstrap the existing send-path suites use.
os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "service-account.json",
    ),
)

from email_automation import email as email_mod
from email_automation.signature_website_validation import (
    SIGNATURE_WEBSITE_REACHABILITY_ENV,
    SIGNATURE_WEBSITE_VALIDATION_ENV,
)

USER_ID = "user-under-test"


def _on(**extra):
    env = {SIGNATURE_WEBSITE_VALIDATION_ENV: "true"}
    env.update(extra)
    return mock.patch.dict(os.environ, env, clear=False)


def _off():
    return mock.patch.dict(
        os.environ,
        {SIGNATURE_WEBSITE_VALIDATION_ENV: "", SIGNATURE_WEBSITE_REACHABILITY_ENV: ""},
        clear=False,
    )


class _FakeDoc:
    def __init__(self, store, data=None, exists=True):
        self._store = store
        self._data = data or {}
        self.exists = exists

    def to_dict(self):
        return dict(self._data)

    def get(self):
        return self

    def set(self, payload, merge=False):
        self._store.writes.append((payload, merge))

    def collection(self, name):
        return _FakeCollection(self._store, name)


class _FakeQuery:
    def order_by(self, *_args, **_kwargs):
        return self

    def stream(self):
        return iter(())


class _FakeCollection(_FakeQuery):
    def __init__(self, store, name):
        self._store = store
        self._name = name

    def document(self, _doc_id):
        if self._name == "users":
            return _FakeDoc(self._store, self._store.user_data, self._store.user_exists)
        return _FakeDoc(self._store)


class _FakeFirestore:
    def __init__(self, user_data=None, user_exists=True):
        self.user_data = user_data or {}
        self.user_exists = user_exists
        self.writes = []

    def collection(self, name):
        return _FakeCollection(self, name)


class _ExplodingFirestore(_FakeFirestore):
    def collection(self, name):
        if self.writes is not None:
            self.writes.append(("attempted", name))
        raise RuntimeError("firestore is down")


BAD_PROFILE = {"professionalSignature": {"website": "yourcompany.com"}}
GOOD_PROFILE = {"professionalSignature": {"website": "northbridge-commercial.com"}}


class AdvisoryWriteTests(unittest.TestCase):
    def test_flag_off_writes_nothing(self):
        fs = _FakeFirestore(BAD_PROFILE)
        with _off():
            email_mod._record_signature_website_advisory(fs, USER_ID, BAD_PROFILE)
        self.assertEqual(fs.writes, [])

    def test_finding_is_written_to_the_user_profile(self):
        fs = _FakeFirestore(BAD_PROFILE)
        with _on():
            email_mod._record_signature_website_advisory(fs, USER_ID, BAD_PROFILE)
        self.assertEqual(len(fs.writes), 1)
        payload, merge = fs.writes[0]
        self.assertTrue(merge)
        advisory = payload["signatureWebsiteAdvisory"]
        self.assertTrue(advisory["hasBlockingFinding"])
        self.assertEqual(advisory["findings"][0]["code"], "placeholder_host")

    def test_clean_profile_with_no_stale_advisory_writes_nothing(self):
        fs = _FakeFirestore(GOOD_PROFILE)
        with _on():
            email_mod._record_signature_website_advisory(fs, USER_ID, GOOD_PROFILE)
        self.assertEqual(fs.writes, [])

    def test_stale_advisory_is_cleared_once_the_website_is_fixed(self):
        fixed = dict(GOOD_PROFILE)
        fixed["signatureWebsiteAdvisory"] = {
            "hasBlockingFinding": True,
            "findings": [{"code": "placeholder_host"}],
        }
        fs = _FakeFirestore(fixed)
        with _on():
            email_mod._record_signature_website_advisory(fs, USER_ID, fixed)
        self.assertEqual(len(fs.writes), 1)
        payload, merge = fs.writes[0]
        self.assertIsNone(payload["signatureWebsiteAdvisory"])
        self.assertTrue(merge)

    def test_a_firestore_write_failure_is_swallowed(self):
        fs = _ExplodingFirestore(BAD_PROFILE)
        with _on():
            email_mod._record_signature_website_advisory(fs, USER_ID, BAD_PROFILE)  # must not raise

    def test_garbage_profile_is_tolerated(self):
        for profile in ({}, {"professionalSignature": "not-a-dict"}, {"emailSignature": 7}):
            fs = _FakeFirestore(profile)
            with _on():
                email_mod._record_signature_website_advisory(fs, USER_ID, profile)
            self.assertEqual(fs.writes, [], profile)

    def test_no_network_call_unless_the_reachability_flag_is_on(self):
        fs = _FakeFirestore(GOOD_PROFILE)
        with _on():
            with mock.patch.object(
                socket, "getaddrinfo", side_effect=AssertionError("network!")
            ):
                email_mod._record_signature_website_advisory(fs, USER_ID, GOOD_PROFILE)
        self.assertEqual(fs.writes, [])

    def test_reachability_finding_is_only_ever_a_warning(self):
        fs = _FakeFirestore(GOOD_PROFILE)
        with _on(**{SIGNATURE_WEBSITE_REACHABILITY_ENV: "true"}):
            with mock.patch.object(
                socket, "getaddrinfo", side_effect=socket.gaierror("nope")
            ):
                email_mod._record_signature_website_advisory(fs, USER_ID, GOOD_PROFILE)
        self.assertEqual(len(fs.writes), 1)
        advisory = fs.writes[0][0]["signatureWebsiteAdvisory"]
        self.assertFalse(advisory["hasBlockingFinding"])
        self.assertEqual(
            {f["severity"] for f in advisory["findings"]}, {"warn"}
        )


class AdvisoryCannotStopASendTests(unittest.TestCase):
    """The assertion that matters most: the advisory is not on the send path."""

    def _run_send_outboxes(self, fs):
        with mock.patch.object(email_mod, "_fs_for", return_value=fs):
            return email_mod.send_outboxes(USER_ID, {"Authorization": "Bearer x"})

    def test_send_outboxes_still_runs_when_the_advisory_explodes(self):
        fs = _FakeFirestore(BAD_PROFILE)
        with _on():
            with mock.patch.object(
                email_mod,
                "_record_signature_website_advisory",
                side_effect=RuntimeError("boom"),
            ):
                with self.assertRaises(RuntimeError):
                    # Sanity: an UNGUARDED raise really would propagate, so the
                    # test below is proving the guard and not the absence of one.
                    self._run_send_outboxes(fs)

        with _on():
            with mock.patch(
                "email_automation.signature_website_validation.signature_website_advisory",
                side_effect=RuntimeError("boom"),
            ):
                states = self._run_send_outboxes(fs)
        self.assertEqual(states, [])

    def test_send_outboxes_still_runs_when_firestore_rejects_the_advisory(self):
        class _WriteOnlyExplodes(_FakeFirestore):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self._seen_users = 0

            def collection(self, name):
                if name == "users":
                    self._seen_users += 1
                    if self._seen_users == 2:  # the advisory's own write
                        raise RuntimeError("firestore rejected the advisory")
                return super().collection(name)

        fs = _WriteOnlyExplodes(BAD_PROFILE)
        with _on():
            states = self._run_send_outboxes(fs)
        self.assertEqual(states, [])

    def test_advisory_runs_but_changes_nothing_about_the_outcome(self):
        clean = _FakeFirestore(GOOD_PROFILE)
        dirty = _FakeFirestore(BAD_PROFILE)
        with _on():
            self.assertEqual(self._run_send_outboxes(clean), self._run_send_outboxes(dirty))


if __name__ == "__main__":
    unittest.main()
