"""A message parked for retry must actually be retryable.

LIVE BREAK. The retry queue was not a queue. A message that could not be
processed -- because an attachment link would not fetch -- was parked and marked
retryable, and both the dashboard and the logs reported it as waiting. It could
never succeed, by construction:

  the parked record stores the message's INTERNET Message-ID, the
  angle-bracketed RFC 5322 identifier that travels in the mail headers;

  the retry asked the mail provider for `/me/messages/<that value>`, and that
  endpoint addresses messages by the PROVIDER'S OWN opaque identifier.

Those are two different kinds of identifier, so the request was rejected
400 Bad Request on every attempt, forever. Two messages sat parked in the
account for weeks behind exactly this, each recording the same rejected URL --
with the internet id visibly URL-encoded into the path.

⚠️ The cost is not one lost message. A parked message is a conversation the
product has stopped processing, so everything the broker says afterwards on that
thread is affected too.

The fix translates the identifier before the lookup. These tests pin the
translation, the pass-through for the other kind, and -- most importantly -- that
a message which genuinely cannot be found fails with a message that SAYS SO,
rather than another anonymous "Bad Request" that reads like a provider blip.
"""
import os
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("E2E_TEST_MODE", "true")

from email_automation import processing  # noqa: E402

INTERNET_ID = "<CAN7v8_Tz0sT-Jdq5YyMs5U91k09FXVZeX3f7u6r=duFy8=r-=g@mail.gmail.com>"
PROVIDER_ID = "AQMkADAwATMwMAExLTk1NQAxLTRhNzgtMDACLTAwCgBGAAAD"


class IdentifierKindTests(unittest.TestCase):
    def test_an_angle_bracketed_id_is_the_internet_kind(self):
        self.assertTrue(processing._looks_like_internet_message_id(INTERNET_ID))

    def test_a_provider_id_is_not_the_internet_kind(self):
        for value in (PROVIDER_ID, "", None, "   ", "not<bracketed", "unbracketed>"):
            with self.subTest(value=value):
                self.assertFalse(processing._looks_like_internet_message_id(value))


class FetchByEitherIdentifierTests(unittest.TestCase):
    def _reader(self, *responses):
        reader = MagicMock()
        reader.read.side_effect = list(responses)
        return reader

    def _response(self, payload):
        r = MagicMock()
        r.json.return_value = payload
        return r

    def test_an_internet_id_is_translated_before_the_lookup(self):
        """The whole defect in one assertion: the internet id must NOT be the
        thing put in the message path."""
        reader = self._reader(
            self._response({"value": [{"id": PROVIDER_ID}]}),
            self._response({"id": PROVIDER_ID, "subject": "RE: a property"}),
        )
        with patch.object(processing, "_mailbox_reader", return_value=reader), \
             patch.object(processing, "exponential_backoff_request", lambda fn: fn()):
            envelope = processing._fetch_graph_message_by_id({}, INTERNET_ID)

        self.assertEqual(PROVIDER_ID, envelope["id"])
        self.assertEqual(2, reader.read.call_count, "expected a translate then a fetch")

        translate_op, translate_url = reader.read.call_args_list[0].args[:2]
        self.assertEqual("message_id_by_internet_id", translate_op)
        self.assertTrue(translate_url.endswith("/me/messages"))
        self.assertIn(
            INTERNET_ID,
            reader.read.call_args_list[0].kwargs["params"]["$filter"],
        )

        _fetch_op, fetch_url = reader.read.call_args_list[1].args[:2]
        self.assertIn(PROVIDER_ID.replace("=", "%3D")[:20], fetch_url.replace("%3D", "%3D"))
        self.assertNotIn("%3C", fetch_url, "the internet id was still used as the path")

    def test_a_provider_id_is_passed_straight_through(self):
        """Most callers already hold the right kind; they must not pay for a
        translation round trip, and must not be broken by one."""
        reader = self._reader(self._response({"id": PROVIDER_ID}))
        with patch.object(processing, "_mailbox_reader", return_value=reader), \
             patch.object(processing, "exponential_backoff_request", lambda fn: fn()):
            envelope = processing._fetch_graph_message_by_id({}, PROVIDER_ID)

        self.assertEqual(PROVIDER_ID, envelope["id"])
        self.assertEqual(1, reader.read.call_count, "a provider id needs no translation")
        self.assertEqual("message_envelope_by_id", reader.read.call_args_list[0].args[0])

    def test_a_message_that_cannot_be_found_says_which_id_failed(self):
        """An anonymous "Bad Request" is what let this hide for weeks.

        It read as a transient provider problem. A retry that cannot resolve its
        own message must name the identifier it could not find, so the next
        person sees a missing message rather than a flaky mailbox.
        """
        reader = self._reader(self._response({"value": []}))
        with patch.object(processing, "_mailbox_reader", return_value=reader), \
             patch.object(processing, "exponential_backoff_request", lambda fn: fn()):
            with self.assertRaises(ValueError) as caught:
                processing._fetch_graph_message_by_id({}, INTERNET_ID)

        self.assertIn(INTERNET_ID, str(caught.exception))
        self.assertEqual(
            1, reader.read.call_count,
            "it must not go on to fetch a message it could not resolve",
        )

    def test_a_quote_in_the_identifier_cannot_break_the_filter(self):
        """Message ids are attacker-influenced text arriving from the network."""
        awkward = "<it's@example.test>"
        reader = self._reader(
            self._response({"value": [{"id": PROVIDER_ID}]}),
            self._response({"id": PROVIDER_ID}),
        )
        with patch.object(processing, "_mailbox_reader", return_value=reader), \
             patch.object(processing, "exponential_backoff_request", lambda fn: fn()):
            processing._fetch_graph_message_by_id({}, awkward)

        sent = reader.read.call_args_list[0].kwargs["params"]["$filter"]
        self.assertIn("it''s", sent, "the quote must be escaped, not passed raw")


if __name__ == "__main__":
    unittest.main()
