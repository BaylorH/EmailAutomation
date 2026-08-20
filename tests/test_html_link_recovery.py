"""A broker's hyperlinked flyer is destroyed before anything can judge it.

Graph delivers broker mail as HTML. ``normalize_graph_body`` converts it with
``strip_html_tags``, whose tag substitution deletes the whole element -- so
``<a href="https://.../flyer.pdf">Flyer</a>`` becomes " Flyer " and the target
is gone. The inbound path then harvests URLs from that converted text with a
plain ``https?://`` regex, so ``fetch_and_process_linked_assets`` is handed an
empty list and the broker's payload never reaches the row.

The whole linked-asset path exists to make exactly this visible: its own
comments say a dropped link is indistinguishable from "no assets" and lets a
message be marked processed with the broker's payload lost. The href never
reaches that code, so none of that judgement runs.

The population is unmeasurable from the stored record BY CONSTRUCTION -- the
stored body is the converted text, so the corpus holds 797 inbound messages,
none containing href markup. Its own blindness is the defect. What the corpus
does show is 29 messages where the broker announces a link in prose while the
stored text contains no URL at all; read individually, four are genuine
property or listing links and the rest are footer boilerplate.

Recovered hrefs are LOWER TRUST than a URL the broker typed as visible text,
because recovery also surfaces the regulatory and mail-system links that a
reader never treats as content. So they go only to the linked-asset lane, which
already binds a candidate to the target property and already refuses what it
cannot verify -- never to the page-fetch lane that feeds the extraction prompt,
whose inputs are deliberately unchanged. Boilerplate is filtered by families
READ OUT OF THE CORPUS rather than imagined, and an unrecognised link is kept
rather than dropped: a wrong filter entry leaves a case exactly as broken as it
is today, while a missing one would put disclosure boilerplate in a client's
flyer column, and only one of those two is recoverable.
"""

import os
import unittest

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "service-account.json",
    ),
)

from email_automation.message_transport import (
    asset_link_candidates,
    is_boilerplate_broker_link,
    normalize_graph_body,
    recover_html_link_targets,
)

FLYER = "https://example-brokerage.test/listings/pinewoods/flyer.pdf"
IABS = "https://www.trec.texas.gov/forms/iabs.pdf"


def html_body(inner):
    return {"contentType": "HTML", "content": inner}


class TheDefectItselfTests(unittest.TestCase):
    def test_the_converted_text_no_longer_contains_the_href(self):
        text = normalize_graph_body(
            html_body(f'<p>Here is the flyer: <a href="{FLYER}">Flyer</a></p>')
        )
        self.assertIn("Flyer", text)
        self.assertNotIn(FLYER, text)
        self.assertNotIn("http", text)


class RecoverHtmlLinkTargetsTests(unittest.TestCase):
    def test_hrefs_are_recovered_in_document_order_with_their_anchor_text(self):
        recovered = recover_html_link_targets(
            html_body(
                f'<a href="{FLYER}">Flyer</a> and <a href="{IABS}">'
                "Information About Brokerage Services</a>"
            )
        )
        self.assertEqual([url for url, _text in recovered], [FLYER, IABS])
        self.assertEqual(recovered[0][1], "Flyer")

    def test_single_quoted_and_unquoted_hrefs_are_both_recovered(self):
        recovered = recover_html_link_targets(
            html_body(
                f"<a href='{FLYER}'>a</a><a href={IABS}>b</a>"
            )
        )
        self.assertEqual([url for url, _text in recovered], [FLYER, IABS])

    def test_non_http_schemes_are_not_links_to_fetch(self):
        recovered = recover_html_link_targets(
            html_body(
                '<a href="mailto:someone@example.test">mail</a>'
                '<a href="tel:+15550000000">call</a>'
                '<a href="cid:image001.png">img</a>'
                '<a href="#top">top</a>'
                '<a href="javascript:void(0)">x</a>'
            )
        )
        self.assertEqual(recovered, [])

    def test_a_repeated_href_is_recovered_once(self):
        recovered = recover_html_link_targets(
            html_body(f'<a href="{FLYER}">a</a><a href="{FLYER}">b</a>')
        )
        self.assertEqual(len(recovered), 1)

    def test_a_plain_text_body_yields_nothing(self):
        self.assertEqual(
            recover_html_link_targets(
                {"contentType": "Text", "content": f"see {FLYER}"}
            ),
            [],
        )

    def test_a_missing_or_malformed_body_yields_nothing(self):
        self.assertEqual(recover_html_link_targets({}), [])
        self.assertEqual(recover_html_link_targets(None), [])
        self.assertEqual(recover_html_link_targets({"contentType": "HTML"}), [])

    def test_an_unclosed_anchor_is_still_recovered(self):
        recovered = recover_html_link_targets(
            html_body(f'<p><a href="{FLYER}">Flyer<p>next paragraph')
        )
        self.assertEqual([url for url, _text in recovered], [FLYER])

    def test_an_anchor_does_not_absorb_the_next_anchors_words(self):
        # Mail HTML is not reliably well-formed. If an unclosed flyer anchor
        # swallowed the following disclosure link's text, the flyer itself
        # would be suppressed as boilerplate -- silently, and in the direction
        # of losing the payload.
        recovered = recover_html_link_targets(
            html_body(
                f'<a href="{FLYER}">Flyer'
                f'<a href="{IABS}">Information About Brokerage Services</a>'
            )
        )
        self.assertEqual([url for url, _text in recovered], [FLYER, IABS])
        self.assertEqual(recovered[0][1], "Flyer")
        self.assertFalse(is_boilerplate_broker_link(*recovered[0]))

    def test_html_entities_in_a_query_string_are_decoded(self):
        recovered = recover_html_link_targets(
            html_body('<a href="https://h.test/f?a=1&amp;b=2">f</a>')
        )
        self.assertEqual(recovered[0][0], "https://h.test/f?a=1&b=2")


