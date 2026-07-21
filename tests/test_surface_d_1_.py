"""Surface D-1 — Base-V1 rubric state-permutation tests for core.upload_mapping.

Feature under test: the UPLOAD / MAPPING code path — turning an uploaded broker
roster into (a) resolved column→canonical mappings and (b) newly-created sheet
rows, with the deterministic safety guards that wrap that path.  Every test drives
REAL production code (email_automation.column_config / .sheet_operations /
.processing / .email); only the datastore boundaries (Google Sheets client,
Firestore) are faked.  ZERO live sends, Sheets, or Graph calls.

Each test maps to exactly one (feature, state) rubric cell and asserts a
safety-relevant behavior that would FAIL if the guard regressed:

  happy_path              -> valid roster headers map to canonical fields AND a
                             row is materialized with each value under its column.
  bad_placeholder         -> a roster cell holding a raw '[NAME]' merge tag is
                             blanked at write time, never persisted as sheet data.
  wrong_recipient         -> a mapped launch whose queued recipient is absent from
                             the resolved sheet row's email columns is flagged.
  terminal_state          -> new mapped rows land ABOVE the NON-VIABLE terminal
                             divider, never inside the dead/terminal section.
  manual_continuation     -> re-running column-mapping resolution is deterministic
                             and never binds one header to two canonical fields.
  duplicate_retry         -> a re-run whose address+city already exists is detected
                             as a duplicate so the row is not double-created.
  operator_visible_failure-> a mapping recipient-mismatch is surfaced as a
                             dead-letter-queue document with a human-readable
                             reason, and the unsendable item leaves the send queue.
"""

import os
import unittest
from unittest import mock

from googleapiclient.errors import HttpError

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "service-account.json",
    ),
)

from email_automation.column_config import detect_column_mapping
from email_automation.sheet_operations import insert_property_row_above_divider
from email_automation.processing import _property_exists_in_sheet
from email_automation import email as email_mod


# ---------------------------------------------------------------------------
# Fake Google Sheets client — models ONLY the googleapiclient builder chain
# (spreadsheets().values().get/update/batchUpdate + spreadsheets().get/batchUpdate)
# that the real upload/mapping functions call.  The mapping + row-creation logic
# under test is untouched real code.
# ---------------------------------------------------------------------------
class _Req:
    def __init__(self, payload=None, on_execute=None):
        self._payload = payload
        self._on_execute = on_execute

    def execute(self):
        if self._on_execute is not None:
            self._on_execute()
        return {} if self._payload is None else self._payload


class _Values:
    def __init__(self, parent):
        self.parent = parent

    def get(self, spreadsheetId=None, range=None, **kwargs):
        return _Req(payload=self.parent._resolve_get(range))

    def update(self, spreadsheetId=None, range=None, valueInputOption=None,
               body=None, **kwargs):
        def _record():
            self.parent.value_updates.append(
                {"range": range, "values": (body or {}).get("values")}
            )
        return _Req(on_execute=_record)

    def batchUpdate(self, spreadsheetId=None, body=None, **kwargs):
        def _record():
            self.parent.values_batch_updates.append(body)
        return _Req(on_execute=_record)


class _Spreadsheets:
    def __init__(self, parent):
        self.parent = parent
        self._values = _Values(parent)

    def values(self):
        return self._values

    def get(self, spreadsheetId=None, **kwargs):
        return _Req(payload={
            "sheets": [{
                "properties": {
                    "sheetId": self.parent.grid_id,
                    "title": self.parent.tab_title,
                }
            }]
        })

    def batchUpdate(self, spreadsheetId=None, body=None, **kwargs):
        def _record():
            self.parent.insert_range_calls.append(body)
        return _Req(on_execute=_record)


