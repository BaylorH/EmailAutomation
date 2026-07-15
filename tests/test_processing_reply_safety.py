import os
from pathlib import Path
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "service-account.json"),
)

from email_automation import processing
from email_automation.campaign_safety import CampaignAutomationDecision


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"Unexpected HTTP status {self.status_code}")


class FakeSnapshot:
    def __init__(self, data=None, exists=True):
        self._data = data or {}
        self.exists = exists

    def to_dict(self):
        return self._data


class FakeUserDoc:
    def __init__(self, user_data, collection_name=None):
        self.user_data = user_data
        self.collection_name = collection_name

    def get(self):
        if self.collection_name == "threads":
            return FakeSnapshot({"clientId": "client-1", "status": "active"})
        if self.collection_name == "clients":
            return FakeSnapshot({"status": "live", "automationPaused": False})
        if self.collection_name == "archivedClients":
            return FakeSnapshot(exists=False)
        return FakeSnapshot(self.user_data)

    def collection(self, name):
        return FakeNestedCollection(self.user_data, name)


class FakeNestedCollection:
    def __init__(self, user_data, name):
        self.user_data = user_data
        self.name = name

    def document(self, _doc_id):
        return FakeUserDoc(self.user_data, self.name)


class FakeUsersCollection:
    def __init__(self, user_data):
        self.user_data = user_data

    def document(self, _user_id):
        return FakeUserDoc(self.user_data)


class FakeFirestore:
    def __init__(self, user_data):
        self.user_data = user_data

    def collection(self, name):
        if name != "users":
            raise AssertionError(f"Unexpected collection {name}")
        return FakeUsersCollection(self.user_data)


