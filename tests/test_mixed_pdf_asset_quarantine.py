import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import fitz


os.environ.setdefault("E2E_TEST_MODE", "true")

from email_automation import ai_processing, campaign_safety, file_handling, processing


TARGET_ADDRESS = "101 Fictional Forge Road"
TARGET_CITY = "Exampleton"
TARGET_ANCHOR = f"{TARGET_ADDRESS}, {TARGET_CITY}"
PDF_NAME = "Fictional Three Property Availability.pdf"
PDF_LINK = "https://assets.example.invalid/fictional-three-property-availability.pdf"
IMAGE_LINK = "https://assets.example.invalid/fictional-three-property-preview.png"
PAGE_MARKERS = (
    "PAGE ONE - TARGET AVAILABILITY",
    "PAGE TWO - COMPETING SUITE A",
    "PAGE THREE - COMPETING SUITE B",
)


def _build_native_three_page_pdf() -> bytes:
    pages = (
        (
            PAGE_MARKERS[0],
            TARGET_ADDRESS,
            f"{TARGET_CITY}, AZ",
            "The target property is available for lease.",
            "No confirmed target figures are provided.",
            "This target section intentionally contains no measurements or financial numbers.",
        ),
        (
            PAGE_MARKERS[1],
            "202 Imaginary Industry Avenue, Suite A",
            "Exampleton, AZ",
            "Available area: 12,650 SF",
            "Base rent: $18.75/SF/YR | Operating expenses: $4.60/SF/YR",
            "Clear height: 37 feet | Dock doors: 7 | Drive-in doors: 3",
            "Power: 1200A, 480V, three phase",
        ),
        (
            PAGE_MARKERS[2],
            "303 Makebelieve Manufacturing Parkway, Suite B",
            "Exampleton, AZ",
            "Available area: 8,275 SF",
            "Base rent: $21.40/SF/YR | Operating expenses: $5.15/SF/YR",
            "Clear height: 29 feet | Dock doors: 4 | Drive-in doors: 2",
            "Power: 800A, 208V, three phase",
            "Portfolio total across Suite A and Suite B: 20,925 SF",
        ),
    )
    document = fitz.open()
    try:
        for lines in pages:
            page = document.new_page(width=612, height=792)
            y = 72
            for index, line in enumerate(lines):
                page.insert_text(
                    (72, y),
                    line,
                    fontname="helv",
                    fontsize=16 if index == 0 else 11,
                )
                y += 34 if index == 0 else 24
        document.set_metadata({})
        return document.tobytes(
            garbage=4,
            deflate=True,
            no_new_id=True,
        )
    finally:
        document.close()


class _Snapshot:
    def __init__(self, reference, data):
        self.reference = reference
        self.id = reference.id
        self.exists = data is not None
        self._data = dict(data or {})

    def to_dict(self):
        return dict(self._data)


class _DocumentReference:
    def __init__(self, firestore, path):
        self._firestore = firestore
        self.path = tuple(path)
        self.id = str(self.path[-1])

    def get(self, transaction=None):
        data = self._firestore.documents.get(self.path)
        return _Snapshot(self, data)

    def set(self, data, merge=False):
        before = self._firestore.documents.get(self.path, {}) if merge else {}
        after = dict(before)
        after.update(dict(data))
        self._firestore.documents[self.path] = after
        self._firestore.writes.append(("set", self.path, dict(data)))

    def update(self, data):
        after = dict(self._firestore.documents.get(self.path, {}))
        after.update(dict(data))
        self._firestore.documents[self.path] = after
        self._firestore.writes.append(("update", self.path, dict(data)))

    def delete(self):
        self._firestore.documents.pop(self.path, None)
        self._firestore.writes.append(("delete", self.path, {}))

    def collection(self, name):
        return _CollectionReference(self._firestore, self.path + (str(name),))


class _CollectionReference:
    def __init__(self, firestore, path):
        self._firestore = firestore
        self.path = tuple(path)

    def document(self, doc_id):
        return _DocumentReference(self._firestore, self.path + (str(doc_id),))

    def stream(self):
        expected_length = len(self.path) + 1
        return [
            _Snapshot(_DocumentReference(self._firestore, path), data)
            for path, data in self._firestore.documents.items()
            if len(path) == expected_length and path[:-1] == self.path
        ]

    def where(self, *args, **kwargs):
        return self

    def limit(self, _limit):
        return self


class _InMemoryFirestore:
    def __init__(self, initial_documents):
        self.documents = {
            tuple(path): dict(data) for path, data in initial_documents.items()
        }
        self.writes = []

    def collection(self, name):
        return _CollectionReference(self, (str(name),))

    def documents_in_collection(self, *collection_path):
        prefix = tuple(collection_path)
        expected_length = len(prefix) + 1
        return {
            path[-1]: dict(data)
            for path, data in self.documents.items()
            if len(path) == expected_length and path[:-1] == prefix
        }