class FakeSheets:
    """Deterministic in-memory stand-in for the Sheets v4 client.

    header is row 2; column A drives divider detection; data_rows answers the
    3:1000 duplicate scan; row_reads answers single-row `{n}:{n}` guard reads.
    """

    def __init__(self, tab_title="Sheet1", header=None, col_a=None,
                 data_rows=None, row_reads=None, grid_id=0):
        self.tab_title = tab_title
        self.header = header or []
        self.col_a = col_a or []
        self.data_rows = data_rows or []
        self.row_reads = row_reads or {}
        self.grid_id = grid_id
        self.value_updates = []          # values().update() payloads (row fills)
        self.values_batch_updates = []   # values().batchUpdate() payloads
        self.insert_range_calls = []     # spreadsheets().batchUpdate() (insertRange)
        self._spreadsheets = _Spreadsheets(self)

    def spreadsheets(self):
        return self._spreadsheets

    def _resolve_get(self, range_notation):
        rng = range_notation.split("!", 1)[1] if "!" in range_notation else range_notation
        if rng == "2:2":
            return {"values": [self.header] if self.header else [[]]}
        if rng == "A:A":
            return {"values": [[cell] for cell in self.col_a]}
        if rng == "3:1000":
            return {"values": self.data_rows}
        if rng in self.row_reads:
            return {"values": [self.row_reads[rng]]}
        return {"values": []}


# ---------------------------------------------------------------------------
# Minimal Firestore double for the dead-letter drop sink (operator_visible_failure).
# ---------------------------------------------------------------------------
class _AddCollection:
    def __init__(self):
        self.added = []

    def add(self, data):
        self.added.append(data)
        return ("fake-ts", object())


class _UserDoc:
    def __init__(self, fs):
        self._fs = fs

    def collection(self, name):
        assert name == "deadLetterQueue", f"unexpected subcollection {name!r}"
        return self._fs.dead_letter


class _UsersCollection:
    def __init__(self, fs):
        self._fs = fs

    def document(self, uid):
        return _UserDoc(self._fs)


class FakeFirestore:
    def __init__(self):
        self.dead_letter = _AddCollection()

    def collection(self, name):
        assert name == "users", f"unexpected top-level collection {name!r}"
        return _UsersCollection(self)


class _FakeOutboxDocRef:
    def __init__(self, doc_id):
        self.id = doc_id
        self.deleted = False
        self.set_calls = []

    def delete(self):
        self.deleted = True

    def set(self, data, merge=False):
        self.set_calls.append((data, merge))


def _lower_keys(mapping):
    """Row-insert reads values_by_header with lowercased header keys."""
    return {k.lower(): v for k, v in mapping.items()}


class UploadMappingHappyPathTests(unittest.TestCase):
    """core.upload_mapping / happy_path."""

    def test_valid_roster_maps_columns_and_creates_row(self):
        """Proves a valid roster upload BOTH resolves headers to canonical
        column mappings AND materializes a new sheet row with every mapped
        value written under its own header column (correct positional merge).
        """
        headers = ["Property Address", "City", "Email", "Leasing Contact"]

        # --- Stage 1: column/variable mapping resolution ---
        mapping = detect_column_mapping(headers, use_ai=False)["mappings"]
        self.assertEqual(mapping["property_address"], "Property Address")
        self.assertEqual(mapping["city"], "City")
        self.assertEqual(mapping["email"], "Email")
        self.assertEqual(mapping["leasing_contact"], "Leasing Contact")

        # --- Stage 2: row creation from the mapped values ---
        sheets = FakeSheets(header=headers, col_a=["", "", "NON-VIABLE"])
        values_by_header = _lower_keys({
            "Property Address": "100 Main St",
            "City": "Dallas",
            "Email": "broker@example.com",
            "Leasing Contact": "Jane Broker",
        })

        with mock.patch(
            "email_automation.sheet_operations._apply_gross_rent_formula_for_row",
            return_value=False,
        ):
            rownum = insert_property_row_above_divider(
                sheets, "sheet-1", "Sheet1", values_by_header
            )

        self.assertEqual(rownum, 3, "row must insert at the divider position (row 3)")
        self.assertEqual(len(sheets.value_updates), 1)
        written = sheets.value_updates[0]["values"][0]
        # Each value lands under its own header column, in header order.
        self.assertEqual(
            written,
            ["100 Main St", "Dallas", "broker@example.com", "Jane Broker"],
        )
        # The insert actually shifted a range in (row was created, not just filled).
        self.assertEqual(len(sheets.insert_range_calls), 1)
        self.assertIn("insertRange", sheets.insert_range_calls[0]["requests"][0])