class ProcessingReplySafetyTests(unittest.TestCase):
    def test_processing_does_not_import_legacy_email_operations_senders(self):
        source = Path(processing.__file__).read_text()

        self.assertNotIn("from .email_operations import", source)

    def test_send_reply_default_allowlist_is_baylor_only(self):
        with patch.dict(os.environ, {}, clear=True), patch(
            "email_automation.utils.exponential_backoff_request",
            side_effect=AssertionError("Graph should not be touched for non-Baylor default auto-replies"),
        ):
            sent = processing.send_reply_in_thread(
                user_id="C4X3UH1r6QhgP3ivXD1QjyhuGyI2",
                headers={"Authorization": "Bearer token"},
                body="Hi Alex,\n\nThanks for the update.",
                current_msg_id="message-1",
                recipient="broker@example.com",
                thread_id="thread-1",
            )

        self.assertFalse(sent)
        self.assertEqual("blocked_auto_reply_policy", processing.send_reply_in_thread.last_outcome)

    def test_send_reply_blocks_non_allowlisted_auto_reply_before_graph_request(self):
        with patch.dict(os.environ, {"SITESIFT_AUTO_REPLY_ALLOWLIST": "NO7lVYVp6BaplKYEfMlWCgBnpdh2"}), patch(
            "email_automation.utils.exponential_backoff_request",
            side_effect=AssertionError("Graph should not be touched for non-allowlisted auto-replies"),
        ):
            sent = processing.send_reply_in_thread(
                user_id="regular-user",
                headers={"Authorization": "Bearer token"},
                body="Hi Alex,\n\nThanks for the update.",
                current_msg_id="message-1",
                recipient="broker@example.com",
                thread_id="thread-1",
            )

        self.assertFalse(sent)
        self.assertEqual("blocked_auto_reply_policy", processing.send_reply_in_thread.last_outcome)
        self.assertIn("Automatic inbox replies are disabled", processing.send_reply_in_thread.last_error)

    def test_auto_reply_allowlist_space_separated_matches_comma_separated(self):
        # Rail 1: an operator who widens the allowlist with a space-separated
        # list must get the same id set as a comma-separated list. The old
        # r"[,\\s]+" character class matched comma/backslash/'s', mangling ids.
        comma = "userAbc,userXyz"
        space = "userAbc userXyz"
        for allowlist in (comma, space):
            with patch.dict(os.environ, {"SITESIFT_AUTO_REPLY_ALLOWLIST": allowlist}):
                self.assertTrue(
                    processing._automatic_inbox_replies_allowed("userAbc"),
                    f"userAbc should be allowed with allowlist {allowlist!r}",
                )
                self.assertTrue(
                    processing._automatic_inbox_replies_allowed("userXyz"),
                    f"userXyz should be allowed with allowlist {allowlist!r}",
                )
                self.assertFalse(
                    processing._automatic_inbox_replies_allowed("userOther"),
                    f"userOther must stay blocked with allowlist {allowlist!r}",
                )

    def test_send_reply_blocks_placeholder_before_graph_request(self):
        with patch(
            "email_automation.utils.exponential_backoff_request",
            side_effect=AssertionError("Graph should not be touched for unsafe reply bodies"),
        ):
            sent = processing.send_reply_in_thread(
                user_id="uid-1",
                headers={"Authorization": "Bearer token"},
                body="Hi [NAME],\n\nThanks for confirming.",
                current_msg_id="message-1",
                recipient="bp21harrison@gmail.com",
                thread_id="thread-1",
            )

        self.assertFalse(sent)
        self.assertEqual("blocked_unsafe_body", processing.send_reply_in_thread.last_outcome)
        self.assertIn("Unresolved outbound placeholder", processing.send_reply_in_thread.last_error)

    def test_send_reply_in_thread_rejects_stale_jill_custom_signature_before_graph_patch(self):
        stale_jill_html = (
            '<div data-sitesift-professional-signature="v1">'
            '<strong>Jill Ames</strong><br>'
            '<a href="mailto:jill.ames@mohrpartners.com">jill.ames@mohrpartners.com</a>'
            '<br>Mohr Partners, Inc.'
            '</div>'
        )
        patched_payloads = []

        def fake_post(url, **_kwargs):
            if url.endswith("/createReplyAll"):
                return FakeResponse(201, {
                    "id": "draft-1",
                    "toRecipients": [{"emailAddress": {"address": "bp21harrison@gmail.com"}}],
                    "ccRecipients": [],
                })
            if url.endswith("/send"):
                return FakeResponse(202, {})
            raise AssertionError(f"Unexpected POST {url}")

        def fake_patch(_url, json=None, **_kwargs):
            patched_payloads.append(json)
            return FakeResponse(204, {})

        current_meta = {
            "conversationId": "conversation-1",
            "subject": "RE: 100 Signature Way",
        }
        sent_message = {
            "internetMessageId": "<sent-1@example.com>",
            "toRecipients": [{"emailAddress": {"address": "bp21harrison@gmail.com"}}],
            "ccRecipients": [],
            "subject": "RE: 100 Signature Way",
            "sentDateTime": "2026-07-01T12:00:00Z",
            "body": {"contentType": "HTML", "content": "Hi Avery"},
            "bodyPreview": "Hi Avery",
        }

        with patch.dict(os.environ, {"SITESIFT_AUTO_REPLY_ALLOWLIST": "uid-1"}), \
             patch.object(processing, "get_client_automation_decision", return_value=CampaignAutomationDecision(
                 state="allow", reason="", client_data={"status": "live"},
                 metadata={"terminal": False, "stopKind": "none"},
             )), \
             patch("email_automation.clients._fs", FakeFirestore({
                 "email": "baylor.freelance@outlook.com",
                 "signatureMode": "custom",
                 "emailSignature": stale_jill_html,
             })), \
             patch("requests.get", return_value=FakeResponse(200, current_meta)), \
             patch("requests.post", side_effect=fake_post), \
             patch("requests.patch", side_effect=fake_patch), \
             patch("email_automation.utils.time.sleep", return_value=None), \
             patch("email_automation.email._hydrate_reply_all_draft_recipients", side_effect=lambda _headers, draft, base=None: draft), \
             patch("email_automation.email._source_message_reply_all_fallback", side_effect=lambda draft, _current_meta: draft), \
             patch("email_automation.email._reviewed_recipient_reply_all_fallback", side_effect=lambda draft, to_emails=None: draft), \
             patch("email_automation.email._filter_reply_all_draft_recipients", return_value={
                 "payload": {
                     "toRecipients": [{"emailAddress": {"address": "bp21harrison@gmail.com"}}],
                     "ccRecipients": [],
                 }
             }), \
             patch("email_automation.processing._find_recent_sent_message_for_conversation", return_value=sent_message), \
             patch("email_automation.messaging.index_message_id", return_value=True), \
             patch("email_automation.messaging.lookup_thread_by_message_id", return_value="thread-1"), \
             patch("email_automation.messaging.save_message"), \
             patch("email_automation.messaging.index_conversation_id", return_value=True), \
             patch("email_automation.processing.time", SimpleNamespace(sleep=lambda _seconds: None)):
            sent = processing.send_reply_in_thread(
                user_id="uid-1",
                headers={"Authorization": "Bearer token"},
                body="Hi Avery,\n\nCould you confirm the rate?",
                current_msg_id="message-1",
                recipient="bp21harrison@gmail.com",
                thread_id="thread-1",
            )

        self.assertTrue(sent)
        self.assertEqual("sent_indexed", processing.send_reply_in_thread.last_outcome)
        self.assertEqual(1, len(patched_payloads))
        html_body = patched_payloads[0]["body"]["content"]
        self.assertIn("Hi Avery", html_body)
        self.assertNotIn("Jill Ames", html_body)
        self.assertNotIn("jill.ames@mohrpartners.com", html_body)
        self.assertNotIn("Mohr Partners, Inc.", html_body)

    def test_send_reply_classifies_all_opted_out_recipients_as_suppressed(self):
        def fake_post(url, **_kwargs):
            if url.endswith("/createReplyAll"):
                return FakeResponse(201, {
                    "id": "draft-1",
                    "toRecipients": [{"emailAddress": {"address": "bp21harrison@gmail.com"}}],
                    "ccRecipients": [],
                })
            raise AssertionError(f"Unexpected POST {url}")

        current_meta = {
            "conversationId": "conversation-1",
            "subject": "RE: 100 Optout Way",
        }
        recipient_result = {
            "payload": {"toRecipients": [], "ccRecipients": []},
            "skipped": {
                "optedOut": [{
                    "email": "bp21harrison@gmail.com",
                    "reason": "broker_opt_out",
                }]
            },
        }

        with patch.dict(os.environ, {"SITESIFT_AUTO_REPLY_ALLOWLIST": "uid-1"}), \
             patch.object(processing, "get_client_automation_decision", return_value=CampaignAutomationDecision(
                 state="allow", reason="", client_data={"status": "live"},
                 metadata={"terminal": False, "stopKind": "none"},
             )), \
             patch("email_automation.clients._fs", FakeFirestore({
                 "email": "baylor.freelance@outlook.com",
             })), \
             patch("requests.get", return_value=FakeResponse(200, current_meta)), \
             patch("requests.post", side_effect=fake_post), \
             patch("requests.patch", side_effect=AssertionError("Suppressed reply must not be patched")), \
             patch("email_automation.email._hydrate_reply_all_draft_recipients", side_effect=lambda _headers, draft, base=None: draft), \
             patch("email_automation.email._source_message_reply_all_fallback", side_effect=lambda draft, _current_meta: draft), \
             patch("email_automation.email._reviewed_recipient_reply_all_fallback", side_effect=lambda draft, to_emails=None: draft), \
             patch("email_automation.email._filter_reply_all_draft_recipients", return_value=recipient_result), \
             patch("email_automation.email._delete_graph_reply_draft") as delete_draft:
            sent = processing.send_reply_in_thread(
                user_id="uid-1",
                headers={"Authorization": "Bearer token"},
                body="Hi Avery,\n\nThanks for the update.",
                current_msg_id="message-1",
                recipient="bp21harrison@gmail.com",
                thread_id="thread-1",
            )

        self.assertFalse(sent)
        self.assertEqual(
            "suppressed_recipient_optout",
            processing.send_reply_in_thread.last_outcome,
        )
        self.assertIn("opted out", processing.send_reply_in_thread.last_error.lower())
        delete_draft.assert_called_once()
        self.assertEqual("draft-1", delete_draft.call_args.args[1])

    def test_tour_actions_default_allowlist_is_baylor_only(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(processing._tour_actions_allowed("NO7lVYVp6BaplKYEfMlWCgBnpdh2"))
            self.assertFalse(processing._tour_actions_allowed("ntR8ACrAgEcZ1i5FWyi6MFuCJfI2"))

    def test_tour_actions_explicit_allowlist_supports_test_lane(self):
        with patch.dict(os.environ, {"SITESIFT_TOUR_ACTION_ALLOWLIST": "test-user, other-user"}):
            self.assertTrue(processing._tour_actions_allowed("test-user"))
            self.assertFalse(processing._tour_actions_allowed("regular-user"))

    def test_tour_actions_wildcard_is_explicit_only(self):
        with patch.dict(os.environ, {"SITESIFT_TOUR_ACTION_ALLOWLIST": "*"}):
            self.assertTrue(processing._tour_actions_allowed("regular-user"))


if __name__ == "__main__":
    unittest.main()
