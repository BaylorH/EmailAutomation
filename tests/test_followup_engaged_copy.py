"""The follow-up nudge must not tell an engaged broker that he never replied.

FDR-061 measured 104 follow-up nudges over the whole stored corpus and found 10
that landed on a thread where that broker had ALREADY replied -- one who had
replied seven times and one five times were both told "I understand you're
busy", and one who had replied twice received the final nudge that presumes
disinterest and closes the door.

The defect is entirely in the COPY. The scheduler's resume-after-silence is
correct and is not touched here.

The trap this file exists to hold shut: the default body is produced by ONE
resolver called from THREE places, and TWO of them are integrity checks that
recompute the expected body and REJECT a send whose body differs. One of those
two (the sealed send envelope) can only see the thread/config it decodes back
out of the send identity -- it never sees the live thread document. So the
"has this broker replied" input has to be durable, has to live where the
identity carries it, and has to be readable identically at all three sites, or
every affected follow-up is refused as tampered and stops going out at all.
"""

import os
import unittest

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "service-account.json",
    ),
)

from email_automation import followup


SILENCE_PHRASES = (
    "I understand you're busy",
    "I wanted to follow up on my previous email",
    "Just a quick check-in on my earlier emails",
    "I'll assume this one isn't a fit",
)


def _engaged_config(followups=None, *, marker: object = True):
    config = {
        "enabled": True,
        "currentFollowUpIndex": 0,
        "followUps": followups if followups is not None else [{}, {}, {}],
        "conversationStage": "initial",
    }
    if marker is not None:
        config["brokerHasReplied"] = marker
    return config


class DefaultFollowupCopyTests(unittest.TestCase):
    def test_silent_copy_is_unchanged_for_a_broker_who_never_replied(self):
        first = followup._get_default_followup_message(0)
        final = followup._get_default_followup_message(2)
        self.assertIn("I wanted to follow up on my previous email", first)
        self.assertIn("I'll assume this one isn't a fit", final)

    def test_engaged_copy_never_claims_the_broker_was_silent(self):
        for index in range(3):
            body = followup._get_default_followup_message(index, engaged=True)
            for phrase in SILENCE_PHRASES:
                self.assertNotIn(
                    phrase,
                    body,
                    f"engaged follow-up {index} still assumes silence: {phrase!r}",
                )

    def test_engaged_final_nudge_drops_the_disinterest_presumption(self):
        final = followup._get_default_followup_message(2, engaged=True)
        self.assertNotIn("isn't a fit for my client's needs", final)
        self.assertNotIn("assume", final.lower())

    def test_engaged_copy_acknowledges_the_prior_exchange(self):
        for index in range(3):
            body = followup._get_default_followup_message(index, engaged=True)
            self.assertTrue(
                "back to me" in body
                or "your replies" in body
                or "our exchange" in body,
                f"engaged follow-up {index} does not acknowledge the prior exchange",
            )

    def test_engaged_copy_keeps_the_greeting_placeholder(self):
        for index in range(3):
            self.assertIn(
                "[NAME]",
                followup._get_default_followup_message(index, engaged=True),
            )

    def test_an_index_past_the_end_still_returns_the_engaged_final_body(self):
        self.assertEqual(
            followup._get_default_followup_message(9, engaged=True),
            followup._get_default_followup_message(2, engaged=True),
        )


class BrokerHasRepliedMarkerTests(unittest.TestCase):
    def test_absent_marker_reads_as_never_replied(self):
        self.assertFalse(followup._followup_broker_has_replied({}))
        self.assertFalse(followup._followup_broker_has_replied(None))

    def test_marker_must_be_exactly_true(self):
        self.assertTrue(
            followup._followup_broker_has_replied({"brokerHasReplied": True})
        )
        self.assertFalse(
            followup._followup_broker_has_replied({"brokerHasReplied": False})
        )

    def test_a_truthy_non_bool_from_untrusted_stored_config_does_not_flip_copy(self):
        for value in ("true", "yes", 1, [1], {"a": 1}):
            self.assertFalse(
                followup._followup_broker_has_replied({"brokerHasReplied": value}),
                f"stored config value {value!r} must not read as a reply",
            )

    def test_the_marker_is_a_durable_config_field_not_a_runtime_one(self):
        # A runtime field is stripped out of the durable config, which is the
        # only config the sealed send envelope can decode back out.
        self.assertNotIn("brokerHasReplied", followup._FOLLOWUP_CONFIG_RUNTIME_FIELDS)
        durable = followup._followup_durable_config(_engaged_config())
        self.assertIs(durable.get("brokerHasReplied"), True)


class ResolvedBodyTests(unittest.TestCase):
    def test_resolver_returns_engaged_copy_when_the_marker_is_set(self):
        engaged = followup._resolve_followup_message(_engaged_config(), 0, "Dana")
        silent = followup._resolve_followup_message(
            _engaged_config(marker=None), 0, "Dana"
        )
        self.assertNotEqual(engaged, silent)
        self.assertNotIn("I understand you're busy", engaged)
        self.assertIn("Dana", engaged)

    def test_a_client_supplied_message_is_never_rewritten(self):
        custom = "Hi [NAME], any update on the space?"
        config = _engaged_config(followups=[{"message": custom}, {}, {}])
        self.assertEqual(
            followup._resolve_followup_message(config, 0, "Dana"),
            "Hi Dana, any update on the space?",
        )