class UploadMappingBadPlaceholderTests(unittest.TestCase):
    """core.upload_mapping / bad_placeholder."""

    def test_unresolved_placeholder_cell_is_blanked_not_persisted(self):
        """Proves a roster cell still carrying a raw '[NAME]'-style merge tag is
        scrubbed to empty at row-write time, so the literal placeholder is never
        persisted as sheet data — while resolved cells in the same write pass
        through unchanged (near-miss negative control)."""
        headers = ["Property Address", "Leasing Contact", "City"]
        sheets = FakeSheets(header=headers, col_a=[])  # no divider -> append at end
        values_by_header = _lower_keys({
            "Property Address": "200 Oak Ave",
            "Leasing Contact": "[NAME]",   # unresolved merge tag from a bad roster
            "City": "Austin",
        })

        with mock.patch(
            "email_automation.sheet_operations._apply_gross_rent_formula_for_row",
            return_value=False,
        ):
            insert_property_row_above_divider(
                sheets, "sheet-1", "Sheet1", values_by_header
            )

        written = sheets.value_updates[0]["values"][0]
        self.assertEqual(written[1], "", "raw '[NAME]' placeholder must be blanked")
        self.assertNotIn(
            "[NAME]", "".join(written),
            "no literal merge placeholder may reach the sheet as data",
        )
        # Near-miss controls: legitimately-resolved cells are preserved verbatim.
        self.assertEqual(written[0], "200 Oak Ave")
        self.assertEqual(written[2], "Austin")

    def test_resolved_name_cell_writes_through(self):
        """Discriminating control: a real resolved contact name is NOT scrubbed,
        proving the placeholder guard is targeted, not a blanket name-column wipe."""
        headers = ["Property Address", "Leasing Contact"]
        sheets = FakeSheets(header=headers, col_a=[])
        values_by_header = _lower_keys({
            "Property Address": "5 Elm",
            "Leasing Contact": "Jane Doe",
        })
        with mock.patch(
            "email_automation.sheet_operations._apply_gross_rent_formula_for_row",
            return_value=False,
        ):
            insert_property_row_above_divider(
                sheets, "sheet-1", "Sheet1", values_by_header
            )
        self.assertEqual(sheets.value_updates[0]["values"][0][1], "Jane Doe")


class UploadMappingWrongRecipientTests(unittest.TestCase):
    """core.upload_mapping / wrong_recipient."""

    HEADER = ["Property Address", "Email", "City"]
    ROW_NUM = 5

    def _run_guard(self, recipient_email):
        data = {
            "source": "dashboard_new_campaign",   # real launch classifier
            "clientId": "client-1",
            "rowNumber": self.ROW_NUM,
        }
        self.assertTrue(
            email_mod._is_campaign_launch_outbox(data),
            "fixture must be a real campaign-launch (variable-mapping) outbox",
        )
        sheets = FakeSheets(
            header=self.HEADER,
            row_reads={f"{self.ROW_NUM}:{self.ROW_NUM}":
                       ["100 Main St", "broker@example.com", "Dallas"]},
        )
        doc_ref = _FakeOutboxDocRef("outbox-1")
        with mock.patch.object(email_mod, "_sheets_client", return_value=sheets), \
             mock.patch.object(email_mod, "_get_first_tab_title", return_value="Sheet1"), \
             mock.patch.object(email_mod, "_get_sheet_id_or_fail", return_value="sheet-1"), \
             mock.patch.object(email_mod, "_move_to_dead_letter") as dead_letter:
            diverted = email_mod._dead_letter_campaign_recipient_row_mismatch_if_needed(
                "user-1", doc_ref, data, recipient_email
            )
        return diverted, dead_letter

    def test_recipient_absent_from_mapped_row_is_flagged(self):
        """Proves a variable-mapped launch whose queued recipient email is NOT
        present in the resolved sheet row's email columns is caught and diverted
        (never sent to the mismatched address)."""
        diverted, dead_letter = self._run_guard("stranger@evil.com")
        self.assertTrue(diverted, "recipient off the mapped row must be diverted")
        dead_letter.assert_called_once()

    def test_recipient_matching_mapped_row_is_allowed(self):
        """Negative control: a recipient that DOES appear on the resolved row is
        allowed through, proving the mismatch flag is discriminating."""
        diverted, dead_letter = self._run_guard("broker@example.com")
        self.assertFalse(diverted, "on-row recipient must pass the mapping guard")
        dead_letter.assert_not_called()

    def test_transient_sheet_verification_error_retries_without_dead_letter(self):
        data = {
            "source": "dashboard_new_campaign",
            "clientId": "client-1",
            "rowNumber": self.ROW_NUM,
            "attempts": 0,
        }
        response = mock.Mock(status=500, reason="Internal error encountered")
        transient_error = HttpError(
            response,
            b'{"error":{"message":"Internal error encountered"}}',
        )
        doc_ref = _FakeOutboxDocRef("outbox-transient")

        with mock.patch.object(
            email_mod,
            "_campaign_sheet_header_and_row",
            side_effect=transient_error,
        ), mock.patch.object(email_mod, "_move_to_dead_letter") as dead_letter, mock.patch.object(
            email_mod,
            "_mark_outbox_action_audit_retrying",
        ) as mark_retrying:
            handled = email_mod._dead_letter_campaign_recipient_row_mismatch_if_needed(
                "user-1",
                doc_ref,
                data,
                "broker@example.com",
            )

        self.assertTrue(handled)
        dead_letter.assert_not_called()
        self.assertEqual(len(doc_ref.set_calls), 1)
        retry_patch, merge = doc_ref.set_calls[0]
        self.assertTrue(merge)
        self.assertEqual(retry_patch["status"], "retrying")
        self.assertEqual(retry_patch["attempts"], 1)
        self.assertIsNone(retry_patch["processingBy"])
        self.assertIsNone(retry_patch["processingAt"])
        mark_retrying.assert_called_once()