def _column_config():
    extraction_fields = [
        "total_sf",
        "rent_sf_yr",
        "ops_ex_sf",
        "ceiling_ht",
        "drive_ins",
        "docks",
        "power",
    ]
    return {
        "mappings": {
            "property_address": "Property Address",
            "city": "City",
            "email": "Email",
            "total_sf": "Total SF",
            "rent_sf_yr": "Rent/SF /Yr",
            "ops_ex_sf": "Ops Ex /SF",
            "ceiling_ht": "Ceiling Ht",
            "drive_ins": "Drive Ins",
            "docks": "Docks",
            "power": "Power",
            "flyer_link": "Flyer / Link",
            "floorplan": "Floorplan",
        },
        "extractionFields": extraction_fields,
        "requiredFields": extraction_fields,
        "formulaFields": [],
        "neverRequest": [],
        "customFields": {},
    }


class MixedPdfAssetQuarantineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pdf_bytes = _build_native_three_page_pdf()

    def test_native_pdf_bytes_render_extract_and_classify_as_mixed(self):
        self.assertEqual(self.pdf_bytes, _build_native_three_page_pdf())

        rendered_pages = []
        document = fitz.open(stream=self.pdf_bytes, filetype="pdf")
        try:
            for page in document:
                rendered_pages.append(
                    page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5), alpha=False).tobytes("png")
                )
        finally:
            document.close()

        self.assertEqual(3, len(rendered_pages))
        self.assertTrue(all(page.startswith(b"\x89PNG") and len(page) > 100 for page in rendered_pages))

        processed = file_handling.process_pdf_for_ai(self.pdf_bytes, PDF_NAME)

        self.assertEqual("local_extraction", processed["method"])
        for page_number, marker in enumerate(PAGE_MARKERS, start=1):
            self.assertIn(f"--- Page {page_number} ---", processed["text"])
            self.assertIn(marker, processed["text"])
        self.assertEqual(
            "mixed",
            ai_processing._attachment_property_verdict(
                f"{PDF_NAME}\n{processed['text']}",
                TARGET_ANCHOR,
            ),
        )

    def test_ambiguous_real_pdf_pipeline_pauses_without_row_or_asset_effects(self):
        user_id = "fictional-user"
        client_id = "fictional-client"
        thread_id = "fictional-thread"
        graph_message_id = "fictional-graph-message"
        internet_message_id = "<fictional-message@example.test>"
        sender_email = "broker@example.test"
        mailbox_email = "owner@example.test"
        header = [
            "Property Address",
            "City",
            "Email",
            "Total SF",
            "Rent/SF /Yr",
            "Ops Ex /SF",
            "Ceiling Ht",
            "Drive Ins",
            "Docks",
            "Power",
            "Flyer / Link",
            "Floorplan",
            "Property Image",
            "Property Image Source",
        ]
        rowvals = [
            TARGET_ADDRESS,
            TARGET_CITY,
            sender_email,
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
        ]
        config = _column_config()
        body = (
            "The attached packet covers the target availability and two alternate "
            "suites. The packet is the only source of figures."
        )
        msg = {
            "id": graph_message_id,
            "subject": "Fictional property packet",
            "from": {"emailAddress": {"address": sender_email, "name": "Casey Example"}},
            "sender": {"emailAddress": {"address": sender_email, "name": "Casey Example"}},
            "toRecipients": [{"emailAddress": {"address": mailbox_email}}],
            "internetMessageId": internet_message_id,
            "conversationId": "fictional-conversation",
            "receivedDateTime": "2026-08-11T12:00:00Z",
            "bodyPreview": body,
            "hasAttachments": True,
            "internetMessageHeaders": [
                {"name": "In-Reply-To", "value": "<fictional-root@example.test>"},
            ],
        }
        full_body_response = MagicMock()
        full_body_response.json.return_value = {
            "body": {"content": body, "contentType": "Text"},
            "hasAttachments": True,
            "sender": msg["sender"],
            "toRecipients": msg["toRecipients"],
        }
        model_proposal = {
            "updates": [
                {"column": "Total SF", "value": "12650", "confidence": 0.99},
                {"column": "Rent/SF /Yr", "value": "18.75", "confidence": 0.99},
                {"column": "Ops Ex /SF", "value": "4.60", "confidence": 0.99},
                {"column": "Ceiling Ht", "value": "37", "confidence": 0.99},
                {"column": "Drive Ins", "value": "3", "confidence": 0.99},
                {"column": "Docks", "value": "7", "confidence": 0.99},
                {"column": "Power", "value": "1200A 480V", "confidence": 0.99},
            ],
            "events": [{
                "type": "needs_user_input",
                "reason": "unclear",
                "question": "Please review the attachment.",
            }],
            "response_email": None,
            "notes": "",
        }
        model_response = SimpleNamespace(
            output_text=json.dumps(model_proposal),
            usage=None,
            id="fictional-response",
        )
        firestore = _InMemoryFirestore({
            ("users", user_id, "threads", thread_id): {
                "clientId": client_id,
                "email": [sender_email],
                "status": processing.THREAD_STATUS["active"],
                "rowNumber": 3,
                "followUpConfig": {"enabled": False},
            },
            ("users", user_id, "clients", client_id): {
                "status": "live",
                "automationPaused": False,
                "criteria": "Fictional industrial search",
            },
            ("systemConfig", "campaignAccess"): {
                "automationEnabled": True,
                "allowedUids": [],
            },
        })

        saved_messages = []
        notifications = []
        status_updates = []

        def save_message(*args):
            saved_messages.append(args)
            return True

        def write_notification(*args, **kwargs):
            notifications.append({"args": args, "kwargs": kwargs})
            return "fictional-notification"

        def update_thread_status(*args):
            status_updates.append(args)
            return True

        apply_proposal = MagicMock(name="apply_proposal_to_sheet")
        append_flyer = MagicMock(
            name="append_links_to_flyer_link_column",
            return_value={"Flyer / Link": [PDF_LINK]},
        )
        append_floorplan = MagicMock(name="append_links_to_floorplan_column", return_value={})
        write_property_image = MagicMock(
            name="write_property_image_columns",
            return_value={
                "Property Image": [IMAGE_LINK],
                "Property Image Source": [f"Broker flyer preview: {PDF_NAME}, page 2"],
            },
        )
        append_ai_meta = MagicMock(name="append_ai_meta")
        insert_row = MagicMock(name="insert_property_row_above_divider")
        send_reply = MagicMock(name="send_reply_in_thread")
        fetch_pdf_attachments = MagicMock(
            name="fetch_pdf_attachments",
            return_value=[{"name": PDF_NAME, "bytes": self.pdf_bytes}],
        )
        upload_pdf_to_drive = MagicMock(name="upload_pdf_to_drive", return_value=PDF_LINK)
        upload_property_image = MagicMock(
            name="upload_property_image_to_drive",
            return_value={
                "url": IMAGE_LINK,
                "driveLink": "https://assets.example.invalid/fictional-preview-archive.png",
                "contentType": "image/png",
                "byteCount": 100,
                "sha256": "fictional-preview-sha256",
            },
        )
        upload_pdf_user_data = MagicMock(name="upload_pdf_user_data")

        patches = [
            patch.object(processing, "_fs", firestore),
            patch.object(ai_processing, "_fs", firestore),
            patch.object(
                processing,
                "get_client_automation_decision",
                side_effect=lambda uid, cid: campaign_safety.get_client_automation_decision(
                    uid, cid, firestore_client=firestore
                ),
            ),
            patch.object(processing, "exponential_backoff_request", return_value=full_body_response),
            patch.object(processing, "lookup_thread_by_message_id", return_value=thread_id),
            patch.object(processing, "lookup_thread_by_conversation_id", return_value=None),
            patch.object(processing, "save_message", side_effect=save_message),
            patch.object(processing, "index_message_id", return_value=True),
            patch.object(processing, "dump_thread_from_firestore"),
            patch("email_automation.followup.cancel_followup_on_response"),
            patch.object(
                processing,
                "fetch_and_log_sheet_for_thread",
                return_value=(client_id, "fictional-sheet", header, 3, rowvals, config, config["extractionFields"]),
            ),
            patch.object(file_handling, "fetch_pdf_attachments", new=fetch_pdf_attachments),
            patch.object(file_handling, "upload_pdf_to_drive", new=upload_pdf_to_drive),
            patch.object(
                file_handling,
                "upload_property_image_to_drive",
                new=upload_property_image,
            ),
            patch.object(file_handling, "upload_pdf_user_data", new=upload_pdf_user_data),
            patch.object(processing, "fetch_and_process_linked_assets", return_value=[]),
            patch.object(processing, "write_message_order_test"),
            patch.object(ai_processing, "build_conversation_payload", return_value=[{
                "direction": "inbound",
                "from": sender_email,
                "fromName": "Casey Example",
                "content": body,
            }]),
            patch.object(ai_processing.client.responses, "create", return_value=model_response),
            patch.object(ai_processing, "track_openai_usage_safely"),
            patch.object(processing, "apply_proposal_to_sheet", new=apply_proposal),
            patch.object(processing, "_sheets_client", return_value=MagicMock()),
            patch.object(processing, "_get_first_tab_title", return_value="Sheet1"),
            patch.object(processing, "_read_header_row2", return_value=header),
            patch.object(processing, "is_event_handled", return_value=False),
            patch.object(processing, "mark_event_handled", return_value=True),
            patch.object(processing, "write_notification", side_effect=write_notification),
            patch.object(processing, "update_thread_status", side_effect=update_thread_status),
            patch.object(processing, "highlight_row"),
            patch.object(processing, "append_links_to_flyer_link_column", new=append_flyer),
            patch.object(processing, "append_links_to_floorplan_column", new=append_floorplan),
            patch.object(processing, "write_property_image_columns", new=write_property_image),
            patch.object(processing, "_append_ai_meta", new=append_ai_meta),
            patch.object(processing, "format_sheet_columns_autosize_with_exceptions"),
            patch.object(processing, "insert_property_row_above_divider", new=insert_row),
            patch.object(processing, "send_reply_in_thread", new=send_reply),
        ]

        for patcher in patches:
            patcher.start()
        try:
            processing.process_inbox_message(
                user_id,
                {"Authorization": "Bearer fictional-token"},
                msg,
                authenticated_mailbox_email=mailbox_email,
            )
        finally:
            for patcher in reversed(patches):
                patcher.stop()

        change_logs = firestore.documents_in_collection(
            "users", user_id, "sheetChangeLog"
        )
        proposed_logs = [entry for entry in change_logs.values() if entry.get("status") == "proposed"]
        applied_or_asset_logs = [
            entry
            for entry in change_logs.values()
            if entry.get("status") == "applied" or entry.get("source") in {
                "pdf_link_write",
                "property_image_write",
            }
        ]
        paused_actions = [
            item for item in notifications
            if item["kwargs"].get("kind") == "action_needed"
            and (item["kwargs"].get("meta") or {}).get("reason")
            == "needs_user_input:multi_property_attachment"
        ]

        self.assertEqual(1, len(proposed_logs))
        self.assertEqual([], proposed_logs[0]["proposalJson"]["updates"])
        self.assertEqual(1, len(notifications))
        self.assertEqual(1, len(paused_actions))
        self.assertEqual(
            [(user_id, thread_id, processing.THREAD_STATUS["paused"], "needs_user_input:multi_property_attachment")],
            status_updates,
        )
        apply_proposal.assert_not_called()
        append_flyer.assert_not_called()
        append_floorplan.assert_not_called()
        write_property_image.assert_not_called()
        append_ai_meta.assert_not_called()
        insert_row.assert_not_called()
        send_reply.assert_not_called()
        fetch_pdf_attachments.assert_called_once_with(
            {"Authorization": "Bearer fictional-token"},
            graph_message_id,
        )
        upload_pdf_to_drive.assert_called_once_with(PDF_NAME, self.pdf_bytes)
        upload_property_image.assert_called_once()
        upload_pdf_user_data.assert_not_called()
        self.assertEqual([], applied_or_asset_logs)

        self.assertEqual(1, len(saved_messages))
        saved_record = saved_messages[0][3]
        self.assertTrue(saved_record["hasAttachments"])
        self.assertEqual(graph_message_id, saved_record["sourceMessage"]["graphMessageId"])
        self.assertEqual(internet_message_id, saved_record["sourceMessage"]["internetMessageId"])
        action_meta = paused_actions[0]["kwargs"]["meta"]
        self.assertEqual(graph_message_id, action_meta["sourceMessageId"])
        self.assertEqual(internet_message_id, action_meta["sourceInternetMessageId"])
        self.assertEqual(graph_message_id, action_meta["sourceMessage"]["graphMessageId"])
        thread_data = firestore.documents[("users", user_id, "threads", thread_id)]
        self.assertEqual(graph_message_id, thread_data["lastInboundEnvelope"]["graphMessageId"])
        message_data = firestore.documents[
            ("users", user_id, "threads", thread_id, "messages", internet_message_id)
        ]
        self.assertEqual(
            [{"name": PDF_NAME, "driveLink": PDF_LINK, "type": "pdf"}],
            message_data["attachments"],
        )
        self.assertEqual(PDF_LINK, proposed_logs[0]["pdfManifest"][0]["drive_link"])
        self.assertEqual(
            "local_extraction",
            proposed_logs[0]["pdfManifest"][0]["method"],
        )
        self.assertIn(PAGE_MARKERS[2], proposed_logs[0]["pdfManifest"][0]["text"])


if __name__ == "__main__":
    unittest.main()
