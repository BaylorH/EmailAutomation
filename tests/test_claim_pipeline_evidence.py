import unittest
from dataclasses import FrozenInstanceError

from email_automation.claim_pipeline.contracts import (
    Actor,
    ActorRole,
    Direction,
    EvidenceFreshness,
    EvidenceSource,
)
from email_automation.claim_pipeline.evidence import (
    EvidenceFailure,
    ExternalEvidenceInput,
    RawMessageEvidence,
    normalize_message_evidence,
)


ACTOR = Actor("Alex Broker", "alex@example.com", ActorRole.BROKER)


def raw_message(**overrides):
    values = {
        "tenant_id": "uid-1",
        "campaign_id": "campaign-1",
        "message_id": "message-1",
        "direction": Direction.INBOUND,
        "actor": ACTOR,
        "observed_at": "2026-07-22T12:00:00Z",
        "subject": "RE: 123 Industrial Ave",
        "body": "Suite B is available.",
    }
    values.update(overrides)
    return RawMessageEvidence(**values)


class RawEvidenceContractTests(unittest.TestCase):
    def test_subject_and_fresh_body_are_normalized(self):
        result = normalize_message_evidence(raw_message())

        self.assertEqual(
            (EvidenceSource.SUBJECT, EvidenceSource.FRESH_BODY),
            tuple(item.source_kind for item in result.evidence),
        )
        self.assertEqual(EvidenceFreshness.FRESH, result.evidence[1].freshness)
        self.assertEqual("body:lines-1-1", result.evidence[1].location)

    def test_contracts_are_frozen_and_sequences_are_tuples(self):
        external = [
            ExternalEvidenceInput(
                EvidenceSource.ATTACHMENT,
                "attachment:flyer.pdf",
                content="123 Industrial Ave\nSuite B",
            )
        ]
        raw = raw_message(external=external)
        external.append(
            ExternalEvidenceInput(
                EvidenceSource.LINK,
                "link:https://example.com",
                content="ignored",
            )
        )

        self.assertIsInstance(raw.external, tuple)
        self.assertEqual(1, len(raw.external))
        with self.assertRaises(FrozenInstanceError):
            raw.subject = "changed"

    def test_external_input_rejects_invalid_sources_and_payloads(self):
        with self.assertRaises(ValueError):
            ExternalEvidenceInput(
                EvidenceSource.FRESH_BODY,
                "body",
                content="text",
            )
        with self.assertRaises(ValueError):
            ExternalEvidenceInput(EvidenceSource.LINK, "link:x")
        with self.assertRaises(ValueError):
            ExternalEvidenceInput(
                EvidenceSource.LINK,
                "link:x",
                content="text",
                error="timeout",
            )
        with self.assertRaises(TypeError):
            raw_message(external=("not external evidence",))