class UploadMappingTerminalStateTests(unittest.TestCase):
    """core.upload_mapping / terminal_state."""

    def test_new_row_inserts_above_terminal_nonviable_divider(self):
        """Proves a fresh roster row is inserted immediately ABOVE the NON-VIABLE
        terminal divider, so mapping onto a sheet that already has a dead/terminal
        section never lands a live contact inside that terminal region; with no
        divider the row appends at the end (control)."""
        headers = ["Property Address", "City"]
        vbh = _lower_keys({"Property Address": "9 Live St", "City": "Reno"})

        # Divider present at row 4 -> new row must take row 4 (above the divider).
        sheets = FakeSheets(header=headers, col_a=["", "", "", "NON-VIABLE", "dead1"])
        with mock.patch(
            "email_automation.sheet_operations._apply_gross_rent_formula_for_row",
            return_value=False,
        ):
            rownum = insert_property_row_above_divider(sheets, "sheet-1", "Sheet1", vbh)
        self.assertEqual(rownum, 4, "row must land at the divider index, above it")
        self.assertEqual(sheets.value_updates[0]["range"], "Sheet1!4:4")

        # No divider -> append at end (len(colA)+1). Control that the guard is
        # divider-driven, not a hard-coded index.
        sheets2 = FakeSheets(header=headers, col_a=["h", "r3", "r4"])
        with mock.patch(
            "email_automation.sheet_operations._apply_gross_rent_formula_for_row",
            return_value=False,
        ):
            rownum2 = insert_property_row_above_divider(sheets2, "sheet-1", "Sheet1", vbh)
        self.assertEqual(rownum2, 4, "with no terminal divider, append after last row")


class UploadMappingManualContinuationTests(unittest.TestCase):
    """core.upload_mapping / manual_continuation."""

    def test_remapping_is_deterministic_and_never_double_binds_a_header(self):
        """Proves that re-running column-mapping resolution on the same roster —
        as when an operator manually re-opens/re-confirms the mapping after a
        pause — reproduces the identical canonical→column bindings, and never
        binds a single header to two canonical fields.  This is the mapping-layer
        'does not double-create' invariant: a manual re-run cannot silently
        diverge or duplicate a column assignment."""
        # Two headers alias-collide onto the 'email' canonical; a stable resolver
        # must claim exactly one and leave the other unmapped, identically twice.
        headers = ["Property Address", "City", "Email", "Email Address", "Total SF"]

        first = detect_column_mapping(headers, use_ai=False)
        second = detect_column_mapping(headers, use_ai=False)

        self.assertEqual(first["mappings"], second["mappings"],
                         "re-run must be deterministic")

        mapped_headers = list(first["mappings"].values())
        self.assertEqual(
            len(mapped_headers), len(set(mapped_headers)),
            "no header may be bound to more than one canonical field",
        )
        # The colliding aliases must not BOTH be consumed — exactly one wins,
        # the other is reported unmapped (no silent double-assignment).
        consumed_email_cols = [h for h in mapped_headers
                               if h in ("Email", "Email Address")]
        self.assertEqual(len(consumed_email_cols), 1)
        self.assertIn("Email Address" if consumed_email_cols == ["Email"] else "Email",
                      first["unmapped"])