class ThreeCallSitesAgreeTests(unittest.TestCase):
    """The whole reason this was not fixed at 3am.

    If the send site can see the reply state and an integrity check cannot, the
    check recomputes the SILENT body, compares it against the ENGAGED body that
    was actually composed, and rejects the send as tampered.
    """

    def _thread(self):
        return {
            "clientId": "client-1",
            "email": ["broker@example.test"],
            "contactName": "Dana",
            "ccEmails": [],
            "ccRecipients": [],
            "rowNumber": 7,
        }

    def test_the_sealed_identity_carries_the_marker_back_out(self):
        thread = self._thread()
        config = _engaged_config()
        identity = followup._followup_send_identity(thread, config, 0)
        decoded = followup._followup_inputs_from_send_identity(identity)
        self.assertIsNotNone(
            decoded,
            "the send identity no longer rebuilds from its own proofs",
        )
        _identity_thread, identity_config, identity_index = decoded
        self.assertTrue(followup._followup_broker_has_replied(identity_config))
        self.assertEqual(
            followup._resolve_followup_message(identity_config, identity_index, "Dana"),
            followup._resolve_followup_message(config, 0, "Dana"),
        )

    def test_envelope_integrity_accepts_an_engaged_body(self):
        thread = self._thread()
        config = _engaged_config()
        identity = followup._followup_send_identity(thread, config, 0)
        body = followup._resolve_followup_message(config, 0, thread["contactName"])
        marker = self._sealed(identity, body)
        self.assertTrue(
            followup._followup_send_envelope_is_complete(
                marker,
                expected_identity=identity,
            ),
            "an engaged follow-up body is refused as tampered by its own envelope",
        )

    def test_envelope_integrity_still_rejects_a_body_that_was_swapped(self):
        thread = self._thread()
        config = _engaged_config()
        identity = followup._followup_send_identity(thread, config, 0)
        silent_body = followup._resolve_followup_message(
            _engaged_config(marker=None), 0, thread["contactName"]
        )
        marker = self._sealed(identity, silent_body)
        self.assertFalse(
            followup._followup_send_envelope_is_complete(
                marker,
                expected_identity=identity,
            )
        )

    def _sealed(self, identity, body):
        from datetime import datetime, timedelta, timezone

        now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
        return followup._seal_followup_send_envelope({
            "id": "followup-attempt-test",
            "state": "sending",
            "owner": "worker-1",
            "index": 0,
            "createdAt": now,
            "sendStartedAt": now,
            "leaseUntil": now + timedelta(seconds=600),
            "sendIdentity": identity,
            "configFingerprint": identity.get("configFingerprint"),
            "clientId": identity.get("clientId"),
            "recipient": "broker@example.test",
            "body": body,
            "subject": "Follow-up",
            "conversationId": "conversation-1",
            "draftId": None,
            "toRecipients": ["broker@example.test"],
            "ccRecipients": [],
        })


class MarkerDurabilityTests(unittest.TestCase):
    """The marker must survive the resume path, which clears the obvious flag."""

    def test_resume_sets_the_marker_and_does_not_clear_it(self):
        from datetime import datetime, timedelta, timezone
        from unittest.mock import patch

        long_ago = datetime.now(timezone.utc) - timedelta(days=9)

        class _Stamp:
            def timestamp(self):
                return long_ago.timestamp()

        thread_data = {
            "followUpStatus": "paused",
            "lastInboundAt": _Stamp(),
            "hasInboundReply": True,
            "followUpConfig": {
                "enabled": True,
                "currentFollowUpIndex": 0,
                "followUps": [{}, {}, {}],
            },
        }
        ref = _FakeRef(thread_data)
        with patch.object(followup, "_fs", _FakeFirestore(ref)):
            self.assertTrue(followup.resume_followup_if_silent("user-1", "thread-1"))

        update = ref.updates[-1]
        self.assertIs(
            update.get("followUpConfig.brokerHasReplied"),
            True,
            "the resume path is the one that produced all ten damaged nudges; "
            "it must mark the thread as engaged",
        )
        self.assertIs(update.get("hasInboundReply"), False)

    def test_recording_an_inbound_reply_sets_the_marker(self):
        from unittest.mock import patch

        thread_data = {
            "followUpStatus": "waiting",
            "status": "active",
            "followUpConfig": {
                "enabled": True,
                "currentFollowUpIndex": 0,
                "followUps": [{}, {}, {}],
            },
        }
        ref = _FakeRef(thread_data)

        def _passthrough_transactional(fn):
            return fn

        with patch.object(followup, "_fs", _FakeFirestore(ref)), patch(
            "google.cloud.firestore.transactional",
            _passthrough_transactional,
        ):
            followup.cancel_followup_on_response("user-1", "thread-1")

        update = ref.updates[-1]
        self.assertIs(update.get("hasInboundReply"), True)
        self.assertIs(update.get("followUpConfig.brokerHasReplied"), True)

    def test_auto_response_reschedule_sets_the_marker(self):
        from unittest.mock import patch

        thread_data = {
            "followUpStatus": "paused",
            "status": "active",
            "followUpConfig": {
                "enabled": True,
                "currentFollowUpIndex": 0,
                "followUps": [{}, {}, {}],
            },
        }
        ref = _FakeRef(thread_data)
        with patch.object(followup, "_fs", _FakeFirestore(ref)):
            self.assertTrue(
                followup.schedule_followup_after_auto_response("user-1", "thread-1")
            )

        update = ref.updates[-1]
        self.assertIs(update.get("followUpConfig.brokerHasReplied"), True)
        self.assertIs(update.get("hasInboundReply"), False)


