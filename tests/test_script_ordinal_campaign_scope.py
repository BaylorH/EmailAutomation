"""Script selection must count contacts within THIS campaign, not all of history.

LIVE break, 2026-08-06 production campaign, recorded as one of eight defects:
"cross-contact copy leaked the wrong property ordinal".

`get_contact_email_count` queries every thread under the user where the address
appears -- no clientId filter, no time bound -- and `_select_script_for_recipient`
uses that count as the SCRIPT INDEX. A contact ordinal is being used as a
property ordinal. So a broker who appeared in any earlier campaign receives
script[N] for their FIRST property in a new campaign.

The damage is not only the wrong script. At count >= 2 the selector appends
"I want to keep things organized for both of us, so I'm sending separate emails
for each of your properties I'm inquiring about." -- to a broker being contacted
about one property, for the first time in this campaign. It reads as a
mass-mailer, which is the reputation damage the record already attributes to
this product ("It really pissed off the listing broker").

The scripts are authored per campaign, so the count that indexes them must be
scoped per campaign.
"""

import unittest
from unittest.mock import patch

from email_automation import email as email_module


class _Doc:
    def __init__(self, **fields):
        self._fields = fields

    def to_dict(self):
        return dict(self._fields)


class _Query:
    """Records the filters applied, so the test asserts the QUERY, not a stub count."""

    def __init__(self, recorder, docs):
        self._recorder = recorder
        self._docs = docs

    def where(self, *args, **kwargs):
        self._recorder.append((args, kwargs))
        return self

    def stream(self):
        return iter(self._docs)


class _Collection:
    def __init__(self, recorder, docs):
        self._recorder, self._docs = recorder, docs

    def where(self, *args, **kwargs):
        return _Query(self._recorder, self._docs).where(*args, **kwargs)


class _FS:
    def __init__(self, recorder, docs):
        self._recorder, self._docs = recorder, docs

    def collection(self, _name):
        return self

    def document(self, _name):
        return self

    def __getattr__(self, _n):
        return lambda *a, **k: _Collection(self._recorder, self._docs)


class ScriptOrdinalIsScopedToTheCampaign(unittest.TestCase):
    def _count(self, docs, **kwargs):
        recorder = []
        fs = _FS(recorder, docs)
        with patch.object(email_module, "_fs_for", return_value=fs):
            n = email_module.get_contact_email_count("user-1", "broker@example.invalid", **kwargs)
        return n, recorder

    def test_the_count_filters_on_the_campaign(self):
        _, recorder = self._count([], client_id="campaign-B")
        filtered = " ".join(repr(f) for f in recorder)
        self.assertIn(
            "clientId", filtered,
            "the thread query must be scoped to the campaign whose scripts are being indexed; "
            "without it, a broker from any earlier campaign is treated as a repeat contact",
        )

    def test_a_broker_new_to_this_campaign_gets_the_primary_script(self):
        # Two threads from an EARLIER campaign; none in this one.
        scripts = ["PRIMARY", "SECOND", "THIRD"]
        with patch.object(email_module, "get_contact_email_count", return_value=0):
            chosen = email_module._select_script_for_recipient(
                "user-1", "broker@example.invalid", scripts,
                contact_name="Pat", client_id="campaign-B",
            )
        self.assertIn("PRIMARY", chosen)
        self.assertNotIn(
            "keep things organized", chosen,
            "a first-contact-in-this-campaign email must not claim to be one of several",
        )

    def test_the_selector_passes_the_campaign_through_to_the_count(self):
        seen = {}

        def fake_count(user_id, recipient_email, runtime=None, client_id=None):
            seen["client_id"] = client_id
            return 0

        with patch.object(email_module, "get_contact_email_count", fake_count):
            email_module._select_script_for_recipient(
                "user-1", "broker@example.invalid", ["PRIMARY"],
                contact_name="Pat", client_id="campaign-B",
            )
        self.assertEqual(
            seen.get("client_id"), "campaign-B",
            "the selector must scope its own count; otherwise the filter exists but is never used",
        )


class TheOrdinalCountsPropertiesNotThreads(unittest.TestCase):
    """The number that indexes the scripts is a PROPERTY ordinal.

    Scoping the query to the campaign fixed WHICH threads are counted. It did
    not fix WHAT is being counted: `len(results)` is a count of thread
    documents, and a thread is not a property. One property can carry more than
    one thread -- a thread re-created after a bounce, a second thread matched to
    the same row -- and each extra one silently advances the broker's ordinal.

    The consequence is the same sentence PROD-0806-1 was filed for: at a count of
    two the selector appends "I'm sending separate emails for each of your
    properties I'm inquiring about" to a broker who has been asked about exactly
    one. Now that a thread carries `propertyRef`, the count can be what it always
    claimed to be.
    """

    def _count(self, docs, **kwargs):
        recorder = []
        fs = _FS(recorder, docs)
        with patch.object(email_module, "_fs_for", return_value=fs):
            return email_module.get_contact_email_count(
                "user-1", "broker@example.invalid", **kwargs
            )

    def test_two_threads_on_one_property_are_one_property(self):
        docs = [
            _Doc(propertyRef="prop_aaaa1111"),
            _Doc(propertyRef="prop_aaaa1111"),
        ]
        self.assertEqual(
            self._count(docs, client_id="campaign-B"), 1,
            "one property asked about twice is still the broker's first property; "
            "counting the second thread hands them a repeat-contact script and the "
            "'separate emails for each of your properties' note",
        )

    def test_two_threads_on_two_properties_are_two(self):
        docs = [
            _Doc(propertyRef="prop_aaaa1111"),
            _Doc(propertyRef="prop_bbbb2222"),
        ]
        self.assertEqual(self._count(docs, client_id="campaign-B"), 2)

    def test_threads_without_a_ref_still_count_one_each(self):
        # Threads that predate the ref have no identity to dedupe on. Counting
        # each as its own property preserves exactly today's behaviour for them,
        # so adopting the ref cannot move an existing campaign's ordinal down.
        docs = [_Doc(), _Doc(propertyRef=""), _Doc(propertyRef=None)]
        self.assertEqual(self._count(docs, client_id="campaign-B"), 3)

    def test_refs_and_unrefed_threads_add_up(self):
        docs = [
            _Doc(propertyRef="prop_aaaa1111"),
            _Doc(propertyRef="prop_aaaa1111"),
            _Doc(),
        ]
        self.assertEqual(self._count(docs, client_id="campaign-B"), 2)

    def test_a_document_that_cannot_be_read_still_counts(self):
        # Never let an unreadable thread doc silently lower the ordinal.
        class _Broken:
            def to_dict(self):
                raise RuntimeError("unreadable")

        self.assertEqual(self._count([_Broken(), _Broken()], client_id="campaign-B"), 2)



if __name__ == "__main__":
    unittest.main()