class BoilerplateFamilyTests(unittest.TestCase):
    """The families are the ones actually read out of the stored corpus."""

    def test_the_families_found_in_the_corpus_are_recognised(self):
        cases = [
            (IABS, "Information About Brokerage Services"),
            ("https://h.test/iabs", "TREC IABS"),
            # Both TREC domains appear in the stored corpus; the older one is
            # here because a broker typed it, not because it was guessed.
            ("https://www.trec.state.tx.us/forms/iabs.pdf", "form"),
            ("https://h.test/x", "Click here to report this email as spam"),
            ("https://h.test/x", "please click here"),  # privacy notice tail
            ("https://h.test/unsubscribe", "Unsubscribe"),
            ("https://h.test/x", "Manage preferences"),
            ("https://h.test/x", "View this email in your browser"),
            ("https://linkedin.com/in/someone", "LinkedIn"),
            ("https://h.test/x", "Click Here For All Available Inventory"),
        ]
        for url, text in cases:
            with self.subTest(text=text):
                self.assertTrue(
                    is_boilerplate_broker_link(url, text),
                    f"{text!r} should be recognised as footer boilerplate",
                )

    def test_real_broker_payload_is_not_boilerplate(self):
        cases = [
            (FLYER, "Flyer"),
            ("https://h.test/marketing-package.pdf", "Marketing package"),
            ("https://h.test/x", "Texas Glocal | Partners"),
            ("https://h.test/x", "1903 Pinewoods Way, Spring, TX 77386"),
            ("https://h.test/floorplan.pdf", "Floor plan"),
            ("https://h.test/x", ""),
        ]
        for url, text in cases:
            with self.subTest(text=text):
                self.assertFalse(
                    is_boilerplate_broker_link(url, text),
                    f"{text!r} must not be filtered as boilerplate",
                )

    def test_an_unrecognised_link_is_kept(self):
        self.assertFalse(is_boilerplate_broker_link("https://h.test/thing", "more"))


class AssetLinkCandidateTests(unittest.TestCase):
    def test_a_hyperlinked_flyer_reaches_the_asset_lane(self):
        self.assertEqual(
            asset_link_candidates([], html_body(f'<a href="{FLYER}">Flyer</a>')),
            [FLYER],
        )

    def test_disclosure_boilerplate_does_not(self):
        self.assertEqual(
            asset_link_candidates(
                [],
                html_body(
                    f'<a href="{FLYER}">Flyer</a>'
                    f'<a href="{IABS}">Information About Brokerage Services</a>'
                ),
            ),
            [FLYER],
        )

    def test_visibly_typed_urls_keep_their_priority(self):
        typed = "https://typed.test/a.pdf"
        self.assertEqual(
            asset_link_candidates([typed], html_body(f'<a href="{FLYER}">Flyer</a>')),
            [typed, FLYER],
        )

    def test_a_url_that_is_both_typed_and_hyperlinked_appears_once(self):
        self.assertEqual(
            asset_link_candidates([FLYER], html_body(f'<a href="{FLYER}">Flyer</a>')),
            [FLYER],
        )

    def test_the_candidate_list_is_capped(self):
        inner = "".join(
            f'<a href="https://h.test/f{i}.pdf">Flyer {i}</a>' for i in range(10)
        )
        self.assertEqual(len(asset_link_candidates([], html_body(inner))), 3)

    def test_typed_urls_are_never_dropped_by_the_boilerplate_filter(self):
        # Today's behaviour for a visibly typed URL is preserved exactly: the
        # filter applies only to links recovered from markup.
        self.assertEqual(asset_link_candidates([IABS], html_body("")), [IABS])

    def test_nothing_is_invented_from_an_empty_message(self):
        self.assertEqual(asset_link_candidates([], html_body("")), [])
        self.assertEqual(asset_link_candidates(None, None), [])


if __name__ == "__main__":
    unittest.main()