class UploadMappingDuplicateRetryTests(unittest.TestCase):
    """core.upload_mapping / duplicate_retry."""

    HEADER = ["Property Address", "City", "Email"]

    def _sheets_with_existing(self):
        return FakeSheets(
            header=self.HEADER,
            data_rows=[
                ["100 Main St", "Dallas", "broker@example.com"],
                ["55 Pine Rd", "Austin", "agent@example.com"],
            ],
        )

    def test_existing_address_is_detected_so_row_is_not_double_created(self):
        """Proves a re-run of the mapping/creation for an address+city that
        already exists in the sheet is detected as a duplicate (returns True),
        so the retry SKIPS re-adding the row — while a genuinely new address is
        reported absent (returns False) and would be created."""
        sheets = self._sheets_with_existing()

        already = _property_exists_in_sheet(
            sheets, "sheet-1", "Sheet1", self.HEADER, "100 Main St", "Dallas"
        )
        self.assertTrue(already, "existing address+city must be flagged a duplicate")

        # Discriminating control: a brand-new address is NOT a duplicate.
        fresh = _property_exists_in_sheet(
            sheets, "sheet-1", "Sheet1", self.HEADER, "999 New Way", "Reno"
        )
        self.assertFalse(fresh, "a new address+city must not be treated as a duplicate")

        # City must participate: same address, different city is a distinct row.
        diff_city = _property_exists_in_sheet(
            sheets, "sheet-1", "Sheet1", self.HEADER, "100 Main St", "Houston"
        )
        self.assertFalse(diff_city, "address match with different city is not a duplicate")


class UploadMappingOperatorVisibleFailureTests(unittest.TestCase):
    """core.upload_mapping / operator_visible_failure."""

    def test_mapping_mismatch_surfaces_visible_dead_letter_and_dequeues(self):
        """Proves a variable-mapping recipient-mismatch failure is SURFACED to the
        operator: it lands a document in the deadLetterQueue carrying a
        human-readable failureReason describing the mismatch, and the unsendable
        outbox item is removed from the send queue (doc deleted).  Unlike the
        wrong_recipient cell (which only checks the detection boolean), this
        drives the REAL _move_to_dead_letter sink end-to-end."""
        header = ["Property Address", "Email"]
        row_num = 7
        data = {
            "source": "dashboard_new_campaign",
            "clientId": "client-1",
            "rowNumber": row_num,
        }
        sheets = FakeSheets(
            header=header,
            row_reads={f"{row_num}:{row_num}": ["100 Main St", "broker@example.com"]},
        )
        doc_ref = _FakeOutboxDocRef("outbox-9")
        fake_fs = FakeFirestore()

        with mock.patch.object(email_mod, "_sheets_client", return_value=sheets), \
             mock.patch.object(email_mod, "_get_first_tab_title", return_value="Sheet1"), \
             mock.patch.object(email_mod, "_get_sheet_id_or_fail", return_value="sheet-1"), \
             mock.patch.object(email_mod, "_update_action_audit"), \
             mock.patch("email_automation.clients._fs", fake_fs):
            diverted = email_mod._dead_letter_campaign_recipient_row_mismatch_if_needed(
                "user-1", doc_ref, data, "stranger@evil.com"
            )

        self.assertTrue(diverted)
        # The failure is visible: exactly one dead-letter document was written.
        self.assertEqual(len(fake_fs.dead_letter.added), 1)
        dl = fake_fs.dead_letter.added[0]
        self.assertEqual(dl["status"], "dead_lettered")
        self.assertTrue(dl.get("failureReason"))
        self.assertIn("does not match", dl["failureReason"])
        self.assertIn("7", dl["failureReason"])  # references the offending row
        # The unsendable item was pulled out of the live send queue.
        self.assertTrue(doc_ref.deleted, "mismatched outbox item must leave the queue")


if __name__ == "__main__":
    unittest.main()
