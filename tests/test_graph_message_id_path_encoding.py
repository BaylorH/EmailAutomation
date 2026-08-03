import ast
from collections import Counter
import os
from pathlib import Path
import unittest
from urllib.parse import quote
from unittest.mock import call, patch


os.environ.setdefault("E2E_TEST_MODE", "true")

from email_automation import email, processing


class FakeResponse:
    def __init__(self, status_code=204, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class FakeSnapshot:
    def __init__(self, data):
        self._data = data
        self.exists = True

    def to_dict(self):
        return self._data


class FakeDocument:
    def __init__(self, user_data, collection_name=None):
        self._user_data = user_data
        self._collection_name = collection_name

    def get(self):
        if self._collection_name == "threads":
            return FakeSnapshot({"clientId": "client-1", "status": "active"})
        return FakeSnapshot(self._user_data)

    def collection(self, name):
        return FakeCollection(self._user_data, name)


class FakeCollection:
    def __init__(self, user_data, name):
        self._user_data = user_data
        self._name = name

    def document(self, _document_id):
        return FakeDocument(self._user_data, self._name)


class FakeFirestore:
    def __init__(self, user_data):
        self._user_data = user_data

    def collection(self, name):
        if name != "users":
            raise AssertionError(f"Unexpected Firestore collection {name}")
        return FakeCollection(self._user_data, name)


class GraphMessageIdPathEncodingTests(unittest.TestCase):
    def test_graph_message_path_segment_quotes_special_ids_once_and_leaves_ordinary_ids_unchanged(self):
        special_id = "immutable/draft+1"

        self.assertTrue(
            hasattr(processing, "_graph_message_path_segment"),
            "processing must expose one centralized Graph message-ID segment encoder",
        )
        with patch.object(email, "quote", wraps=quote) as quote_segment:
            encoded_id = processing._graph_message_path_segment(special_id)

        self.assertEqual(quote(special_id, safe=""), encoded_id)
        quote_segment.assert_called_once_with(special_id, safe="")
        self.assertNotIn(
            "%252F",
            encoded_id,
        )
        self.assertEqual(
            "ordinary-message-id",
            processing._graph_message_path_segment("ordinary-message-id"),
        )

    def test_every_processing_message_url_encodes_its_id_at_the_path_boundary(self):
        tree = ast.parse(Path(processing.__file__).read_text())
        message_segments = []
        endpoint_suffixes = []

        for node in ast.walk(tree):
            if not isinstance(node, ast.JoinedStr):
                continue
            for index, value in enumerate(node.values[:-1]):
                if not (
                    isinstance(value, ast.Constant)
                    and isinstance(value.value, str)
                    and value.value.endswith("/me/messages/")
                ):
                    continue
                message_segments.append((node.lineno, node.values[index + 1]))
                suffix = ""
                if (
                    index + 2 < len(node.values)
                    and isinstance(node.values[index + 2], ast.Constant)
                    and isinstance(node.values[index + 2].value, str)
                ):
                    suffix = node.values[index + 2].value
                endpoint_suffixes.append(suffix)

        self.assertEqual(14, len(message_segments))
        self.assertEqual(
            Counter(
                {
                    "": 8,
                    "/createReplyAll": 2,
                    "/attachments": 2,
                    "/send": 2,
                }
            ),
            Counter(endpoint_suffixes),
        )
        for lineno, segment in message_segments:
            with self.subTest(lineno=lineno):
                self.assertIsInstance(segment, ast.FormattedValue)
                self.assertIsInstance(segment.value, ast.Call)
                self.assertIsInstance(segment.value.func, ast.Name)
                self.assertEqual(
                    "_graph_message_path_segment",
                    segment.value.func.id,
                )
                self.assertEqual(1, len(segment.value.args))
                self.assertEqual([], segment.value.keywords)

    def test_delete_graph_reply_draft_encodes_special_id_once_and_preserves_ordinary_id(self):
        delete_urls = []

        def fake_delete(url, **_kwargs):
            delete_urls.append(url)
            return FakeResponse()

        with patch.object(
            email,
            "exponential_backoff_request",
            side_effect=lambda operation, **_kwargs: operation(),
        ), patch.object(
            email,
            "quote",
            wraps=quote,
        ) as quote_segment, patch.object(
            email.requests,
            "delete",
            side_effect=fake_delete,
        ):
            self.assertTrue(
                email._delete_graph_reply_draft(
                    {"Authorization": "Bearer test"},
                    "immutable/draft+1",
                )
            )
            self.assertTrue(
                email._delete_graph_reply_draft(
                    {"Authorization": "Bearer test"},
                    "ordinary-draft-id",
                )
            )

        self.assertEqual(
            [
                "https://graph.microsoft.com/v1.0/me/messages/immutable%2Fdraft%2B1",
                "https://graph.microsoft.com/v1.0/me/messages/ordinary-draft-id",
            ],
            delete_urls,
        )
        self.assertNotIn("%252F", delete_urls[0])
        self.assertEqual(
            [
                call("immutable/draft+1", safe=""),
                call("ordinary-draft-id", safe=""),
            ],
            quote_segment.call_args_list,
        )

    def test_reply_flow_preserves_endpoints_and_encodes_each_source_and_draft_segment_once(self):
        cases = (
            (
                "immutable/source+1",
                "immutable/draft+1",
                "immutable%2Fsource%2B1",
                "immutable%2Fdraft%2B1",
            ),
            (
                "ordinary-source-id",
                "ordinary-draft-id",
                "ordinary-source-id",
                "ordinary-draft-id",
            ),
        )
        attachment = {
            "@odata.type": "#microsoft.graph.fileAttachment",
            "name": "signature.png",
            "contentType": "image/png",
            "contentBytes": "YWJj",
            "isInline": True,
            "contentId": "signature-image",
        }

        for source_id, draft_id, encoded_source, encoded_draft in cases:
            with self.subTest(source_id=source_id, draft_id=draft_id):
                get_urls = []
                post_urls = []
                patch_urls = []

                def fake_get(url, **_kwargs):
                    get_urls.append(url)
                    return FakeResponse(
                        200,
                        {
                            "conversationId": "conversation-1",
                            "subject": "RE: Path encoding",
                        },
                    )

                def fake_post(url, **_kwargs):
                    post_urls.append(url)
                    if url.endswith("/createReplyAll"):
                        return FakeResponse(
                            201,
                            {
                                "id": draft_id,
                                "subject": "RE: Path encoding",
                                "toRecipients": [
                                    {
                                        "emailAddress": {
                                            "address": "recipient@example.com",
                                        }
                                    }
                                ],
                                "ccRecipients": [],
                            },
                        )
                    if url.endswith("/attachments"):
                        return FakeResponse(201, {"id": "attachment-1"})
                    if url.endswith("/send"):
                        return FakeResponse(202)
                    raise AssertionError(f"Unexpected Graph POST {url}")

                def fake_patch(url, **_kwargs):
                    patch_urls.append(url)
                    return FakeResponse(204)

                allow = type(
                    "AllowDecision",
                    (),
                    {"denies_autonomous_work": False},
                )()
                recipient_payload = {
                    "payload": {
                        "toRecipients": [
                            {
                                "emailAddress": {
                                    "address": "recipient@example.com",
                                }
                            }
                        ],
                        "ccRecipients": [],
                    },
                    "skipped": {},
                }

                with patch.dict(
                    os.environ,
                    {
                        "SITESIFT_AUTO_REPLY_ALLOWLIST": "uid-1",
                        "SITESIFT_OUTBOUND_MODE": "live",
                    },
                ), patch.object(
                    processing,
                    "get_client_automation_decision",
                    return_value=allow,
                ), patch(
                    "email_automation.clients._fs",
                    FakeFirestore(
                        {
                            "email": "sender@example.com",
                            "signatureMode": "custom",
                            "emailSignature": "signature",
                        }
                    ),
                ), patch(
                    "email_automation.utils.exponential_backoff_request",
                    side_effect=lambda request_callable, **_kwargs: request_callable(),
                ), patch(
                    "email_automation.utils.resolve_signature_settings",
                    return_value=("signature", "custom", "sender@example.com"),
                ), patch(
                    "email_automation.utils.needs_signature_attachments",
                    return_value=True,
                ), patch(
                    "email_automation.utils.get_signature_attachments",
                    return_value=[attachment],
                ), patch.object(
                    processing,
                    "validate_graph_draft_attachment_plan",
                    return_value=1,
                ), patch.object(
                    processing.requests,
                    "get",
                    side_effect=fake_get,
                ), patch.object(
                    processing.requests,
                    "post",
                    side_effect=fake_post,
                ), patch.object(
                    processing.requests,
                    "patch",
                    side_effect=fake_patch,
                ), patch(
                    "email_automation.email._hydrate_reply_all_draft_recipients",
                    side_effect=lambda _headers, draft, base=None: draft,
                ), patch(
                    "email_automation.email._source_message_reply_all_fallback",
                    side_effect=lambda draft, _current_meta: draft,
                ), patch(
                    "email_automation.email._reviewed_recipient_reply_all_fallback",
                    side_effect=lambda draft, to_emails=None: draft,
                ), patch(
                    "email_automation.email._filter_reply_all_draft_recipients",
                    return_value=recipient_payload,
                ), patch.object(
                    processing,
                    "find_exact_sent_message_by_immutable_id",
                    return_value=None,
                ), patch.object(processing.time, "sleep", return_value=None):
                    sent = processing.send_reply_in_thread(
                        user_id="uid-1",
                        headers={"Authorization": "Bearer test"},
                        body="Hi there,\n\nThanks for the update.",
                        current_msg_id=source_id,
                        recipient="recipient@example.com",
                        thread_id="thread-1",
                    )

                self.assertFalse(sent)
                self.assertEqual(
                    [
                        "https://graph.microsoft.com/v1.0/me/messages/"
                        f"{encoded_source}",
                    ],
                    get_urls,
                )
                self.assertEqual(
                    [
                        "https://graph.microsoft.com/v1.0/me/messages/"
                        f"{encoded_source}/createReplyAll",
                        "https://graph.microsoft.com/v1.0/me/messages/"
                        f"{encoded_draft}/attachments",
                        "https://graph.microsoft.com/v1.0/me/messages/"
                        f"{encoded_draft}/send",
                    ],
                    post_urls,
                )
                self.assertEqual(
                    [
                        "https://graph.microsoft.com/v1.0/me/messages/"
                        f"{encoded_draft}",
                    ],
                    patch_urls,
                )
                self.assertFalse(
                    any("%252F" in url for url in get_urls + post_urls + patch_urls)
                )

    def test_sparse_reply_flow_encodes_real_draft_hydration_get_and_retains_raw_id(self):
        source_id = "immutable/source+sparse"
        draft_id = "immutable/draft+sparse"
        encoded_source = "immutable%2Fsource%2Bsparse"
        encoded_draft = "immutable%2Fdraft%2Bsparse"
        get_urls = []
        post_urls = []
        patch_urls = []
        filtered_drafts = []

        def fake_get(url, **_kwargs):
            get_urls.append(url)
            if url.endswith(f"/{encoded_source}"):
                return FakeResponse(
                    200,
                    {
                        "conversationId": "conversation-sparse",
                        "subject": "RE: Sparse path encoding",
                    },
                )
            if url.endswith(f"/{encoded_draft}") or url.endswith(f"/{draft_id}"):
                return FakeResponse(
                    200,
                    {
                        "id": draft_id,
                        "subject": "RE: Sparse path encoding",
                        "toRecipients": [
                            {
                                "emailAddress": {
                                    "address": "recipient@example.com",
                                }
                            }
                        ],
                        "ccRecipients": [],
                    },
                )
            raise AssertionError(f"Unexpected Graph GET {url}")

        def fake_post(url, **_kwargs):
            post_urls.append(url)
            if url.endswith("/createReplyAll"):
                return FakeResponse(201, {"id": draft_id})
            if url.endswith("/send"):
                return FakeResponse(202)
            raise AssertionError(f"Unexpected Graph POST {url}")

        def fake_patch(url, **_kwargs):
            patch_urls.append(url)
            return FakeResponse(204)

        def fake_filter(_user_id, draft, **_kwargs):
            filtered_drafts.append(dict(draft))
            return {
                "payload": {
                    "toRecipients": [
                        {
                            "emailAddress": {
                                "address": "recipient@example.com",
                            }
                        }
                    ],
                    "ccRecipients": [],
                },
                "skipped": {},
            }

        allow = type(
            "AllowDecision",
            (),
            {"denies_autonomous_work": False},
        )()
        with patch.dict(
            os.environ,
            {
                "SITESIFT_AUTO_REPLY_ALLOWLIST": "uid-1",
                "SITESIFT_OUTBOUND_MODE": "live",
            },
        ), patch.object(
            processing,
            "get_client_automation_decision",
            return_value=allow,
        ), patch.object(
            processing,
            "format_email_body_with_footer",
            return_value="<p>Sparse path encoding</p>",
        ), patch(
            "email_automation.clients._fs",
            FakeFirestore({"email": "sender@example.com"}),
        ), patch(
            "email_automation.utils.exponential_backoff_request",
            side_effect=lambda request_callable, **_kwargs: request_callable(),
        ), patch.object(
            email,
            "exponential_backoff_request",
            side_effect=lambda request_callable, **_kwargs: request_callable(),
        ), patch(
            "email_automation.utils.resolve_signature_settings",
            return_value=(None, None, "sender@example.com"),
        ), patch(
            "email_automation.utils.needs_signature_attachments",
            return_value=False,
        ), patch.object(
            processing,
            "validate_graph_draft_attachment_plan",
            return_value=0,
        ), patch.object(
            processing.requests,
            "get",
            side_effect=fake_get,
        ), patch.object(
            processing.requests,
            "post",
            side_effect=fake_post,
        ), patch.object(
            processing.requests,
            "patch",
            side_effect=fake_patch,
        ), patch(
            "email_automation.email._source_message_reply_all_fallback",
            side_effect=lambda draft, _current_meta: draft,
        ), patch(
            "email_automation.email._reviewed_recipient_reply_all_fallback",
            side_effect=lambda draft, to_emails=None: draft,
        ), patch(
            "email_automation.email._filter_reply_all_draft_recipients",
            side_effect=fake_filter,
        ), patch.object(
            processing,
            "find_exact_sent_message_by_immutable_id",
            return_value=None,
        ), patch.object(processing.time, "sleep", return_value=None):
            sent = processing.send_reply_in_thread(
                user_id="uid-1",
                headers={"Authorization": "Bearer test"},
                body="Hi there,\n\nThanks for the sparse response.",
                current_msg_id=source_id,
                recipient="recipient@example.com",
                thread_id="thread-1",
            )

        self.assertFalse(sent)
        self.assertEqual(
            [
                "https://graph.microsoft.com/v1.0/me/messages/"
                f"{encoded_source}",
                "https://graph.microsoft.com/v1.0/me/messages/"
                f"{encoded_draft}",
            ],
            get_urls,
        )
        self.assertEqual(draft_id, filtered_drafts[0]["id"])
        self.assertEqual(
            [
                "https://graph.microsoft.com/v1.0/me/messages/"
                f"{encoded_source}/createReplyAll",
                "https://graph.microsoft.com/v1.0/me/messages/"
                f"{encoded_draft}/send",
            ],
            post_urls,
        )
        self.assertEqual(
            [
                "https://graph.microsoft.com/v1.0/me/messages/"
                f"{encoded_draft}",
            ],
            patch_urls,
        )
        self.assertFalse(
            any("%252F" in url for url in get_urls + post_urls + patch_urls)
        )


if __name__ == "__main__":
    unittest.main()
