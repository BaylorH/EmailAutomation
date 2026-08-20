"""A broker's "there is none" is an ANSWER, not a gap — found in real customer traffic.

LIVE break (a real client's broker thread, 2026-07-20/21). The broker wrote:

    "You can ramp one of the 2 loading docks. There's no existing drive-in."
    "$18 gross, no opex. Remainder on the attached."

Forty-five seconds after the second message the system replied asking for exactly
"Ops Ex / SF" and "Drive Ins" -- the two things he had just answered. He wrote back:

    "Are you using Ai or something to email me. This is ridiculous. I have answered
     these questions. You are wasting my time."

and the client forwarded the whole thread to us. This is the most expensive class of
bug this product has: it is visible to the customer's own counterparty and it reads
as incompetence rather than as a glitch.

Both fields failed for the same underlying reason and in two different places:

  * OPS EX. _augment_proposal_opex_basis DELETED the opex update whenever the broker
    said "gross / no opex". Its docstring calls that stripping a fabricated zero the
    model invented -- but the condition it fires on is the broker HAVING SAID SO, so
    it discarded the one case where the zero is a stated fact rather than an invention.
    The cell stayed empty, an empty cell reads as missing, and a missing field is
    re-asked forever.

  * DRIVE INS. "There's no existing drive-in" matched no zero-detection pattern at
    all, so nothing was ever proposed for the column and it stayed missing the same way.

A definitive "there is none" must be RECORDED, so the row can close and the question
stops. A zero is only ever written from the broker's own fresh words.
"""
import os
import sys
import unittest

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "service-account.json"),
)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import email_automation.ai_processing as A  # noqa: E402

HEADER = ["Address", "City", "Contact", "Email", "Phone",
          "Total SF", "Rent/SF/Yr", "Ops Ex / SF", "Drive Ins", "Docks",
          "Ceiling Ht", "Power"]
CONFIG = {"mappings": {"ops_ex_sf": "Ops Ex / SF", "drive_ins": "Drive Ins"}}

GROSS_QUOTE = "$18 gross, no opex. Remainder on the attached."
NO_DRIVE_IN = "You can ramp one of the 2 loading docks. There's no existing drive-in."


def _conv(text):
    return [{"direction": "inbound", "from": "broker@example-cre.com",
             "subject": "Re: Availability", "timestamp": "2026-07-21T00:09:09Z",
             "content": text}]


def _opex_proposal(value):
    return {"updates": [{"column": "Ops Ex / SF", "value": value, "confidence": 0.9}],
            "events": [], "response_email": None}


class GrossQuoteRecordsOpsExTests(unittest.TestCase):
    def test_a_stated_gross_quote_records_the_opex_rather_than_dropping_it(self):
        out = A._augment_proposal_opex_basis(
            _opex_proposal("0"), [""] * len(HEADER), HEADER, CONFIG, _conv(GROSS_QUOTE)
        )
        cols = [u.get("column") for u in out.get("updates") or []]
        self.assertIn(
            "Ops Ex / SF", cols,
            "the broker said there is no opex; discarding that leaves the cell empty "
            "and the system asks him again",
        )

    def test_the_recorded_value_satisfies_the_missing_field_check(self):
        """The whole point: the row must stop reporting this field as missing."""
        out = A._augment_proposal_opex_basis(
            _opex_proposal("0"), [""] * len(HEADER), HEADER, CONFIG, _conv(GROSS_QUOTE)
        )
        update = next(u for u in out["updates"] if u.get("column") == "Ops Ex / SF")
        row = [""] * len(HEADER)
        row[HEADER.index("Ops Ex / SF")] = str(update.get("value"))
        self.assertNotIn("Ops Ex /SF", A.check_missing_required_fields(row, HEADER, None))

    def test_the_recorded_value_carries_its_reason(self):
        out = A._augment_proposal_opex_basis(
            _opex_proposal("0"), [""] * len(HEADER), HEADER, CONFIG, _conv(GROSS_QUOTE)
        )
        update = next(u for u in out["updates"] if u.get("column") == "Ops Ex / SF")
        self.assertTrue(str(update.get("reason") or "").strip(),
                        "a zero written on a gross basis must say so")

    def test_a_real_opex_number_is_never_rewritten(self):
        out = A._augment_proposal_opex_basis(
            _opex_proposal("3.75"), [""] * len(HEADER), HEADER, CONFIG,
            _conv("Opex is $3.75/SF/year."),
        )
        update = next(u for u in out["updates"] if u.get("column") == "Ops Ex / SF")
        self.assertEqual(str(update.get("value")), "3.75")

    def test_a_zero_the_broker_never_stated_is_still_dropped(self):
        """Provenance is the whole distinction: no stated basis, no recorded zero."""
        out = A._augment_proposal_opex_basis(
            _opex_proposal("0"), [""] * len(HEADER), HEADER, CONFIG,
            _conv("The space is 22,000 SF and available now."),
        )
        cols = [u.get("column") for u in out.get("updates") or []]
        self.assertNotIn("Ops Ex / SF", cols)


if __name__ == "__main__":
    unittest.main()
