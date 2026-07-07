"""FIX 4: multi-building PDF escalation for the decomposed extract-fields sub-call.

`_pdf_manifest_needs_escalation` is a deterministic, address-pattern-based
detector. When a PDF manifest carries >=2 DISTINCT street addresses that are not
all the target property's address, `_extract_fields` escalates the extraction
sub-call from gpt-4o-mini to gpt-5.2 (cost-only-safe: gpt-5.2 is strictly more
capable). Ordinary single-building flyers stay on the cheap gpt-4o-mini path.

No live API: the OpenAI client and usage metering are mocked throughout.
"""

import os
import unittest
from unittest import mock

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "service-account.json"),
)

from email_automation import ai_processing as ai  # noqa: E402


def _fake_client(output_text='{"updates": []}'):
    resp = mock.Mock()
    resp.output_text = output_text
    resp.usage = None
    resp.id = "resp_test"
    fake = mock.Mock()
    fake.responses.create.return_value = resp
    return fake


class PdfManifestEscalationDetectorTests(unittest.TestCase):
    TARGET = "1 Randolph Ct, Evans"

    def test_no_manifest_no_escalation(self):
        self.assertFalse(ai._pdf_manifest_needs_escalation(None, self.TARGET))
        self.assertFalse(ai._pdf_manifest_needs_escalation([], self.TARGET))

    def test_manifest_text_without_addresses_no_escalation(self):
        # Zips, prices, SF, years, clear-height — none are street addresses.
        manifest = [{"name": "flyer.pdf",
                     "text": "Premium warehouse, 50,000 SF total, asking $12.00/SF NNN, "
                             "24' clear height, built 2015, zip 30809."}]
        self.assertFalse(ai._pdf_manifest_needs_escalation(manifest, self.TARGET))

    def test_single_matching_address_no_escalation(self):
        manifest = [{"name": "flyer.pdf",
                     "text": "1 Randolph Ct, Evans. Great space. "
                             "1 Randolph Ct available now."}]
        self.assertFalse(ai._pdf_manifest_needs_escalation(manifest, self.TARGET))

    def test_two_distinct_addresses_escalates(self):
        manifest = [{"name": "brochure.pdf",
                     "text": "Building A: 1 Randolph Ct. "
                             "Building B: 9 Center Drive is also available."}]
        self.assertTrue(ai._pdf_manifest_needs_escalation(manifest, self.TARGET))

    def test_distinct_addresses_across_multiple_pdfs_escalate(self):
        manifest = [{"name": "a.pdf", "text": "1 Randolph Ct, Evans."},
                    {"name": "b.pdf", "text": "42 Main Street, Augusta."}]
        self.assertTrue(ai._pdf_manifest_needs_escalation(manifest, self.TARGET))

    def test_empty_text_entries_no_escalation(self):
        manifest = [{"name": "scan.pdf", "text": ""}, {"name": "other.pdf", "text": None}]
        self.assertFalse(ai._pdf_manifest_needs_escalation(manifest, self.TARGET))


class ExtractFieldsModelSelectionTests(unittest.TestCase):
    def _run_extract(self, pdf_manifest, target_anchor):
        fake = _fake_client('{"updates": []}')
        with mock.patch.object(ai, "client", fake), \
             mock.patch.object(ai, "track_openai_usage_safely") as track:
            ai._extract_fields(
                column_rules="C", doc_selection_rules="D", header=["A"], rowvals=["x"],
                rownum=3, missing_fields=[], target_anchor=target_anchor, conversation=[],
                pdf_manifest=pdf_manifest, url_texts=None, uid="u", client_id="c",
                thread_id="t", sheet_id="s", extraction_fields=None,
            )
        return fake, track

    def test_single_building_uses_cheap_model(self):
        manifest = [{"name": "flyer.pdf", "text": "1 Randolph Ct, Evans available now."}]
        fake, track = self._run_extract(manifest, "1 Randolph Ct, Evans")
        self.assertEqual(fake.responses.create.call_args.kwargs["model"], "gpt-4o-mini")
        # Cost attribution follows the resolved model.
        self.assertEqual(track.call_args.kwargs["model"], "gpt-4o-mini")

    def test_multi_building_escalates_model(self):
        manifest = [{"name": "brochure.pdf",
                     "text": "Building A: 1 Randolph Ct. "
                             "Building B: 9 Center Drive is also available."}]
        fake, track = self._run_extract(manifest, "1 Randolph Ct, Evans")
        self.assertEqual(fake.responses.create.call_args.kwargs["model"], "gpt-5.2")
        self.assertEqual(track.call_args.kwargs["model"], "gpt-5.2")

    def test_no_manifest_uses_cheap_model(self):
        fake, _ = self._run_extract(None, "1 Randolph Ct, Evans")
        self.assertEqual(fake.responses.create.call_args.kwargs["model"], "gpt-4o-mini")


if __name__ == "__main__":
    unittest.main()
