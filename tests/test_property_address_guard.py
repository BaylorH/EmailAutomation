"""Control tests for the deterministic wrong-property address guard in
property_images.build_download_candidate.

Companion to tests/test_broker_language_broker_attachment_or_link_only.py
TestWrongPropertyPdfNoDeterministicGuard (the red safety test): a forwarded
flyer whose filename/address does not match the target property must not
silently become the row's flyer/preview source.

Contract pinned here (NEW tests only; no existing assertions were changed):

1. target_property_hint provided + filename/URL carries a clearly different
   street address (deterministic pattern: street-number + street-name tokens
   + street-suffix token) -> None (fail closed).
2. target_property_hint provided + the address in the filename matches the
   target -> candidate builds (production keeps resolving correct flyers).
3. NO hint + address-bearing filename that ALSO carries extra identifying
   tokens we cannot verify (e.g. "123-Main-St-DIFFERENT.pdf") -> None.
   The extra non-descriptor token is an unverifiable property claim.
4. NO hint + plain address-plus-descriptor filename (e.g. "4402 Rex Rd
   Flyer.pdf") keeps building — pinned by the existing green tests in
   test_property_image_resolver.py.
5. fetch_and_process_linked_assets threads target_property_hint through to
   build_download_candidate.
"""
import os
import unittest
from unittest import mock

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    "/Users/baylorharrison/Documents/GitHub.nosync/EmailAutomation/service-account.json",
)

from email_automation import file_handling as fh
from email_automation import property_images as pi


class TestAddressGuardWithTargetHint(unittest.TestCase):
    """When the caller CAN supply the target property, the guard verifies."""

    def test_matching_target_address_builds_candidate(self):
        candidate = pi.build_download_candidate(
            "https://www.dropbox.com/s/abc/123-Main-St-Flyer.pdf?dl=0",
            "123-Main-St-Flyer.pdf",
            target_property_hint="123 Main St, Houston, TX 77002",
        )
        self.assertIsNotNone(candidate)
        self.assertEqual("dropbox_pdf", candidate["sourceType"])

    def test_clearly_different_address_returns_none(self):
        self.assertIsNone(
            pi.build_download_candidate(
                "https://www.dropbox.com/s/abc/123-Main-St-Flyer.pdf?dl=0",
                "123-Main-St-Flyer.pdf",
                target_property_hint="4402 Rex Rd, Webster, TX",
            ),
            "A flyer named for a different street address must not become "
            "the row's flyer/preview source when the target is known.",
        )

    def test_no_address_in_filename_with_hint_builds(self):
        candidate = pi.build_download_candidate(
            "https://www.dropbox.com/s/abc/flyer.pdf?dl=0",
            "flyer.pdf",
            target_property_hint="4402 Rex Rd",
        )
        self.assertIsNotNone(candidate)

    def test_matching_address_with_extra_tokens_builds_when_verified(self):
        # Extra unknown token, but the target hint verifies the address, so
        # the claim is no longer unverifiable.
        candidate = pi.build_download_candidate(
            "https://www.dropbox.com/s/abc/123-Main-St-DIFFERENT.pdf?dl=0",
            "123-Main-St-DIFFERENT.pdf",
            target_property_hint="123 Main St",
        )
        self.assertIsNotNone(candidate)


