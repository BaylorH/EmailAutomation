import os
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("E2E_TEST_MODE", "true")

from email_automation import ai_processing, column_config, processing
from email_automation.column_config import (
    get_column_config_error,
    get_default_column_config,
    get_default_mode_for_canonical,
)


class CanonicalColumnModeDefaultsTests(unittest.TestCase):
    def test_rent_is_ask_required_and_flyer_is_note(self):
        config = get_default_column_config()

        self.assertEqual("ask_required", get_default_mode_for_canonical("rent_sf_yr"))
        self.assertIn("rent_sf_yr", config["requiredFields"])
        self.assertNotIn("rent_sf_yr", config["neverRequest"])

        self.assertEqual("note", get_default_mode_for_canonical("flyer_link"))
        self.assertNotIn("flyer_link", config["requiredFields"])
        self.assertIn("flyer_link", config["neverRequest"])


class ColumnConfigFailClosedTests(unittest.TestCase):
    def _propose(self, column_config, extraction_fields=None):
        return ai_processing.propose_sheet_updates(
            "uid",
            "client",
            "broker@example.com",
            "sheet",
            ["Property Address", "Rent/SF /Yr", "Flyer / Link"],
            3,
            ["123 Main St", "", ""],
            "thread",
            conversation=[
                {
                    "direction": "inbound",
                    "from": "broker@example.com",
                    "content": "The space is available.",
                }
            ],
            column_config=column_config,
            extraction_fields=extraction_fields,
            dry_run=True,
        )

    def test_missing_or_malformed_config_never_reaches_openai(self):
        malformed_configs = [
            None,
            {},
            {"mappings": []},
            {
                "mappings": {"rent_sf_yr": "Rent/SF /Yr"},
                "requiredFields": "rent_sf_yr",
                "formulaFields": [],
                "neverRequest": [],
                "customFields": {},
            },
        ]

        for column_config in malformed_configs:
            with self.subTest(column_config=column_config), patch.object(
                ai_processing.client.responses,
                "create",
            ) as create:
                proposal = self._propose(column_config)

                self.assertIsNone(proposal)
                create.assert_not_called()

    def test_duplicate_extraction_fields_drift_fails_closed(self):
        with patch.object(ai_processing.client.responses, "create") as create:
            proposal = self._propose(
                get_default_column_config(),
                extraction_fields=["flyer_link"],
            )

        self.assertIsNone(proposal)
        create.assert_not_called()


