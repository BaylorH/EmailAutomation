import os
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("E2E_TEST_MODE", "true")

from email_automation import sent_mail_guard


class GraphImmutableSentIdentityTests(unittest.TestCase):
    def test_immutable_prefer_header_is_merged_on_a_copy(self):
        original = {
            "Authorization": "Bearer test",
            "Prefer": 'outlook.body-content-type="text"',
        }

        merged = sent_mail_guard.graph_headers_with_immutable_id(original)

        self.assertIsNot(original, merged)
        self.assertEqual(
            'outlook.body-content-type="text"',
            original["Prefer"],
        )
        self.assertIn('outlook.body-content-type="text"', merged["Prefer"])
        self.assertIn('IdType="ImmutableId"', merged["Prefer"])

    def test_exact_immutable_id_and_is_draft_false_are_required(self):
        response = MagicMock(status_code=200)
        response.json.return_value = {
            "id": "immutable/id+1",
            "isDraft": False,
            "conversationId": "conv-1",
            "toRecipients": [
                {"emailAddress": {"address": "broker@example.test"}},
            ],
            "body": {"contentType": "HTML", "content": "Thanks for this."},
            "internetMessageId": "<sent-1@example.test>",
            "sentDateTime": "2026-08-02T10:00:00Z",
        }

        with patch.object(
            sent_mail_guard.requests,
            "get",
            return_value=response,
        ) as graph_get:
            match = sent_mail_guard.find_exact_sent_message_by_immutable_id(
                {"Authorization": "Bearer test"},
                "immutable/id+1",
                recipient="broker@example.test",
                body="Thanks for this.",
                conversation_id="conv-1",
                attempts=1,
            )

        self.assertEqual("<sent-1@example.test>", match["internetMessageId"])
        request_url = graph_get.call_args.args[0]
        request_headers = graph_get.call_args.kwargs["headers"]
        self.assertTrue(request_url.endswith("/me/messages/immutable%2Fid%2B1"))
        self.assertIn('IdType="ImmutableId"', request_headers["Prefer"])

    def test_draft_or_async_404_never_becomes_exact_sent_evidence(self):
        draft_response = MagicMock(status_code=200)
        draft_response.json.return_value = {
            "id": "immutable-1",
            "isDraft": True,
        }
        missing_response = MagicMock(status_code=404)

        for response in (draft_response, missing_response):
            with self.subTest(status=response.status_code), patch.object(
                sent_mail_guard.requests,
                "get",
                return_value=response,
            ):
                self.assertIsNone(
                    sent_mail_guard.find_exact_sent_message_by_immutable_id(
                        {"Authorization": "Bearer test"},
                        "immutable-1",
                        attempts=1,
                    )
                )

    def test_missing_sent_timestamp_never_becomes_exact_sent_evidence(self):
        response = MagicMock(status_code=200)
        response.json.return_value = {
            "id": "immutable-1",
            "isDraft": False,
            "internetMessageId": "<sent-1@example.test>",
        }

        with patch.object(sent_mail_guard.requests, "get", return_value=response):
            self.assertIsNone(
                sent_mail_guard.find_exact_sent_message_by_immutable_id(
                    {"Authorization": "Bearer test"},
                    "immutable-1",
                    attempts=1,
                )
            )

    def test_exact_id_with_envelope_drift_fails_closed(self):
        response = MagicMock(status_code=200)
        response.json.return_value = {
            "id": "immutable-1",
            "isDraft": False,
            "conversationId": "conv-1",
            "toRecipients": [
                {"emailAddress": {"address": "other@example.test"}},
            ],
            "body": {"contentType": "HTML", "content": "Different body"},
            "sentDateTime": "2026-08-02T10:00:00Z",
        }

        with patch.object(sent_mail_guard.requests, "get", return_value=response):
            with self.assertRaises(sent_mail_guard.SentMailGuardLookupError):
                sent_mail_guard.find_exact_sent_message_by_immutable_id(
                    {"Authorization": "Bearer test"},
                    "immutable-1",
                    recipient="broker@example.test",
                    body="Expected body",
                    conversation_id="conv-1",
                    attempts=1,
                )

    def test_exact_id_with_extra_recipient_or_bcc_fails_closed(self):
        base_message = {
            "id": "immutable-1",
            "isDraft": False,
            "conversationId": "conv-1",
            "toRecipients": [
                {"emailAddress": {"address": "broker@example.test"}},
            ],
            "ccRecipients": [],
            "bccRecipients": [],
            "body": {"contentType": "HTML", "content": "Expected body"},
            "sentDateTime": "2026-08-02T10:00:00Z",
        }
        drifted_messages = (
            {
                **base_message,
                "toRecipients": [
                    *base_message["toRecipients"],
                    {"emailAddress": {"address": "outsider@example.test"}},
                ],
            },
            {
                **base_message,
                "bccRecipients": [
                    {"emailAddress": {"address": "outsider@example.test"}},
                ],
            },
        )

        for message in drifted_messages:
            response = MagicMock(status_code=200)
            response.json.return_value = message
            with self.subTest(message=message), patch.object(
                sent_mail_guard.requests,
                "get",
                return_value=response,
            ):
                with self.assertRaisesRegex(
                    sent_mail_guard.SentMailGuardLookupError,
                    "recipient|bcc|envelope",
                ):
                    sent_mail_guard.find_exact_sent_message_by_immutable_id(
                        {"Authorization": "Bearer test"},
                        "immutable-1",
                        to_recipients=["broker@example.test"],
                        cc_recipients=[],
                        require_no_bcc=True,
                        body="Expected body",
                        conversation_id="conv-1",
                        attempts=1,
                    )

    def test_semantic_body_proof_tracks_links_and_images_but_ignores_wrappers(self):
        prepared_body = (
            '<div style="font-family: Arial"><p>View '
            '<a href="https://safe.example.test/property/1">the property</a>'
            '</p><img src="cid:logo-1" alt="SiteSift logo" style="width: 80px">'
            '</div>'
        )
        benign_graph_body = (
            '<html><body><section><span>View </span>'
            '<a style="color: blue" href="https://safe.example.test/property/1">'
            'the property</a></section>'
            '<img style="height: auto" alt="SiteSift logo" src="cid:logo-1">'
            '</body></html>'
        )
        base_message = {
            "id": "immutable-semantic-body",
            "isDraft": False,
            "conversationId": "conv-1",
            "toRecipients": [
                {"emailAddress": {"address": "broker@example.test"}},
            ],
            "ccRecipients": [],
            "bccRecipients": [],
            "sentDateTime": "2026-08-02T10:00:00Z",
        }
        expected_hash = sent_mail_guard.canonical_graph_body_hash(prepared_body)

        response = MagicMock(status_code=200)
        response.json.return_value = {
            **base_message,
            "body": {"contentType": "HTML", "content": benign_graph_body},
        }
        with patch.object(sent_mail_guard.requests, "get", return_value=response):
            self.assertIsNotNone(
                sent_mail_guard.find_exact_sent_message_by_immutable_id(
                    {"Authorization": "Bearer test"},
                    "immutable-semantic-body",
                    to_recipients=["broker@example.test"],
                    cc_recipients=[],
                    require_no_bcc=True,
                    canonical_body_hash=expected_hash,
                    conversation_id="conv-1",
                    attempts=1,
                )
            )

        drifted_bodies = {
            "href": benign_graph_body.replace(
                "https://safe.example.test/property/1",
                "https://phishing.example.test/property/1",
            ),
            "src": benign_graph_body.replace(
                "cid:logo-1",
                "https://tracker.example.test/pixel.gif",
            ),
            "cid": benign_graph_body.replace("cid:logo-1", "cid:logo-2"),
        }
        for label, actual_body in drifted_bodies.items():
            response = MagicMock(status_code=200)
            response.json.return_value = {
                **base_message,
                "body": {"contentType": "HTML", "content": actual_body},
            }
            with self.subTest(label=label), patch.object(
                sent_mail_guard.requests,
                "get",
                return_value=response,
            ):
                with self.assertRaisesRegex(
                    sent_mail_guard.SentMailGuardLookupError,
                    "canonical body drifted",
                ):
                    sent_mail_guard.find_exact_sent_message_by_immutable_id(
                        {"Authorization": "Bearer test"},
                        "immutable-semantic-body",
                        to_recipients=["broker@example.test"],
                        cc_recipients=[],
                        require_no_bcc=True,
                        canonical_body_hash=expected_hash,
                        conversation_id="conv-1",
                        attempts=1,
                    )

    def test_semantic_body_proof_preserves_visible_text_case(self):
        prepared_body = "<div>Access code AbC123</div>"
        sent_body = "<html><body><p>Access code abc123</p></body></html>"

        self.assertNotEqual(
            sent_mail_guard.canonical_graph_body_hash(prepared_body),
            sent_mail_guard.canonical_graph_body_hash(sent_body),
        )

    def test_semantic_body_proof_preserves_decoded_angle_bracket_visible_text(self):
        prepared_body = "<div>Use   code &lt;Admin&gt;</div>"
        provider_formatted_body = (
            "<html><body><section>Use code &lt;Admin&gt;</section></body></html>"
        )
        omitted_text_body = "<p>Use code</p>"

        self.assertEqual(
            sent_mail_guard.canonical_graph_body_hash(prepared_body),
            sent_mail_guard.canonical_graph_body_hash(provider_formatted_body),
        )
        self.assertNotEqual(
            sent_mail_guard.canonical_graph_body_hash(prepared_body),
            sent_mail_guard.canonical_graph_body_hash(omitted_text_body),
        )

    def test_semantic_body_proof_preserves_decoded_angle_bracket_alt_text(self):
        prepared_body = '<img src="cid:logo-1" alt="Logo   &lt;Admin&gt;">'
        provider_formatted_body = (
            '<html><body><img alt="Logo &lt;Admin&gt;" src="cid:logo-1">'
            "</body></html>"
        )
        omitted_alt_text_body = '<img src="cid:logo-1" alt="Logo">'

        self.assertEqual(
            sent_mail_guard.canonical_graph_body_hash(prepared_body),
            sent_mail_guard.canonical_graph_body_hash(provider_formatted_body),
        )
        self.assertNotEqual(
            sent_mail_guard.canonical_graph_body_hash(prepared_body),
            sent_mail_guard.canonical_graph_body_hash(omitted_alt_text_body),
        )

    def test_semantic_body_proof_normalizes_provider_formatting_and_origin_case(self):
        prepared_body = (
            '<div>Access   code AbC123<a '
            'href="HTTPS://SAFE.Example.Test/CasePath"> Open</a></div>'
        )
        provider_body = (
            '<html><body><section>Access code AbC123<a '
            'href="https://safe.example.test/CasePath"> Open</a>'
            '</section></body></html>'
        )

        self.assertEqual(
            sent_mail_guard.canonical_graph_body_hash(prepared_body),
            sent_mail_guard.canonical_graph_body_hash(provider_body),
        )

    def test_semantic_body_proof_preserves_http_userinfo_case(self):
        prepared_body = (
            '<a href="HTTPS://User:Pass@SAFE.Example.Test:8443/'
            'CasePath?Token=AbC#Frag">Open</a>'
        )
        normalized_origin_body = (
            '<html><body><a href="https://User:Pass@safe.example.test:8443/'
            'CasePath?Token=AbC#Frag">Open</a></body></html>'
        )
        drifted_userinfo_body = (
            '<a href="https://user:pass@safe.example.test:8443/'
            'CasePath?Token=AbC#Frag">Open</a>'
        )

        self.assertEqual(
            sent_mail_guard.canonical_graph_body_hash(prepared_body),
            sent_mail_guard.canonical_graph_body_hash(normalized_origin_body),
        )
        self.assertNotEqual(
            sent_mail_guard.canonical_graph_body_hash(prepared_body),
            sent_mail_guard.canonical_graph_body_hash(drifted_userinfo_body),
        )

    def test_exact_sent_attachment_proof_reads_the_exact_message_collection(self):
        message_response = MagicMock(status_code=200)
        message_response.json.return_value = {
            "id": "immutable-with-attachment",
            "isDraft": False,
            "sentDateTime": "2026-08-02T10:00:00Z",
        }
        attachment = {
            "@odata.type": "#microsoft.graph.fileAttachment",
            "id": "provider-attachment-1",
            "name": "logo.png",
            "contentType": "image/png",
            "contentBytes": "bG9nby1ieXRlcw==",
            "contentId": "logo-1",
            "isInline": True,
        }
        attachment_response = MagicMock(status_code=200)
        attachment_response.json.return_value = {"value": [attachment]}

        def graph_get(url, **_kwargs):
            if url.endswith("/attachments"):
                return attachment_response
            return message_response

        with patch.object(
            sent_mail_guard.requests,
            "get",
            side_effect=graph_get,
        ) as exact_get:
            match = sent_mail_guard.find_exact_sent_message_by_immutable_id(
                {"Authorization": "Bearer test"},
                "immutable-with-attachment",
                require_attachment_proof=True,
                attempts=1,
            )

        self.assertEqual([attachment], match["attachments"])
        self.assertEqual(2, exact_get.call_count)
        self.assertTrue(
            exact_get.call_args_list[1].args[0].endswith(
                "/me/messages/immutable-with-attachment/attachments"
            )
        )

    def test_semantic_body_canonicalization_is_order_tolerant_without_uri_collisions(self):
        ordered = (
            '<img src="cid:Logo-A" '
            'srcset="https://cdn.example.test/logo@2x.png 2x" alt="Logo">'
        )
        reordered = (
            '<img alt="Logo" '
            'srcset="https://cdn.example.test/logo@2x.png 2x" src="cid:Logo-A">'
        )
        self.assertEqual(
            sent_mail_guard.canonical_graph_body_hash(ordered),
            sent_mail_guard.canonical_graph_body_hash(reordered),
        )
        self.assertNotEqual(
            sent_mail_guard.canonical_graph_body_hash(
                '<a href="mailto:broker@example.test?subject=Property-A">Email</a>'
            ),
            sent_mail_guard.canonical_graph_body_hash(
                '<a href="mailto:broker@example.test?subject=Property-B">Email</a>'
            ),
        )
        self.assertNotEqual(
            sent_mail_guard.canonical_graph_body_hash(
                '<img src="cid:Logo-A" alt="Logo">'
            ),
            sent_mail_guard.canonical_graph_body_hash(
                '<img src="cid:logo-a" alt="Logo">'
            ),
        )

    def test_semantic_body_tracks_style_block_urls_and_imports(self):
        prepared = (
            '<style>.hero { background-image: url("cid:Hero-A"); } '
            '@import "https://safe.example.test/email.css";</style>'
            '<div class="hero">Property update</div>'
        )
        benign_formatting = (
            '<style> .hero{background-image:url(cid:Hero-A)} '
            '@import url(https://safe.example.test/email.css); </style>'
            '<section><div class="hero">Property update</div></section>'
        )
        drifted = benign_formatting.replace(
            "https://safe.example.test/email.css",
            "https://tracker.example.test/email.css",
        )

        self.assertEqual(
            sent_mail_guard.canonical_graph_body_hash(prepared),
            sent_mail_guard.canonical_graph_body_hash(benign_formatting),
        )
        self.assertNotEqual(
            sent_mail_guard.canonical_graph_body_hash(prepared),
            sent_mail_guard.canonical_graph_body_hash(drifted),
        )

    def test_exact_sent_attachment_proof_fails_closed_when_unreadable(self):
        message_response = MagicMock(status_code=200)
        message_response.json.return_value = {
            "id": "immutable-with-attachment",
            "isDraft": False,
            "sentDateTime": "2026-08-02T10:00:00Z",
        }
        attachment_response = MagicMock(status_code=503)

        with patch.object(
            sent_mail_guard.requests,
            "get",
            side_effect=(message_response, attachment_response),
        ):
            with self.assertRaisesRegex(
                sent_mail_guard.SentMailGuardLookupError,
                "attachment",
            ):
                sent_mail_guard.find_exact_sent_message_by_immutable_id(
                    {"Authorization": "Bearer test"},
                    "immutable-with-attachment",
                    require_attachment_proof=True,
                    attempts=1,
                )

    def test_expected_conversation_cannot_be_missing_from_exact_sent_copy(self):
        response = MagicMock(status_code=200)
        response.json.return_value = {
            "id": "immutable-1",
            "isDraft": False,
            "toRecipients": [
                {"emailAddress": {"address": "broker@example.test"}},
            ],
            "body": {"contentType": "HTML", "content": "Expected body"},
            "sentDateTime": "2026-08-02T10:00:00Z",
        }

        with patch.object(sent_mail_guard.requests, "get", return_value=response):
            with self.assertRaisesRegex(
                sent_mail_guard.SentMailGuardLookupError,
                "conversation drifted",
            ):
                sent_mail_guard.find_exact_sent_message_by_immutable_id(
                    {"Authorization": "Bearer test"},
                    "immutable-1",
                    recipient="broker@example.test",
                    body="Expected body",
                    conversation_id="conv-1",
                    attempts=1,
                )


if __name__ == "__main__":
    unittest.main()