class WholesaleConfigWriteTests(unittest.TestCase):
    """Scheduling a fresh sequence replaces the whole config. A thread survives
    a property replacement, so that write must not erase the engagement fact.
    """

    def _schedule(self, existing_thread):
        from unittest.mock import patch

        ref = _FakeRef(existing_thread)
        with patch.object(followup, "_fs", _FakeFirestore(ref)), patch.object(
            followup, "firestore_for", lambda runtime, default: _FakeFirestore(ref)
        ):
            followup.schedule_followup_for_thread(
                "user-1",
                "thread-1",
                {"enabled": True, "followUps": [{"waitTime": 3, "waitUnit": "days"}]},
            )
        return ref.updates[-1]

    def test_a_replacement_sequence_keeps_an_earlier_engagement_marker(self):
        update = self._schedule({
            "followUpConfig": {
                "enabled": True,
                "followUps": [{}],
                "currentFollowUpIndex": 1,
                "brokerHasReplied": True,
            },
        })
        self.assertIs(update["followUpConfig"]["brokerHasReplied"], True)
        # and the sequence itself really did reset
        self.assertEqual(update["followUpConfig"]["currentFollowUpIndex"], 0)

    def test_a_first_sequence_carries_no_marker(self):
        update = self._schedule({})
        self.assertNotIn("brokerHasReplied", update["followUpConfig"])

    def test_a_stale_rejection_config_is_still_replaced_wholesale(self):
        update = self._schedule({
            "followUpConfig": {
                "enabled": False,
                "invalidReason": "followUps has 99 steps (max 10)",
                "rejectedAt": "2026-08-01T00:00:00Z",
            },
        })
        self.assertNotIn("invalidReason", update["followUpConfig"])
        self.assertNotIn("rejectedAt", update["followUpConfig"])
        self.assertIs(update["followUpConfig"]["enabled"], True)


class LateReplyPatchTests(unittest.TestCase):
    def test_a_late_reply_after_exhaustion_also_marks_the_thread_engaged(self):
        from email_automation import processing

        patch_data = processing._late_reply_after_followup_exhaustion_patch(
            {
                "status": processing.THREAD_STATUS["stopped"],
                "statusReason": "max_followups_reached",
            },
            message_text="Sorry for the delay - here are the specs.",
            has_attachments=False,
        )
        self.assertIsNotNone(patch_data)
        self.assertIs(patch_data.get("hasInboundReply"), True)
        # set(merge=True) plus an in-memory dict.update(): a dotted key would
        # become a literal field name, so the marker rides a whole config map.
        self.assertNotIn("followUpConfig.brokerHasReplied", patch_data)
        self.assertIs(
            patch_data["followUpConfig"].get("brokerHasReplied"),
            True,
        )

    def test_the_late_reply_patch_preserves_the_rest_of_the_config(self):
        from email_automation import processing

        patch_data = processing._late_reply_after_followup_exhaustion_patch(
            {
                "status": processing.THREAD_STATUS["stopped"],
                "statusReason": "max_followups_reached",
                "followUpConfig": {
                    "enabled": True,
                    "followUps": [{}, {}, {}],
                    "currentFollowUpIndex": 2,
                },
            },
            message_text="Sorry for the delay - here are the specs.",
            has_attachments=False,
        )
        config = patch_data["followUpConfig"]
        self.assertEqual(config["currentFollowUpIndex"], 2)
        self.assertEqual(len(config["followUps"]), 3)
        self.assertIs(config["brokerHasReplied"], True)


class _FakeSnapshot:
    def __init__(self, data):
        self.exists = True
        self._data = data

    def to_dict(self):
        return dict(self._data)


class _FakeRef:
    def __init__(self, data):
        self._data = data
        self.updates = []

    def update(self, data):
        self.updates.append(dict(data))

    def get(self, transaction=None):
        return _FakeSnapshot(self._data)


class _FakeTransaction:
    def update(self, ref, data):
        ref.update(data)


class _FakeFirestore:
    def __init__(self, ref):
        self._ref = ref

    def collection(self, _name):
        return self

    def document(self, _name):
        return self

    def get(self, transaction=None):
        return self._ref.get(transaction=transaction)

    def update(self, data):
        self._ref.update(data)

    def transaction(self):
        return _FakeTransaction()


if __name__ == "__main__":
    unittest.main()