class BrokerReplyColumnModeValidationTests(unittest.TestCase):
    def setUp(self):
        self.config = get_default_column_config()
        self.config["customFields"] = {
            "Broker Context": {"mode": "note", "description": "Context only"},
            "Internal Score": {"mode": "skip", "description": "Ignored"},
            "Building Condition Notes": {"mode": "note", "description": "Condition context"},
            "Condition Notes": {"mode": "note", "description": "Generic condition context"},
        }

    def test_accepts_request_for_missing_ask_field_only(self):
        body = "Thanks for the details. Could you also confirm the asking rent?"

        self.assertTrue(
            processing._response_mentions_missing_fields(
                body,
                ["Rent/SF /Yr"],
                self.config,
            )
        )

    def test_requested_ask_fields_returns_every_explicit_question_target(self):
        helper = getattr(column_config, "get_requested_ask_fields", None)

        self.assertTrue(callable(helper))
        self.assertEqual(
            {"rent/sf /yr", "ops ex /sf"},
            set(helper(
                "Could you confirm both the asking rent and operating expenses?",
                self.config,
            )),
        )

    def test_requested_ask_fields_fails_closed_for_malformed_config(self):
        helper = getattr(column_config, "get_requested_ask_fields", None)

        self.assertTrue(callable(helper))
        self.assertEqual(
            [],
            helper(
                "Could you confirm both the asking rent and operating expenses?",
                {"mappings": self.config["mappings"]},
            ),
        )

    def test_rejects_question_reasking_known_ask_field_with_missing_field(self):
        body = "Could you confirm both the asking rent and operating expenses?"

        self.assertFalse(
            processing._response_mentions_missing_fields(
                body,
                ["Ops Ex /SF"],
                self.config,
            )
        )

    def test_rejects_direct_structural_questions_for_known_ask_fields(self):
        cases = (
            (
                "Could you confirm operating expenses? What is the asking rent?",
                {"ops ex /sf", "rent/sf /yr"},
            ),
            (
                "Could you confirm operating expenses? How many docks are there?",
                {"ops ex /sf", "docks"},
            ),
            (
                "Could you confirm operating expenses? Do the premises have docks?",
                {"ops ex /sf", "docks"},
            ),
        )
        helper = getattr(column_config, "get_requested_ask_fields", None)

        self.assertTrue(callable(helper))
        for body, expected_fields in cases:
            with self.subTest(body=body):
                self.assertEqual(
                    expected_fields,
                    set(helper(body, self.config)),
                )
                self.assertFalse(
                    processing._response_mentions_missing_fields(
                        body,
                        ["Ops Ex /SF"],
                        self.config,
                    )
                )

    def test_rejects_structural_questions_and_non_question_reask_forms(self):
        cases = (
            (
                "Could you confirm operating expenses? What's the asking rent?",
                {"ops ex /sf", "rent/sf /yr"},
            ),
            ("What’s the asking rent?", {"rent/sf /yr"}),
            ("Do you happen to know the asking rent?", {"rent/sf /yr"}),
            ("Let me know the asking rent.", {"rent/sf /yr"}),
            ("I'd like to know the asking rent.", {"rent/sf /yr"}),
        )
        helper = getattr(column_config, "get_requested_ask_fields", None)

        self.assertTrue(callable(helper))
        for body, expected_fields in cases:
            with self.subTest(body=body):
                self.assertEqual(
                    expected_fields,
                    set(helper(body, self.config)),
                )
                self.assertFalse(
                    processing._response_mentions_missing_fields(
                        body,
                        ["Ops Ex /SF"],
                        self.config,
                    )
                )

    def test_unclassified_known_ask_mentions_fail_closed(self):
        cases = (
            (
                "Could you confirm operating expenses? What's the asking rent per sq. ft.?",
                {"ops ex /sf", "rent/sf /yr"},
            ),
            ("What's the asking rent\nfor this property?", {"rent/sf /yr"}),
            (
                "Could you confirm operating expenses? I'd also like the asking rent.",
                {"ops ex /sf", "rent/sf /yr"},
            ),
            ("I'd like the asking rent.", {"rent/sf /yr"}),
            ("I'm curious about the asking rent.", {"rent/sf /yr"}),
            ("Kindly confirm the asking rent.", {"rent/sf /yr"}),
            ("Tell me the asking rent.", {"rent/sf /yr"}),
            (
                "Thanks for confirming operating expenses, and I'd like the asking rent.",
                {"rent/sf /yr"},
            ),
            ("Tell me if the premises do have docks.", {"docks"}),
            (
                "Could you confirm operating expenses? We don't need the flyer — what's the asking rent?",
                {"ops ex /sf", "rent/sf /yr"},
            ),
        )
        helper = getattr(column_config, "get_requested_ask_fields", None)

        self.assertTrue(callable(helper))
        for body, expected_fields in cases:
            with self.subTest(body=body):
                self.assertEqual(
                    expected_fields,
                    set(helper(body, self.config)),
                )
                self.assertFalse(
                    processing._response_mentions_missing_fields(
                        body,
                        ["Ops Ex /SF"],
                        self.config,
                    )
                )
        self.assertFalse(
            processing._response_requests_nonrequestable_fields(
                cases[-1][0],
                self.config,
            )
        )

    def test_soft_wrapped_multiword_fields_keep_their_configured_modes(self):
        self.config["customFields"]["Loading Capacity Details"] = {
            "mode": "ask_required",
            "description": "Required loading capacity",
        }
        helper = getattr(column_config, "get_requested_ask_fields", None)

        self.assertTrue(callable(helper))
        body = "Could you confirm operating expenses? What is the loading\ncapacity?"
        with self.subTest(mode="ask_required"):
            self.assertEqual(
                {"ops ex /sf", "Loading Capacity Details"},
                set(helper(body, self.config)),
            )
            self.assertFalse(
                processing._response_mentions_missing_fields(
                    body,
                    ["Ops Ex /SF"],
                    self.config,
                )
            )

        for mode, body in (
            (
                "note",
                "Could you confirm operating expenses? Could you share the building\ncondition?",
            ),
            (
                "formula",
                "Could you confirm operating expenses? What is the gross\nrent?",
            ),
        ):
            with self.subTest(mode=mode):
                self.assertTrue(
                    processing._response_requests_nonrequestable_fields(
                        body,
                        self.config,
                    )
                )

    def test_bullet_and_paragraph_boundaries_do_not_join_field_aliases(self):
        self.config["customFields"]["Loading Capacity Details"] = {
            "mode": "ask_required",
            "description": "Required loading capacity",
        }
        bodies = (
            "Could you confirm operating expenses?\n- loading\n- capacity",
            "Could you confirm operating expenses?\nloading\n\ncapacity",
        )

        for body in bodies:
            with self.subTest(body=body):
                self.assertEqual(
                    {"ops ex /sf"},
                    set(column_config.get_requested_ask_fields(body, self.config)),
                )
                self.assertTrue(
                    processing._response_mentions_missing_fields(
                        body,
                        ["Ops Ex /SF"],
                        self.config,
                    )
                )

    def test_multiword_aliases_accept_whitespace_or_hyphen_separators(self):
        self.config["mappings"]["docks"] = "Dock High Doors"
        cases = (
            (
                "Could you confirm operating-expenses and the asking rent?",
                ["Rent/SF /Yr"],
                {"ops ex /sf", "rent/sf /yr"},
            ),
            (
                "Could you confirm common-area maintenance and the asking rent?",
                ["Rent/SF /Yr"],
                {"ops ex /sf", "rent/sf /yr"},
            ),
            (
                "Could you confirm common\u00adarea maintenance and the asking rent?",
                ["Rent/SF /Yr"],
                {"ops ex /sf", "rent/sf /yr"},
            ),
            (
                "Could you confirm square-footage and the asking rent?",
                ["Rent/SF /Yr"],
                {"total sf", "rent/sf /yr"},
            ),
            (
                "Could you confirm drive-in doors and operating expenses?",
                ["Ops Ex /SF"],
                {"drive ins", "ops ex /sf"},
            ),
            (
                "Could you confirm dock-high doors and operating expenses?",
                ["Ops Ex /SF"],
                {"Dock High Doors", "ops ex /sf"},
            ),
        )
        helper = getattr(column_config, "get_requested_ask_fields", None)

        self.assertTrue(callable(helper))
        for body, missing_fields, expected_fields in cases:
            with self.subTest(body=body):
                self.assertEqual(
                    expected_fields,
                    set(helper(body, self.config)),
                )
                self.assertFalse(
                    processing._response_mentions_missing_fields(
                        body,
                        missing_fields,
                        self.config,
                    )
                )

    def test_hyphenated_soft_wrap_matches_multiword_alias(self):
        body = "Could you confirm common-\narea maintenance and the asking rent?"

        self.assertEqual(
            {"ops ex /sf", "rent/sf /yr"},
            set(column_config.get_requested_ask_fields(body, self.config)),
        )
        self.assertFalse(
            processing._response_mentions_missing_fields(
                body,
                ["Rent/SF /Yr"],
                self.config,
            )
        )

    def test_just_or_only_field_span_overrides_earlier_no_need_marker(self):
        for qualifier in ("just", "only"):
            with self.subTest(qualifier=qualifier, field="flyer"):
                body = (
                    "Could you confirm operating expenses? We don't need anything "
                    f"else, {qualifier} the flyer."
                )
                self.assertTrue(
                    processing._response_requests_nonrequestable_fields(
                        body,
                        self.config,
                    )
                )

            with self.subTest(qualifier=qualifier, field="asking rent"):
                body = (
                    "Could you confirm operating expenses? We don't need the flyer, "
                    f"{qualifier} the asking rent."
                )
                self.assertEqual(
                    {"ops ex /sf", "rent/sf /yr"},
                    set(column_config.get_requested_ask_fields(body, self.config)),
                )
                self.assertFalse(
                    processing._response_mentions_missing_fields(
                        body,
                        ["Ops Ex /SF"],
                        self.config,
                    )
                )
                self.assertFalse(
                    processing._response_requests_nonrequestable_fields(
                        body,
                        self.config,
                    )
                )

    def test_unspaced_hyphen_starts_a_new_field_intent_context(self):
        body = "No need to confirm the asking rent-just operating expenses?"

        self.assertEqual(
            {"ops ex /sf"},
            set(column_config.get_requested_ask_fields(body, self.config)),
        )
        self.assertTrue(
            processing._response_mentions_missing_fields(
                body,
                ["Ops Ex /SF"],
                self.config,
            )
        )

    def test_local_question_after_informational_field_does_not_inherit_exemption(self):
        for separator in (": ", "\u2014", "\u2013"):
            body = (
                "Could you confirm operating expenses? Here is the flyer"
                f"{separator}what's the asking rent?"
            )
            with self.subTest(separator=separator):
                self.assertEqual(
                    {"ops ex /sf", "rent/sf /yr"},
                    set(column_config.get_requested_ask_fields(body, self.config)),
                )
                self.assertFalse(
                    processing._response_mentions_missing_fields(
                        body,
                        ["Ops Ex /SF"],
                        self.config,
                    )
                )
                self.assertFalse(
                    processing._response_requests_nonrequestable_fields(
                        body,
                        self.config,
                    )
                )

        self.assertTrue(
            processing._response_requests_nonrequestable_fields(
                "It would be possible to send the flyer?",
                self.config,
            )
        )
        self.assertTrue(
            processing._response_requests_nonrequestable_fields(
                "Here is the flyer? Could you confirm operating expenses?",
                self.config,
            )
        )

    def test_request_evidence_overrides_later_direct_benign_marker(self):
        rent_body = (
            "Could you confirm operating expenses? "
            "Let me know if you already have the asking rent."
        )
        with self.subTest(field="asking rent"):
            self.assertEqual(
                {"ops ex /sf", "rent/sf /yr"},
                set(column_config.get_requested_ask_fields(rent_body, self.config)),
            )
            self.assertFalse(
                processing._response_mentions_missing_fields(
                    rent_body,
                    ["Ops Ex /SF"],
                    self.config,
                )
            )

        flyer_body = (
            "Could you confirm operating expenses? "
            "Let me know if you already have the flyer."
        )
        with self.subTest(field="flyer"):
            self.assertTrue(
                processing._response_requests_nonrequestable_fields(
                    flyer_body,
                    self.config,
                )
            )

    def test_immediately_following_anaphoric_request_binds_to_factual_field(self):
        cases = (
            "The asking rent is $12/SF; can you confirm that?",
            "The asking rent is $12/SF. Could you verify it?",
            "The asking rent is $12/SF; please check this.",
            "The asking rent is $12/SF. Can you confirm?",
            "The asking rent is $12/SF. Please confirm.",
            "The asking rent is $12/SF. Correct?",
        )

        for body in cases:
            with self.subTest(body=body):
                self.assertEqual(
                    {"rent/sf /yr"},
                    set(column_config.get_requested_ask_fields(body, self.config)),
                )
                self.assertFalse(
                    processing._response_mentions_missing_fields(
                        body,
                        ["Ops Ex /SF"],
                        self.config,
                    )
                )

        self.assertEqual(
            [],
            column_config.get_requested_ask_fields(
                "The asking rent is $12/SF. The property is available. "
                "Can you confirm that?",
                self.config,
            ),
        )

    def test_plural_anaphoric_request_binds_all_fields_in_prior_context(self):
        body = (
            "The asking rent is $12/SF and operating expenses are $3/SF; "
            "can you confirm those?"
        )

        self.assertEqual(
            {"rent/sf /yr", "ops ex /sf"},
            set(column_config.get_requested_ask_fields(body, self.config)),
        )
        self.assertFalse(
            processing._response_mentions_missing_fields(
                body,
                ["Ops Ex /SF"],
                self.config,
            )
        )

    def test_unicode_word_hyphens_preserve_formula_alias(self):
        for separator in ("\u2010", "\u2011"):
            body = f"Could you confirm gross{separator}rent?"
            with self.subTest(separator=separator):
                self.assertEqual(
                    [],
                    column_config.get_requested_ask_fields(body, self.config),
                )
                self.assertTrue(
                    processing._response_requests_nonrequestable_fields(
                        body,
                        self.config,
                    )
                )
                self.assertFalse(
                    processing._response_mentions_missing_fields(
                        body,
                        ["Rent/SF /Yr"],
                        self.config,
                    )
                )

    def test_trailing_and_hopped_requests_bind_to_prior_field(self):
        rent_bodies = (
            "asking rent: $12/SF. Can you confirm that?",
            "The asking rent is $12/SF. Can you confirm that again?",
            "The asking rent is $12/SF please confirm.",
            "The asking rent is $12/SF. Is that correct?",
        )

        for body in rent_bodies:
            with self.subTest(body=body):
                self.assertEqual(
                    {"rent/sf /yr"},
                    set(column_config.get_requested_ask_fields(body, self.config)),
                )
                self.assertFalse(
                    processing._response_mentions_missing_fields(
                        body,
                        ["Ops Ex /SF"],
                        self.config,
                    )
                )

        flyer_bodies = (
            "The flyer is available. Please send it.",
            "Flyer: available please send.",
        )
        for body in flyer_bodies:
            with self.subTest(body=body):
                self.assertTrue(
                    processing._response_requests_nonrequestable_fields(
                        body,
                        self.config,
                    )
                )

    def test_fieldless_request_verbs_and_anaphors_bind_bounded_context(self):
        for verb in ("confirm", "verify", "check"):
            body = f"The asking rent is $12/SF. Please {verb} it."
            with self.subTest(verb=verb):
                self.assertEqual(
                    {"rent/sf /yr"},
                    set(column_config.get_requested_ask_fields(body, self.config)),
                )

        for verb in ("send", "share", "provide", "attach", "forward"):
            body = f"The flyer is available. Please {verb} it."
            with self.subTest(verb=verb):
                self.assertTrue(
                    processing._response_requests_nonrequestable_fields(
                        body,
                        self.config,
                    )
                )

        for anaphor in ("these", "both", "those values"):
            body = (
                "The asking rent is $12/SF and operating expenses are $3/SF; "
                f"can you confirm {anaphor}?"
            )
            with self.subTest(anaphor=anaphor):
                self.assertEqual(
                    {"rent/sf /yr", "ops ex /sf"},
                    set(column_config.get_requested_ask_fields(body, self.config)),
                )

        nearest_body = (
            "The asking rent is $12/SF and operating expenses are $3/SF; "
            "can you confirm that?"
        )
        self.assertEqual(
            {"rent/sf /yr", "ops ex /sf"},
            set(column_config.get_requested_ask_fields(nearest_body, self.config)),
        )
        self.assertEqual(
            [],
            column_config.get_requested_ask_fields(
                "The asking rent is $12/SF. The property is available. "
                "Please confirm it.",
                self.config,
            ),
        )

    def test_unknown_fieldless_requests_bind_the_prior_field_context(self):
        rent_bodies = (
            "The asking rent is $12/SF. Can you reconfirm that?",
            "The asking rent is $12/SF. Does that look right?",
            "The asking rent is $12/SF. Reconfirm that.",
            "The asking rent is $12/SF. That's correct?",
            "The asking rent is $12/SF. Isn't it?",
        )

        for body in rent_bodies:
            with self.subTest(body=body):
                self.assertEqual(
                    {"rent/sf /yr"},
                    set(column_config.get_requested_ask_fields(body, self.config)),
                )
                self.assertFalse(
                    processing._response_mentions_missing_fields(
                        body,
                        ["Ops Ex /SF"],
                        self.config,
                    )
                )

        for body in (
            "The flyer is available. Please resend it.",
            "The flyer is available. Please email it.",
        ):
            with self.subTest(body=body):
                self.assertTrue(
                    processing._response_requests_nonrequestable_fields(
                        body,
                        self.config,
                    )
                )

        bare_rent_body = (
            "The asking rent is $12/SF. Confirm. "
            "Could you confirm operating expenses?"
        )
        self.assertEqual(
            {"rent/sf /yr", "ops ex /sf"},
            set(column_config.get_requested_ask_fields(
                bare_rent_body,
                self.config,
            )),
        )
        bare_flyer_body = (
            "The flyer is available. Send. Could you confirm operating expenses?"
        )
        self.assertTrue(
            processing._response_requests_nonrequestable_fields(
                bare_flyer_body,
                self.config,
            )
        )

        deontic_rent_body = (
            "The asking rent is $12/SF. You have to confirm. "
            "Could you confirm operating expenses?"
        )
        self.assertEqual(
            {"rent/sf /yr", "ops ex /sf"},
            set(column_config.get_requested_ask_fields(
                deontic_rent_body,
                self.config,
            )),
        )
        deontic_flyer_body = (
            "The flyer is available. You have to send it. "
            "Could you confirm operating expenses?"
        )
        self.assertTrue(
            processing._response_requests_nonrequestable_fields(
                deontic_flyer_body,
                self.config,
            )
        )

    def test_forward_request_leadin_overrides_factual_looking_bullets(self):
        rent_body = (
            "Could you confirm the following?\n"
            "- Asking rent: $12/SF\n"
            "- Operating expenses"
        )
        self.assertEqual(
            {"rent/sf /yr", "ops ex /sf"},
            set(column_config.get_requested_ask_fields(rent_body, self.config)),
        )
        self.assertFalse(
            processing._response_mentions_missing_fields(
                rent_body,
                ["Ops Ex /SF"],
                self.config,
            )
        )

        flyer_body = (
            "Could you confirm the following?\n"
            "- Flyer: available\n"
            "- Operating expenses"
        )
        self.assertTrue(
            processing._response_requests_nonrequestable_fields(
                flyer_body,
                self.config,
            )
        )

        all_factual_body = (
            "Could you confirm the following?\n"
            "- Asking rent: $12/SF\n"
            "- Flyer: available"
        )
        self.assertTrue(
            processing._response_requests_nonrequestable_fields(
                all_factual_body,
                self.config,
            )
        )

    def test_forward_request_list_state_survives_headings_and_pending_blanks(self):
        heading_body = (
            "Could you confirm the following?\n"
            "Property details:\n"
            "Availability details:\n"
            "- Asking rent: $12/SF\n"
            "- Operating expenses"
        )
        self.assertEqual(
            {"rent/sf /yr", "ops ex /sf"},
            set(column_config.get_requested_ask_fields(heading_body, self.config)),
        )
        self.assertFalse(
            processing._response_mentions_missing_fields(
                heading_body,
                ["Ops Ex /SF"],
                self.config,
            )
        )

        fieldful_leadin_body = (
            "Could you confirm docks and the following?\n"
            "Property details:\n"
            "- Asking rent: $12/SF\n"
            "- Flyer: available"
        )
        self.assertEqual(
            {"docks", "rent/sf /yr"},
            set(column_config.get_requested_ask_fields(
                fieldful_leadin_body,
                self.config,
            )),
        )
        self.assertTrue(
            processing._response_requests_nonrequestable_fields(
                fieldful_leadin_body,
                self.config,
            )
        )

        blank_before_bullets_body = (
            "Could you confirm the following?\n"
            "Property details:\n\n"
            "- Asking rent: $12/SF\n"
            "- Flyer: available"
        )
        self.assertEqual(
            {"rent/sf /yr"},
            set(column_config.get_requested_ask_fields(
                blank_before_bullets_body,
                self.config,
            )),
        )
        self.assertTrue(
            processing._response_requests_nonrequestable_fields(
                blank_before_bullets_body,
                self.config,
            )
        )

        ended_list_body = (
            "Could you confirm the following?\n"
            "- Operating expenses\n\n"
            "For reference, the asking rent is $12/SF."
        )
        self.assertEqual(
            {"ops ex /sf"},
            set(column_config.get_requested_ask_fields(ended_list_body, self.config)),
        )
        self.assertTrue(
            processing._response_mentions_missing_fields(
                ended_list_body,
                ["Ops Ex /SF"],
                self.config,
            )
        )

    def test_explicit_request_scope_governs_bounded_field_clusters(self):
        ask_bodies = (
            (
                "Please provide these details\n"
                "- Asking rent: $12/SF\n"
                "- Operating expenses"
            ),
            (
                "Can you share this information\n"
                "- Asking rent: $12/SF\n"
                "- Operating expenses"
            ),
            (
                "Please provide these details\n"
                "Asking rent: $12/SF\n"
                "Operating expenses"
            ),
        )
        for body in ask_bodies:
            with self.subTest(body=body):
                self.assertEqual(
                    {"rent/sf /yr", "ops ex /sf"},
                    set(column_config.get_requested_ask_fields(body, self.config)),
                )
                self.assertFalse(
                    processing._response_mentions_missing_fields(
                        body,
                        ["Ops Ex /SF"],
                        self.config,
                    )
                )

        nonrequestable_bodies = (
            (
                "Could you confirm these details?\n"
                "- Flyer: available\n"
                "- Operating expenses"
            ),
            (
                "Please provide the details below\n"
                "- Gross rent: $24/SF\n"
                "- Operating expenses"
            ),
        )
        for body in nonrequestable_bodies:
            with self.subTest(body=body):
                self.assertTrue(
                    processing._response_requests_nonrequestable_fields(
                        body,
                        self.config,
                    )
                )

        inline_body = (
            "Could you please confirm: flyer: available, operating expenses?"
        )
        with self.subTest(body=inline_body):
            self.assertEqual(
                {"ops ex /sf"},
                set(column_config.get_requested_ask_fields(inline_body, self.config)),
            )
            self.assertTrue(
                processing._response_requests_nonrequestable_fields(
                    inline_body,
                    self.config,
                )
            )

        heading_body = (
            "Could you confirm these details?\n"
            "Property details:\n\n\n"
            "- Asking rent: $12/SF\n"
            "- Flyer: available"
        )
        with self.subTest(body=heading_body):
            self.assertEqual(
                {"rent/sf /yr"},
                set(column_config.get_requested_ask_fields(
                    heading_body,
                    self.config,
                )),
            )
            self.assertTrue(
                processing._response_requests_nonrequestable_fields(
                    heading_body,
                    self.config,
                )
            )

        fieldful_leadin_body = (
            "Could you confirm docks and these details?\n"
            "- Asking rent: $12/SF\n"
            "- Flyer: available"
        )
        with self.subTest(body=fieldful_leadin_body):
            self.assertEqual(
                {"docks", "rent/sf /yr"},
                set(column_config.get_requested_ask_fields(
                    fieldful_leadin_body,
                    self.config,
                )),
            )
            self.assertTrue(
                processing._response_requests_nonrequestable_fields(
                    fieldful_leadin_body,
                    self.config,
                )
            )

        spaced_bullets_body = (
            "Please provide these details\n"
            "- Operating expenses\n\n"
            "- Asking rent: $12/SF\n"
            "- Flyer: available"
        )
        with self.subTest(body=spaced_bullets_body):
            self.assertEqual(
                {"ops ex /sf", "rent/sf /yr"},
                set(column_config.get_requested_ask_fields(
                    spaced_bullets_body,
                    self.config,
                )),
            )
            self.assertTrue(
                processing._response_requests_nonrequestable_fields(
                    spaced_bullets_body,
                    self.config,
                )
            )

        ended_scope_body = (
            "Please provide these details\n"
            "Operating expenses\n\n"
            "For reference, the flyer is available."
        )
        self.assertEqual(
            {"ops ex /sf"},
            set(column_config.get_requested_ask_fields(
                ended_scope_body,
                self.config,
            )),
        )
        self.assertFalse(
            processing._response_requests_nonrequestable_fields(
                ended_scope_body,
                self.config,
            )
        )

        for body in (
            (
                "Please provide these details\n"
                "Operating expenses\n"
                "For reference, the flyer is available."
            ),
            "Please confirm when convenient; for reference: the flyer is available.",
        ):
            with self.subTest(body=body):
                self.assertFalse(
                    processing._response_requests_nonrequestable_fields(
                        body,
                        self.config,
                    )
                )

    def test_explicit_anaphora_uses_bounded_recent_antecedents(self):
        plural_body = (
            "The asking rent is $12/SF. Operating expenses are $3/SF. "
            "Can you confirm both?"
        )
        with self.subTest(body=plural_body):
            self.assertEqual(
                {"rent/sf /yr", "ops ex /sf"},
                set(column_config.get_requested_ask_fields(
                    plural_body,
                    self.config,
                )),
            )
            self.assertFalse(
                processing._response_mentions_missing_fields(
                    plural_body,
                    ["Ops Ex /SF"],
                    self.config,
                )
            )

        flyer_body = "Here is the flyer.\n\nCould you resend it?"
        with self.subTest(body=flyer_body):
            self.assertTrue(
                processing._response_requests_nonrequestable_fields(
                    flyer_body,
                    self.config,
                )
            )

        email_body = "Here is the email address.\n\nCould you resend it?"
        with self.subTest(body=email_body):
            self.assertTrue(
                processing._response_requests_nonrequestable_fields(
                    email_body,
                    self.config,
                )
            )

        rent_body = "The asking rent is $12/SF.\n\nCould you reconfirm it?"
        with self.subTest(body=rent_body):
            self.assertEqual(
                {"rent/sf /yr"},
                set(column_config.get_requested_ask_fields(rent_body, self.config)),
            )

        bounded_plural_body = (
            "The asking rent is $12/SF. Docks are 4. Power is 400A. "
            "Operating expenses are $3/SF. Can you confirm both?"
        )
        self.assertEqual(
            {"power", "ops ex /sf"},
            set(column_config.get_requested_ask_fields(
                bounded_plural_body,
                self.config,
            )),
        )

    def test_every_default_skip_canonical_contributes_nonrequestable_aliases(self):
        groups = [
            set(group)
            for group in column_config.get_non_requestable_field_terms(self.config)
        ]
        skip_canonicals = {
            canonical
            for canonical in self.config["mappings"]
            if get_default_mode_for_canonical(canonical) == "skip"
        }
        for canonical in skip_canonicals:
            expected_terms = set(column_config._canonical_field_reference_terms(
                canonical,
                self.config["mappings"][canonical],
            ))
            with self.subTest(canonical=canonical):
                self.assertTrue(
                    any(expected_terms <= group for group in groups),
                    canonical,
                )

        for term in (
            "email address",
            "property address",
            "building name",
            "leasing contact",
            "leasing company",
            "contact name",
            "municipality",
        ):
            body = f"Could you confirm the {term}?"
            with self.subTest(term=term):
                self.assertTrue(
                    processing._response_requests_nonrequestable_fields(
                        body,
                        self.config,
                    )
                )

        for body in (
            "For this property, could you confirm the asking rent?",
            "Here is the flyer for this property.",
            (
                "The asking rent is $12/SF. The property is available. "
                "Can you confirm it?"
            ),
        ):
            with self.subTest(body=body):
                self.assertFalse(
                    processing._response_requests_nonrequestable_fields(
                        body,
                        self.config,
                    )
                )

        for body in (
            "Could you confirm operating expenses at this property?",
            "Could your company confirm operating expenses?",
            "Could you email the operating expenses?",
            "Please contact me with the operating expenses.",
            "We are interested in this property.",
        ):
            with self.subTest(body=body):
                self.assertFalse(
                    processing._response_requests_nonrequestable_fields(
                        body,
                        self.config,
                    )
                )

    def test_plural_request_crosses_consecutive_colon_value_pairs(self):
        bodies = (
            (
                "Asking rent: $12/SF\n"
                "Operating expenses: $3/SF\n"
                "Can you confirm both values?"
            ),
            (
                "Asking rent: $12/SF; operating expenses: $3/SF; "
                "Are these correct?"
            ),
        )

        for body in bodies:
            with self.subTest(body=body):
                self.assertEqual(
                    {"rent/sf /yr", "ops ex /sf"},
                    set(column_config.get_requested_ask_fields(body, self.config)),
                )
                self.assertFalse(
                    processing._response_mentions_missing_fields(
                        body,
                        ["Ops Ex /SF"],
                        self.config,
                    )
                )

        remote_body = (
            "Asking rent: $12/SF.\n\n"
            "Operating expenses: $3/SF;\n"
            "Can you confirm both values?"
        )
        self.assertEqual(
            {"ops ex /sf"},
            set(column_config.get_requested_ask_fields(remote_body, self.config)),
        )

    def test_ambiguous_anaphora_binds_every_field_in_compound_proposition(self):
        bodies = (
            (
                "Asking rent: $12/SF, operating expenses: $3/SF; "
                "can you confirm both?"
            ),
            (
                "The asking rent is $12/SF and operating expenses are $3/SF. "
                "Does that look right?"
            ),
            (
                "The asking rent is $12/SF, and they quoted operating expenses "
                "at $3/SF. Can you confirm that?"
            ),
        )

        for body in bodies:
            with self.subTest(body=body):
                self.assertEqual(
                    {"rent/sf /yr", "ops ex /sf"},
                    set(column_config.get_requested_ask_fields(body, self.config)),
                )
                self.assertFalse(
                    processing._response_mentions_missing_fields(
                        body,
                        ["Ops Ex /SF"],
                        self.config,
                    )
                )

    def test_excluded_extractable_canonical_uses_every_alias_as_nonrequestable(self):
        self.config["extractionFields"].remove("docks")
        self.config["requiredFields"].remove("docks")
        self.assertIsNone(column_config.get_column_config_error(self.config))
        dock_field = column_config.CANONICAL_FIELDS["docks"]
        aliases = list(dict.fromkeys((
            self.config["mappings"]["docks"],
            dock_field["label"],
            *dock_field["default_aliases"],
            *dock_field["ai_synonyms"],
        )))

        for alias in aliases:
            body = f"Could you confirm {alias}?"
            with self.subTest(alias=alias):
                self.assertEqual(
                    [],
                    column_config.get_requested_ask_fields(body, self.config),
                )
                self.assertTrue(
                    processing._response_requests_nonrequestable_fields(
                        body,
                        self.config,
                    )
                )

    def test_field_reference_separators_and_simple_inflections_are_equivalent(self):
        separators = (
            " ",
            "\n",
            "-",
            "\u00ad",
            "\u2010",
            "\u2011",
            "\u2013",
            "\u2014",
            ".",
            "/",
            "_",
        )
        for separator in separators:
            body = (
                f"Could you confirm operating{separator}expenses and the asking rent?"
            )
            with self.subTest(separator=separator):
                self.assertEqual(
                    {"ops ex /sf", "rent/sf /yr"},
                    set(column_config.get_requested_ask_fields(body, self.config)),
                )
                self.assertFalse(
                    processing._response_mentions_missing_fields(
                        body,
                        ["Rent/SF /Yr"],
                        self.config,
                    )
                )

        for body, expected_fields in (
            (
                "Could you confirm operating expense and the asking rent?",
                {"ops ex /sf", "rent/sf /yr"},
            ),
            (
                "Could you confirm sq. ft. and the asking rent?",
                {"total sf", "rent/sf /yr"},
            ),
            (
                "Could you confirm operating expenses and square foot?",
                {"ops ex /sf", "total sf"},
            ),
        ):
            with self.subTest(body=body):
                self.assertEqual(
                    expected_fields,
                    set(column_config.get_requested_ask_fields(body, self.config)),
                )

        self.config["customFields"]["Loading Capacity"] = {
            "mode": "ask_required",
            "description": "Required loading capacity",
        }
        self.assertEqual(
            {"Loading Capacity"},
            set(column_config.get_requested_ask_fields(
                "Could you confirm loading capacities?",
                self.config,
            )),
        )

    def test_single_token_and_per_token_inflections_are_equivalent(self):
        body = "Could you confirm operating expenses? Is there a dock?"
        self.assertEqual(
            {"ops ex /sf", "docks"},
            set(column_config.get_requested_ask_fields(body, self.config)),
        )
        self.assertFalse(
            processing._response_mentions_missing_fields(
                body,
                ["Ops Ex /SF"],
                self.config,
            )
        )

        self.config["customFields"]["Amenities"] = {
            "mode": "ask_required",
            "description": "Required amenity details",
        }
        self.assertEqual(
            {"Amenities"},
            set(column_config.get_requested_ask_fields(
                "Could you confirm the amenity?",
                self.config,
            )),
        )

        self.config["customFields"]["Amenities"]["mode"] = "skip"
        self.assertTrue(
            processing._response_requests_nonrequestable_fields(
                "Could you confirm the amenity?",
                self.config,
            )
        )

        self.assertEqual(
            {"ops ex /sf", "rent/sf /yr"},
            set(column_config.get_requested_ask_fields(
                "Could you confirm op. ex. and the asking rent?",
                self.config,
            )),
        )
        self.assertEqual(
            [],
            column_config.get_requested_ask_fields(
                "Op. ex. is $3/SF.",
                self.config,
            ),
        )
        self.assertEqual(
            {"ops ex /sf"},
            set(column_config.get_requested_ask_fields(
                "Could you confirm op. ex. The asking rent is $12/SF.",
                self.config,
            )),
        )
        self.assertEqual(
            {"rent/sf /yr"},
            set(column_config.get_requested_ask_fields(
                "Thanks for confirming op. ex. Can you confirm the asking rent?",
                self.config,
            )),
        )

    def test_longer_nonrequestable_alias_dominates_contained_ask_alias(self):
        for separator in ("\u2013", "\u2014", ".", "/", "_"):
            body = f"Could you confirm gross{separator}rent?"
            with self.subTest(separator=separator):
                self.assertEqual(
                    [],
                    column_config.get_requested_ask_fields(body, self.config),
                )
                self.assertTrue(
                    processing._response_requests_nonrequestable_fields(
                        body,
                        self.config,
                    )
                )

    def test_nonrequestable_overlap_dominates_longer_custom_ask_field(self):
        for header in ("Gross Rent Details", "Flyer Availability"):
            with self.subTest(header=header):
                self.config["customFields"][header] = {
                    "mode": "ask_required",
                    "description": "Overlapping custom Ask field",
                }
                body = f"Could you confirm {header}?"

                self.assertEqual(
                    [],
                    column_config.get_requested_ask_fields(body, self.config),
                )
                self.assertTrue(
                    processing._response_requests_nonrequestable_fields(
                        body,
                        self.config,
                    )
                )
                del self.config["customFields"][header]

    def test_terse_acknowledgements_and_facts_remain_benign(self):
        for body in (
            "Asking rent confirmed.",
            "Only the asking rent is $12/SF.",
        ):
            with self.subTest(body=body):
                self.assertEqual(
                    [],
                    column_config.get_requested_ask_fields(body, self.config),
                )

        for body in (
            "Here's the flyer.",
            "Only the flyer is attached.",
        ):
            with self.subTest(body=body):
                self.assertFalse(
                    processing._response_requests_nonrequestable_fields(
                        body,
                        self.config,
                    )
                )

        for body in (
            "The asking rent is $12/SF. Correct.",
            "The asking rent is $12/SF. Right.",
        ):
            with self.subTest(body=body):
                self.assertEqual(
                    [],
                    column_config.get_requested_ask_fields(body, self.config),
                )

        self.assertEqual(
            [],
            column_config.get_requested_ask_fields(
                "Please note:\n- Asking rent: $12/SF",
                self.config,
            ),
        )

        for body in (
            "The flyer is available. Correct.",
            "The flyer is available. Right.",
        ):
            with self.subTest(body=body):
                self.assertFalse(
                    processing._response_requests_nonrequestable_fields(
                        body,
                        self.config,
                    )
                )

    def test_semicolon_acknowledgement_only_requests_missing_field(self):
        body = (
            "Thanks for confirming the asking rent; could you confirm operating expenses?"
        )
        helper = getattr(column_config, "get_requested_ask_fields", None)

        self.assertTrue(callable(helper))
        self.assertEqual(
            {"ops ex /sf"},
            set(helper(body, self.config)),
        )
        self.assertTrue(
            processing._response_mentions_missing_fields(
                body,
                ["Ops Ex /SF"],
                self.config,
            )
        )

    def test_clear_nonrequest_context_does_not_leak_across_comma_clause(self):
        helper = getattr(column_config, "get_requested_ask_fields", None)

        self.assertTrue(callable(helper))
        body = "Thanks for confirming operating expenses, I'd like the asking rent."
        self.assertEqual({"rent/sf /yr"}, set(helper(body, self.config)))
        self.assertFalse(
            processing._response_mentions_missing_fields(
                body,
                ["Ops Ex /SF"],
                self.config,
            )
        )

        body = "We don't need the flyer, I'd like the asking rent."
        self.assertEqual({"rent/sf /yr"}, set(helper(body, self.config)))
        self.assertFalse(
            processing._response_requests_nonrequestable_fields(body, self.config)
        )

    def test_factual_known_ask_value_does_not_become_a_request(self):
        bodies = (
            "The asking rent is $12/SF. Could you confirm operating expenses?",
            "The asking rent: $12/SF. Could you confirm operating expenses?",
        )
        helper = getattr(column_config, "get_requested_ask_fields", None)

        self.assertTrue(callable(helper))
        for body in bodies:
            with self.subTest(body=body):
                self.assertEqual(
                    {"ops ex /sf"},
                    set(helper(body, self.config)),
                )
                self.assertTrue(
                    processing._response_mentions_missing_fields(
                        body,
                        ["Ops Ex /SF"],
                        self.config,
                    )
                )

    def test_declarative_dock_context_does_not_become_a_request(self):
        bodies = (
            "There are many docks at the premises. Could you confirm operating expenses?",
            "The premises do have docks. Could you confirm operating expenses?",
        )
        helper = getattr(column_config, "get_requested_ask_fields", None)

        self.assertTrue(callable(helper))
        for body in bodies:
            with self.subTest(body=body):
                self.assertEqual(
                    {"ops ex /sf"},
                    set(helper(body, self.config)),
                )
                self.assertTrue(
                    processing._response_mentions_missing_fields(
                        body,
                        ["Ops Ex /SF"],
                        self.config,
                    )
                )

    def test_decimal_rate_does_not_split_shared_request_clause(self):
        body = (
            "Could you confirm operating expenses at $3.25/SF and the asking rent?"
        )
        helper = getattr(column_config, "get_requested_ask_fields", None)

        self.assertTrue(callable(helper))
        self.assertEqual(
            {"ops ex /sf", "rent/sf /yr"},
            set(helper(body, self.config)),
        )
        self.assertFalse(
            processing._response_mentions_missing_fields(
                body,
                ["Ops Ex /SF"],
                self.config,
            )
        )

    def test_rejects_semicolon_clauses_reasking_known_ask_field(self):
        body = "Could you confirm the asking rent; please provide operating expenses?"

        self.assertFalse(
            processing._response_mentions_missing_fields(
                body,
                ["Ops Ex /SF"],
                self.config,
            )
        )

    def test_rejects_shared_request_intent_when_known_field_comes_last(self):
        for separator in (",", ";"):
            body = f"Could you confirm operating expenses{separator} asking rent?"

            with self.subTest(separator=separator):
                self.assertFalse(
                    processing._response_mentions_missing_fields(
                        body,
                        ["Ops Ex /SF"],
                        self.config,
                    )
                )

    def test_rejects_conjoined_is_request_for_known_field(self):
        body = (
            "Could you confirm operating expenses, and is the asking rent still $12?"
        )
        helper = getattr(column_config, "get_requested_ask_fields", None)

        self.assertTrue(callable(helper))
        self.assertEqual(
            {"rent/sf /yr", "ops ex /sf"},
            set(helper(body, self.config)),
        )
        self.assertFalse(
            processing._response_mentions_missing_fields(
                body,
                ["Ops Ex /SF"],
                self.config,
            )
        )

    def test_rejects_bullet_list_reasking_known_ask_field(self):
        body = "Could you please provide:\n- asking rent\n- operating expenses"

        self.assertFalse(
            processing._response_mentions_missing_fields(
                body,
                ["Ops Ex /SF"],
                self.config,
            )
        )

    def test_rejects_question_leadin_bullets_reasking_known_ask_field(self):
        body = "Could you provide the following?\n- operating expenses\n- asking rent"

        self.assertFalse(
            processing._response_mentions_missing_fields(
                body,
                ["Ops Ex /SF"],
                self.config,
            )
        )

        helper = getattr(column_config, "get_requested_ask_fields", None)
        self.assertTrue(callable(helper))
        self.assertEqual(
            {"rent/sf /yr", "ops ex /sf"},
            set(helper(body, self.config)),
        )

    def test_known_field_acknowledgement_does_not_become_a_request(self):
        bodies = (
            "Thanks for confirming the asking rent. Could you confirm operating expenses?",
            "Thanks for confirming the asking rent; please provide operating expenses.",
            "Thanks for confirming the asking rent.\nCould you please provide:\n- operating expenses",
            "Please note that the asking rent is already confirmed. Could you confirm operating expenses?",
            "Thanks for confirming the asking rent and could you confirm operating expenses?",
            "We already have the asking rent we need. Could you confirm operating expenses?",
            "The asking rent is already confirmed. Please provide operating expenses.",
        )

        for body in bodies:
            with self.subTest(body=body):
                self.assertTrue(
                    processing._response_mentions_missing_fields(
                        body,
                        ["Ops Ex /SF"],
                        self.config,
                    )
                )

    def test_negated_known_field_request_verb_does_not_become_a_request(self):
        body = "We don't need the asking rent. Could you confirm operating expenses?"
        helper = getattr(column_config, "get_requested_ask_fields", None)

        self.assertTrue(callable(helper))
        self.assertEqual(
            {"ops ex /sf"},
            set(helper(body, self.config)),
        )
        self.assertTrue(
            processing._response_mentions_missing_fields(
                body,
                ["Ops Ex /SF"],
                self.config,
            )
        )

    def test_negated_known_field_ask_clauses_do_not_become_requests(self):
        bodies = (
            "No need to confirm the asking rent. Could you confirm operating expenses?",
            "We don't need to ask about the asking rent. Could you confirm operating expenses?",
        )
        helper = getattr(column_config, "get_requested_ask_fields", None)

        self.assertTrue(callable(helper))
        for body in bodies:
            with self.subTest(body=body):
                self.assertEqual(
                    {"ops ex /sf"},
                    set(helper(body, self.config)),
                )
                self.assertTrue(
                    processing._response_mentions_missing_fields(
                        body,
                        ["Ops Ex /SF"],
                        self.config,
                    )
                )

    def test_configured_rent_header_does_not_trigger_short_size_alias(self):
        body = "Could you confirm the Rent/SF /Yr?"

        self.assertTrue(
            processing._response_mentions_missing_fields(
                body,
                ["Rent/SF /Yr"],
                self.config,
            )
        )

    def test_per_sf_unit_does_not_become_a_total_size_request(self):
        body = "Could you confirm operating expenses per SF?"

        self.assertTrue(
            processing._response_mentions_missing_fields(
                body,
                ["Ops Ex /SF"],
                self.config,
            )
        )

    def test_sf_alias_disambiguation_is_local_to_each_match(self):
        body = "Could you confirm SF and Rent/SF /Yr?"
        helper = getattr(column_config, "get_requested_ask_fields", None)

        self.assertTrue(callable(helper))
        self.assertEqual(
            {"total sf", "rent/sf /yr"},
            set(helper(body, self.config)),
        )
        self.assertFalse(
            processing._response_mentions_missing_fields(
                body,
                ["Rent/SF /Yr"],
                self.config,
            )
        )

    def test_unconfigured_missing_field_is_not_promoted_to_requestable(self):
        self.assertFalse(
            processing._response_mentions_missing_fields(
                "Could you confirm Rail Access?",
                ["Rail Access"],
                get_default_column_config(),
            )
        )

    def test_optional_ask_must_be_deliberately_listed_as_missing(self):
        config = get_default_column_config()
        config["mappings"]["total_sf"] = "Available Size"
        body = "Could you confirm the square footage and asking rent?"

        self.assertFalse(
            processing._response_mentions_missing_fields(
                body,
                ["Rent/SF /Yr"],
                config,
            )
        )
        self.assertTrue(
            processing._response_mentions_missing_fields(
                body,
                ["Rent/SF /Yr", "Available Size"],
                config,
            )
        )

    def test_missing_field_subset_normalizes_configured_header_spacing(self):
        config = get_default_column_config()
        config["mappings"]["total_sf"] = "Available / Size"
        body = "Could you confirm the square footage and asking rent?"

        self.assertTrue(
            processing._response_mentions_missing_fields(
                body,
                ["Rent/SF /Yr", "available/size"],
                config,
            )
        )

    def test_rejects_known_custom_required_ask_field_paraphrase(self):
        config = get_default_column_config()
        config["customFields"]["Loading Capacity Details"] = {
            "mode": "ask_required",
            "description": "Required loading-area capacity",
        }
        body = "Could you confirm the loading capacity and operating expenses?"

        self.assertFalse(
            processing._response_mentions_missing_fields(
                body,
                ["Ops Ex /SF"],
                config,
            )
        )

    def test_formula_mode_drift_cannot_promote_formula_header_to_ask(self):
        config = get_default_column_config()
        config["formulaFields"] = list(config["formulaFields"])
        config["formulaFields"].remove("gross_rent")
        config["extractionFields"].append("gross_rent")
        config["requiredFields"].append("gross_rent")

        self.assertIsNotNone(get_column_config_error(config))
        self.assertFalse(
            processing._response_mentions_missing_fields(
                "Could you confirm Gross Rent?",
                [config["mappings"]["gross_rent"]],
                config,
            )
        )

    def test_unknown_canonical_cannot_be_promoted_from_missing_fields(self):
        config = get_default_column_config()
        config["mappings"]["mystery"] = "Rail Access"
        config["extractionFields"].append("mystery")
        config["requiredFields"].append("mystery")

        self.assertIsNotNone(get_column_config_error(config))
        self.assertFalse(
            processing._response_mentions_missing_fields(
                "Could you confirm Rail Access?",
                ["Rail Access"],
                config,
            )
        )

    def test_benign_listing_link_context_does_not_invalidate_rent_request(self):
        body = (
            "Could you confirm the asking rent, and here is the link to the listing "
            "for context: https://example.com/listing."
        )

        self.assertTrue(
            processing._response_mentions_missing_fields(
                body,
                ["Rent/SF /Yr"],
                self.config,
            )
        )

    def test_short_alias_does_not_match_inside_unrelated_word(self):
        body = "Could you confirm the asking rent? This will be useful for review."

        self.assertTrue(
            processing._response_mentions_missing_fields(
                body,
                ["Rent/SF /Yr"],
                self.config,
            )
        )

    def test_identity_column_words_do_not_count_as_skip_requests(self):
        body = "Could you confirm the asking rent for this city property?"

        self.assertTrue(
            processing._response_mentions_missing_fields(
                body,
                ["Rent/SF /Yr"],
                self.config,
            )
        )

    def test_rejects_allowed_ask_mixed_with_note_field(self):
        body = (
            "Could you confirm the asking rent and also send the flyer or brochure?"
        )

        self.assertFalse(
            processing._response_mentions_missing_fields(
                body,
                ["Rent/SF /Yr"],
                self.config,
            )
        )

    def test_rejects_allowed_ask_mixed_with_custom_skip_field(self):
        body = "Could you confirm the asking rent and your internal score?"

        self.assertFalse(
            processing._response_mentions_missing_fields(
                body,
                ["Rent/SF /Yr"],
                self.config,
            )
        )

    def test_independent_guard_rejects_actual_flyer_request(self):
        for body in (
            "Could you please send the flyer or brochure?",
            "Is there a flyer available?",
            "What about the brochure?",
            "Do you have the flyer?",
            "We are interested in the flyer/listing link.",
            "Any chance you can share the flyer?",
            "Do you know the flyer?",
            "Could I get the flyer?",
            "May I get the flyer?",
            "Would it be possible to send the flyer?",
            "I would appreciate a flyer.",
            "I'm curious about the flyer link.",
        ):
            with self.subTest(body=body):
                self.assertTrue(
                    processing._response_requests_nonrequestable_fields(body, self.config)
                )

    def test_independent_guard_allows_benign_listing_link_context(self):
        body = "Here is the link to the listing for context."

        self.assertFalse(
            processing._response_requests_nonrequestable_fields(body, self.config)
        )

    def test_independent_guard_allows_informational_offer_of_flyer(self):
        for body in (
            "Happy to send over the flyer and asking rate whenever useful.",
            "Here is the flyer you asked for.",
            "I know the flyer is available.",
            "I could get the flyer from our files.",
            "It would be possible to send the flyer later.",
            "I appreciate the flyer you sent.",
        ):
            with self.subTest(body=body):
                self.assertFalse(
                    processing._response_requests_nonrequestable_fields(body, self.config)
                )

    def test_independent_guard_rejects_configured_canonical_and_custom_notes(self):
        for body in (
            "What about the Listing Broker Comments?",
            "Do you have the Client Comments?",
            "We are interested in the Broker Context.",
        ):
            with self.subTest(body=body):
                self.assertTrue(
                    processing._response_requests_nonrequestable_fields(body, self.config)
                )

    def test_custom_note_paraphrase_uses_distinctive_tokens(self):
        self.assertTrue(
            processing._response_requests_nonrequestable_fields(
                "Could you share the building condition?",
                self.config,
            )
        )

    def test_custom_note_paraphrase_does_not_create_single_word_alias(self):
        self.assertFalse(
            processing._response_requests_nonrequestable_fields(
                "Could you confirm the asking rent given the condition?",
                self.config,
            )
        )

    def test_incomplete_legacy_shape_is_rejected_without_guessing_ask_fields(self):
        incomplete = {
            "mappings": self.config["mappings"],
            "requiredFields": self.config["requiredFields"],
            "formulaFields": self.config["formulaFields"],
            "neverRequest": self.config["neverRequest"],
        }

        self.assertTrue(
            processing._response_requests_nonrequestable_fields(
                "Could you confirm the asking rent?",
                incomplete,
            )
        )


