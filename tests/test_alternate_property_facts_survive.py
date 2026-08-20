"""The facts a broker states about the alternate property must survive the veto.

LIVE break, 2026-08-06 production campaign, PROD-0806-5: "first alternate-
property reply facts discarded until the broker repeats them". This is the
recurrence the project has now seen five times -- two end-user reports, a
synthetic probe, FDR-010's own recurrence row, and this live reproduction.

The mechanism is a fact being deleted TWICE and stored nowhere:

  1. `_suppress_cross_property_current_row_updates` strips those updates off
     `proposal["updates"]`, and it is RIGHT to -- facts about the alternate
     property must not be written onto the current property's row. But it drops
     them on the floor instead of handing them on.
  2. The `new_property_pending_approval` notification carries address, city,
     link, notes, leasing company/contact, PDFs -- and no updates. So on accept,
     `app.py` re-extracts from the flyer against a SYNTHETIC conversation stub
     ("Here is information about {address}."). Anything the broker stated in the
     email BODY and not in the flyer is gone, and the operator gets a blank
     column until the broker is asked again and repeats himself.

So the fix is not new extraction. The fact was already extracted correctly,
once. It has to be carried to the row it is about.
"""

import unittest

from email_automation import ai_processing


def _updates(*columns):
    return [
        {"column": c, "value": v, "confidence": 0.9, "reason": "stated in reply"}
        for c, v in columns
    ]


class TheVetoCarriesWhatItRemoves(unittest.TestCase):
    """Whatever the veto takes off the current row is handed to the alternate."""

    def _run(self, proposal, conversation, target_anchor="100 Example Rd, Springfield"):
        return ai_processing._suppress_cross_property_current_row_updates(
            proposal, conversation, target_anchor,
        )

    def test_alternate_facts_are_carried_when_the_current_row_is_vetoed(self):
        proposal = {
            "events": [{"type": "new_property", "address": "250 Other Way", "city": "Springfield"}],
            "updates": _updates(("Total SF", "12000"), ("Rent/SF/Yr", "8.50")),
        }
        conversation = [{
            "direction": "inbound",
            "content": "That one is gone. Try 250 Other Way instead - 12,000 SF at $8.50/SF/yr.",
        }]
        result = self._run(proposal, conversation)
        self.assertEqual(result["updates"], [], "alternate facts must not land on the current row")
        carried = result.get("alternate_property_updates") or []
        self.assertEqual(
            {u["column"] for u in carried}, {"Total SF", "Rent/SF/Yr"},
            "the veto removed these because they belong to the ALTERNATE property; "
            "that is exactly the set the alternate-property notification needs",
        )

    def test_the_compound_unavailable_plus_alternate_case_carries_too(self):
        # The live 2026-08-06 shape: the original goes non-viable in the same
        # reply that suggests the replacement. The property_unavailable branch
        # returns early, so this is where the facts actually died.
        proposal = {
            "events": [
                {"type": "property_unavailable"},
                {"type": "new_property", "address": "250 Other Way", "city": "Springfield"},
            ],
            "updates": _updates(("Total SF", "12000"), ("Rent/SF/Yr", "8.50")),
        }
        conversation = [{
            "direction": "inbound",
            "content": "100 Example Rd just leased. 250 Other Way is available - 12,000 SF at $8.50.",
        }]
        result = self._run(proposal, conversation)
        carried = result.get("alternate_property_updates") or []
        self.assertEqual(
            {u["column"] for u in carried}, {"Total SF", "Rent/SF/Yr"},
            "the compound case is the one production actually reproduced",
        )

    def test_current_property_facts_are_not_carried_to_the_alternate(self):
        # The mirror failure: writing the CURRENT property's numbers onto the
        # replacement row would be the same defect pointed the other way.
        proposal = {
            "events": [{"type": "close_conversation"}],
            "updates": _updates(("Total SF", "12000")),
        }
        conversation = [{"direction": "inbound", "content": "100 Example Rd is 12,000 SF."}]
        result = self._run(proposal, conversation)
        self.assertEqual(result["updates"], proposal["updates"], "no alternate property, no veto")
        self.assertFalse(
            result.get("alternate_property_updates"),
            "nothing may be carried when there is no alternate property to carry it to",
        )

    def test_nothing_is_carried_when_nothing_was_removed(self):
        proposal = {
            "events": [{"type": "new_property", "address": "250 Other Way", "city": "Springfield"}],
            "updates": [],
        }
        conversation = [{"direction": "inbound", "content": "Try 250 Other Way instead."}]
        result = self._run(proposal, conversation)
        self.assertFalse(result.get("alternate_property_updates"))


class TheAcceptPathPrefersTheStatedFact(unittest.TestCase):
    """On accept, a fact stated in the email body outranks one re-read from a flyer."""

    def test_carried_updates_win_over_flyer_reextraction(self):
        from app import _merge_carried_updates_over_extraction

        carried = _updates(("Total SF", "12000"), ("Rent/SF/Yr", "8.50"))
        # The flyer disagrees on SF and adds a column the body never mentioned.
        extracted = _updates(("Total SF", "9500"), ("Ceiling Height", "24'"))

        merged = _merge_carried_updates_over_extraction(carried, extracted)
        by_column = {u["column"]: u["value"] for u in merged}

        self.assertEqual(
            by_column["Total SF"], "12000",
            "the broker stated 12,000 SF in the email; a flyer number must not silently "
            "overwrite what the broker actually said about this property",
        )
        self.assertEqual(by_column["Rent/SF/Yr"], "8.50", "body-stated fact must survive")
        self.assertEqual(
            by_column["Ceiling Height"], "24'",
            "flyer extraction still fills columns the body did not cover",
        )

    def test_column_matching_ignores_case_and_spacing(self):
        from app import _merge_carried_updates_over_extraction

        merged = _merge_carried_updates_over_extraction(
            _updates(("Total SF", "12000")),
            _updates(("  total sf ", "9500")),
        )
        self.assertEqual(len(merged), 1, "one column, not two, however the header is spelled")
        self.assertEqual(merged[0]["value"], "12000")

    def test_no_carried_updates_leaves_extraction_untouched(self):
        from app import _merge_carried_updates_over_extraction

        extracted = _updates(("Total SF", "9500"))
        self.assertEqual(_merge_carried_updates_over_extraction([], extracted), extracted)
        self.assertEqual(_merge_carried_updates_over_extraction(None, extracted), extracted)

    def test_carried_updates_apply_with_no_flyer_at_all(self):
        # Today a suggestion with no PDF manifest applies NOTHING: the accept
        # path's only writer is the flyer extractor. A broker who states the
        # numbers in plain text and attaches nothing gets an empty row.
        from app import _merge_carried_updates_over_extraction

        carried = _updates(("Total SF", "12000"))
        self.assertEqual(_merge_carried_updates_over_extraction(carried, []), carried)
        self.assertEqual(_merge_carried_updates_over_extraction(carried, None), carried)


if __name__ == "__main__":
    unittest.main()