class EvidenceSegmentationTests(unittest.TestCase):
    def test_gmail_quote_and_bottom_posted_fresh_text_are_separate(self):
        body = (
            "Suite B is available.\n\n"
            "On Tue, Jul 21, 2026 at 9:30 AM Pat wrote:\n"
            "> Suite A is available.\n"
            "> Asking $15/SF.\n\n"
            "Also, Suite C may work."
        )

        result = normalize_message_evidence(raw_message(body=body))
        body_items = result.evidence[1:]

        self.assertEqual(
            (
                EvidenceSource.FRESH_BODY,
                EvidenceSource.QUOTED_BODY,
                EvidenceSource.FRESH_BODY,
            ),
            tuple(item.source_kind for item in body_items),
        )
        self.assertEqual("Suite B is available.", body_items[0].content)
        self.assertIn("Suite A is available", body_items[1].content)
        self.assertEqual("Also, Suite C may work.", body_items[2].content)
        self.assertEqual(body_items[0].evidence_id, body_items[1].parent_evidence_id)

    def test_outlook_original_message_is_quoted_through_end(self):
        body = (
            "Suite B remains available.\n\n"
            "-----Original Message-----\n"
            "From: Pat Broker <pat@example.com>\n"
            "Sent: Monday, July 20, 2026 9:15 AM\n"
            "To: Alex User <alex@example.com>\n"
            "Subject: RE: 123 Industrial Ave\n\n"
            "Suite A was available."
        )

        result = normalize_message_evidence(raw_message(body=body))

        self.assertEqual(EvidenceSource.FRESH_BODY, result.evidence[1].source_kind)
        self.assertEqual(EvidenceSource.QUOTED_BODY, result.evidence[2].source_kind)
        self.assertIn("Original Message", result.evidence[2].content)
        self.assertIn("Suite A was available", result.evidence[2].content)

    def test_forwarded_message_is_not_treated_as_fresh(self):
        body = (
            "This may be useful.\n\n"
            "---------- Forwarded message ---------\n"
            "From: Pat Broker <pat@example.com>\n"
            "Subject: 999 Other Road\n\n"
            "999 Other Road is available."
        )

        result = normalize_message_evidence(raw_message(body=body))

        self.assertEqual(
            (EvidenceSource.FRESH_BODY, EvidenceSource.FORWARDED_BODY),
            tuple(item.source_kind for item in result.evidence[1:]),
        )
        self.assertEqual(
            EvidenceFreshness.FORWARDED,
            result.evidence[2].freshness,
        )

    def test_apple_forwarded_message_is_not_treated_as_fresh(self):
        body = (
            "This may be useful.\n\n"
            "Begin forwarded message:\n\n"
            "From: Pat Broker <pat@example.com>\n"
            "Subject: 999 Other Road\n\n"
            "999 Other Road is available."
        )

        result = normalize_message_evidence(raw_message(body=body))

        self.assertEqual(
            (EvidenceSource.FRESH_BODY, EvidenceSource.FORWARDED_BODY),
            tuple(item.source_kind for item in result.evidence[1:]),
        )

    def test_outlook_fw_header_preserves_forwarded_source_actor(self):
        body = (
            "Please review.\n\n"
            "From: Pat Broker <pat@example.com>\n"
            "Sent: Monday, July 20, 2026 9:15 AM\n"
            "To: Alex User <alex@example.com>\n"
            "Subject: 999 Other Road\n\n"
            "999 Other Road is available."
        )

        result = normalize_message_evidence(
            raw_message(subject="FW: another option", body=body)
        )
        forwarded = result.evidence[2]

        self.assertEqual(EvidenceSource.FORWARDED_BODY, forwarded.source_kind)
        self.assertEqual("pat@example.com", forwarded.actor.email)
        self.assertEqual("Pat Broker", forwarded.actor.name)
        self.assertEqual(ActorRole.UNKNOWN, forwarded.actor.role)

    def test_gmail_quote_preserves_inner_actor(self):
        body = (
            "Suite B is available.\n\n"
            "On Tue, Jul 21, 2026 at 9:30 AM Pat Broker "
            "<pat@example.com> wrote:\n"
            "> Suite A was leased."
        )

        result = normalize_message_evidence(raw_message(body=body))
        quoted = result.evidence[2]

        self.assertEqual("Pat Broker", quoted.actor.name)
        self.assertEqual("pat@example.com", quoted.actor.email)
        self.assertEqual(ActorRole.UNKNOWN, quoted.actor.role)

    def test_signature_and_external_content_are_parented_to_fresh_body(self):
        result = normalize_message_evidence(
            raw_message(
                signature="Alex Broker\nalex@example.com",
                external=(
                    ExternalEvidenceInput(
                        EvidenceSource.ATTACHMENT,
                        "attachment:floorplan.pdf",
                        content="Floor plan for Suite B",
                    ),
                    ExternalEvidenceInput(
                        EvidenceSource.LINK,
                        "link:https://example.com/flyer",
                        content="Flyer for 123 Industrial Ave",
                    ),
                ),
            )
        )
        fresh_id = result.evidence[1].evidence_id

        self.assertEqual(
            (
                EvidenceSource.SUBJECT,
                EvidenceSource.FRESH_BODY,
                EvidenceSource.SIGNATURE,
                EvidenceSource.ATTACHMENT,
                EvidenceSource.LINK,
            ),
            tuple(item.source_kind for item in result.evidence),
        )
        self.assertTrue(
            all(item.parent_evidence_id == fresh_id for item in result.evidence[2:])
        )

    def test_extraction_failure_is_visible_without_fabricated_evidence(self):
        result = normalize_message_evidence(
            raw_message(
                body="",
                external=(
                    ExternalEvidenceInput(
                        EvidenceSource.ATTACHMENT,
                        "attachment:locked.pdf",
                        error="encrypted_pdf",
                    ),
                ),
            )
        )

        self.assertEqual((EvidenceSource.SUBJECT,), tuple(x.source_kind for x in result.evidence))
        self.assertEqual(1, len(result.failures))
        failure = result.failures[0]
        self.assertIsInstance(failure, EvidenceFailure)
        self.assertEqual("encrypted_pdf", failure.reason)
        self.assertEqual(result.evidence[0].evidence_id, failure.parent_evidence_id)
        with self.assertRaises(ValueError):
            EvidenceFailure(
                failure_id="wrong",
                tenant_id=failure.tenant_id,
                campaign_id=failure.campaign_id,
                message_id=failure.message_id,
                source_kind=failure.source_kind,
                location=failure.location,
                reason=failure.reason,
                parent_evidence_id=failure.parent_evidence_id,
            )

    def test_attachment_only_reply_is_valid(self):
        result = normalize_message_evidence(
            raw_message(
                subject="",
                body="",
                external=(
                    ExternalEvidenceInput(
                        EvidenceSource.ATTACHMENT,
                        "attachment:flyer.pdf",
                        content="123 Industrial Ave is available.",
                    ),
                ),
            )
        )

        self.assertEqual(1, len(result.evidence))
        self.assertEqual(EvidenceSource.ATTACHMENT, result.evidence[0].source_kind)
        self.assertIsNone(result.evidence[0].parent_evidence_id)

    def test_crlf_and_lf_produce_identical_evidence(self):
        lf = normalize_message_evidence(
            raw_message(body="Suite B is available.\nAsking $15/SF.")
        )
        crlf = normalize_message_evidence(
            raw_message(body="Suite B is available.\r\nAsking $15/SF.")
        )

        self.assertEqual(lf, crlf)

    def test_repeated_normalization_is_deterministic(self):
        raw = raw_message(
            body="Fresh response.\n\n> Older response.",
            external=(
                ExternalEvidenceInput(
                    EvidenceSource.LINK,
                    "link:https://example.com",
                    content="123 Industrial Ave",
                ),
            ),
        )

        self.assertEqual(
            normalize_message_evidence(raw),
            normalize_message_evidence(raw),
        )


if __name__ == "__main__":
    unittest.main()