class TestAddressGuardWithoutTargetHint(unittest.TestCase):
    """The red test's call shape: no target context available."""

    def test_address_with_unverifiable_extra_token_returns_none(self):
        self.assertIsNone(
            pi.build_download_candidate(
                "https://www.dropbox.com/s/other/123-Main-St-DIFFERENT.pdf?dl=0",
                "123-Main-St-DIFFERENT.pdf",
            )
        )

    def test_plain_address_plus_descriptor_filename_still_builds(self):
        # Pinned by test_property_image_resolver.py: address+descriptor names
        # without target context keep working.
        candidate = pi.build_download_candidate(
            "https://www.dropbox.com/s/abc/4402-Rex-Rd.pdf?dl=0",
            "4402 Rex Rd Flyer.pdf",
        )
        self.assertIsNotNone(candidate)

    def test_no_address_filename_still_builds(self):
        candidate = pi.build_download_candidate(
            "https://www.dropbox.com/s/ab12/flyer.pdf?dl=0", ""
        )
        self.assertIsNotNone(candidate)

    def test_no_hint_address_with_geographic_qualifiers_still_builds(self):
        # REGRESSION (CodeRabbit PR#15): the no-hint heuristic treated the
        # city/state qualifiers of a SAME-property address ("Webster, TX") as
        # unverifiable extra identifying tokens and dropped a valid flyer.
        # City/state that merely locate the one street address are geographic
        # qualifiers, not a competing property claim -> must still build.
        candidate = pi.build_download_candidate(
            "https://www.dropbox.com/s/abc/4402-Rex-Rd-Webster-TX-Flyer.pdf?dl=0",
            "4402-Rex-Rd-Webster-TX-Flyer.pdf",
        )
        self.assertIsNotNone(
            candidate,
            "A same-property flyer whose only 'extra' tokens are geographic "
            "qualifiers (city + state) must not be dropped when no target "
            "context is available.",
        )
        self.assertEqual("dropbox_pdf", candidate["sourceType"])

    def test_no_hint_state_abbreviation_alone_still_builds(self):
        candidate = pi.build_download_candidate(
            "https://www.dropbox.com/s/abc/1419-Atlantis-Dr-FL.pdf?dl=0",
            "1419-Atlantis-Dr-FL.pdf",
        )
        self.assertIsNotNone(candidate)

    def test_no_hint_unverifiable_address_records_manual_review_reason(self):
        # The genuinely-unverifiable case (non-geographic extra token we cannot
        # confirm belongs to the same property). Contract: still return None
        # (keeps TestWrongPropertyPdfNoDeterministicGuard green) BUT record a
        # manual-review reason via the sink so the link is never silently lost.
        reasons = []
        candidate = pi.build_download_candidate(
            "https://www.dropbox.com/s/other/123-Main-St-DIFFERENT.pdf?dl=0",
            "123-Main-St-DIFFERENT.pdf",
            manual_review_reasons=reasons,
        )
        self.assertIsNone(candidate)
        self.assertTrue(
            reasons,
            "An address-bearing link that cannot be verified without target "
            "context must record a manual-review reason instead of silently "
            "returning None.",
        )

    def test_hint_wrong_property_does_not_record_manual_review_reason(self):
        # When a target hint IS present and the address clearly differs, we are
        # confident it is the wrong property -> hard reject, NOT manual review.
        reasons = []
        candidate = pi.build_download_candidate(
            "https://www.dropbox.com/s/other/123-Main-St-Flyer.pdf?dl=0",
            "123-Main-St-Flyer.pdf",
            target_property_hint="4402 Rex Rd, Webster, TX",
            manual_review_reasons=reasons,
        )
        self.assertIsNone(candidate)
        self.assertEqual(
            [], reasons,
            "A confirmed wrong-property flyer (hint present) is a hard reject, "
            "not a manual-review surface.",
        )


class TestLinkedAssetsThreadTargetHint(unittest.TestCase):
    """fetch_and_process_linked_assets must pass the target context through so
    production keeps resolving correct-property flyers."""

    def test_target_hint_reaches_build_download_candidate(self):
        seen = {}

        def fake_candidate(url, filename_hint="", target_property_hint="",
                           manual_review_reasons=None):
            seen["target"] = target_property_hint
            return None

        with mock.patch(
            "email_automation.property_images.build_download_candidate",
            side_effect=fake_candidate,
        ):
            fh.fetch_and_process_linked_assets(
                ["https://www.dropbox.com/s/abc/flyer.pdf?dl=0"],
                target_property_hint="123 Main St, Houston, TX",
            )
        self.assertEqual("123 Main St, Houston, TX", seen.get("target"))

    def test_wrong_property_link_is_not_processed_when_target_known(self):
        results = fh.fetch_and_process_linked_assets(
            ["https://www.dropbox.com/s/abc/123-Main-St-Flyer.pdf?dl=0"],
            target_property_hint="4402 Rex Rd, Webster, TX",
        )
        self.assertEqual([], results)

    def test_no_hint_unverifiable_link_surfaced_for_manual_review(self):
        # No target context (production call shape at processing.py:3527). An
        # address-bearing link we cannot verify must be surfaced as a
        # manual-review entry, NEVER silently dropped (no-silent-drop contract).
        results = fh.fetch_and_process_linked_assets(
            ["https://www.dropbox.com/s/other/123-Main-St-DIFFERENT.pdf?dl=0"],
        )
        self.assertEqual(1, len(results))
        self.assertTrue(results[0].get("requires_manual_review"))
        self.assertEqual("manual_review_required", results[0].get("method"))

    def test_no_hint_same_property_geographic_link_is_processed(self):
        # The regression case: a valid same-property flyer whose filename
        # carries city/state qualifiers must resolve, not be dropped. We stub
        # the network + Drive so the test stays deterministic and offline.
        with mock.patch.object(
            fh, "_download_linked_asset",
            return_value=(b"%PDF-1.4 fake", "application/pdf"),
        ), mock.patch.object(
            fh, "process_pdf_for_ai",
            return_value={"text": "spec", "images": [], "method": "text"},
        ), mock.patch.object(
            fh, "upload_pdf_to_drive", return_value="https://drive/x"
        ), mock.patch.object(
            fh, "_attach_pdf_property_preview", return_value=None
        ):
            results = fh.fetch_and_process_linked_assets(
                ["https://www.dropbox.com/s/abc/4402-Rex-Rd-Webster-TX-Flyer.pdf?dl=0"],
            )
        self.assertEqual(1, len(results))
        self.assertFalse(results[0].get("requires_manual_review"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