class AutomaticResponseScenarioValidationTests(unittest.TestCase):
    def setUp(self):
        self.config = get_default_column_config()
        self.unsafe_llm_body = (
            "Thanks for the update. Could you also send the flyer or brochure?"
        )

    def test_scenario_1_uses_safe_alternative_property_fallback(self):
        body = processing._select_automatic_response_body(
            "nonviable_with_alternative",
            self.unsafe_llm_body,
            self.config,
            "Alex",
        )

        self.assertNotIn("flyer", body.lower())
        self.assertIn("alternative property", body.lower())

    def test_scenario_2_uses_safe_alternatives_fallback(self):
        body = processing._select_automatic_response_body(
            "nonviable",
            self.unsafe_llm_body,
            self.config,
            "Alex",
        )

        self.assertNotIn("flyer", body.lower())
        self.assertIn("other properties", body.lower())

    def test_scenario_4_uses_safe_completion_fallback(self):
        body = processing._select_automatic_response_body(
            "complete",
            self.unsafe_llm_body,
            self.config,
            "Alex",
        )

        self.assertEqual(
            "Hi Alex,\n\n"
            "Thanks for sending those details over. "
            "That gives me everything I need for now.",
            body,
        )

    def test_scenario_keeps_llm_copy_with_benign_link_context(self):
        safe_llm_body = (
            "Thanks for the details. Here is the link to the listing I reviewed."
        )

        body = processing._select_automatic_response_body(
            "complete",
            safe_llm_body,
            self.config,
            "Alex",
        )

        self.assertEqual(safe_llm_body, body)


class ProposalFailureVisibilityRegressionTests(unittest.TestCase):
    def test_proposal_none_branch_records_failure_and_raises_retryable(self):
        source = Path(processing.__file__).read_text(encoding="utf-8")

        self.assertIn('else:\n            print("ℹ️ No proposal generated; nothing to apply.")', source)
        self.assertIn("_record_ai_processing_failure(", source)
        self.assertIn(
            'raise RetryableProcessingError("OpenAI proposal was unavailable or invalid JSON")',
            source,
        )


if __name__ == "__main__":
    unittest.main()
