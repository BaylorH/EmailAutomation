import os
import unittest


os.environ.setdefault("E2E_TEST_MODE", "true")

from email_automation import processing


class PropertyUnavailableRowAliasTests(unittest.TestCase):
    EVENT = {
        "type": "property_unavailable",
        "reason": "no_longer_available",
        "address": "",
        "city": "",
    }
    ROW_ANCHOR = "10675 W Olive Ave, Houston"

    def _aliases(self, property_name="Olive Commerce Park"):
        return processing._server_owned_row_aliases(
            ["10675 W Olive Ave", "Houston", property_name],
            ["Property Address", "City", "Property Name"],
        )

    def _applies(self, message_text, *, property_name="Olive Commerce Park"):
        return processing._property_unavailable_event_applies_to_row(
            self.EVENT,
            row_anchor=self.ROW_ANCHOR,
            row_aliases=self._aliases(property_name),
            message_text=message_text,
        )

    def test_current_property_name_binds_exact_case_and_punctuation_variants(self):
        for message_text in (
            "Olive Commerce Park is no longer available.",
            "OLIVE COMMERCE PARK is no longer available.",
            "Olive-Commerce Park is no longer available.",
        ):
            with self.subTest(message_text=message_text):
                self.assertTrue(self._applies(message_text))

    def test_other_property_name_does_not_bind_current_row(self):
        for message_text in (
            "Oak Commerce Center is no longer available.",
            "North Olive Commerce Park is no longer available.",
            "Olive Commerce Park II is no longer available.",
            "Olive Commerce Park Building B is no longer available.",
            "Olive Commerce Park East is no longer available.",
            "Olive Commerce Park Annex is no longer available.",
            "Olive Commerce Park A is no longer available.",
            "Olive Commerce Park Expansion is no longer available.",
            "Olive Commerce Park Redevelopment is no longer available.",
            "Olive Commerce Park Tower is no longer available.",
            "Olive Commerce Park Campus is no longer available.",
            "Olive Commerce Park Warehouse 2 is no longer available.",
            "Olive Commerce Park Facility B is no longer available.",
            "Olive Commerce Park Lot 2 is no longer available.",
            "Olive Commerce Park Block B is no longer available.",
            "Olive Commerce Park Pod A is no longer available.",
            "Olive Commerce Park Wing A is no longer available.",
            "Olive Commerce Park Addition is no longer available.",
            "Unit A Olive Commerce Park is no longer available.",
            "Pod A Olive Commerce Park is no longer available.",
            "Wing A Olive Commerce Park is no longer available.",
        ):
            with self.subTest(message_text=message_text):
                self.assertFalse(self._applies(message_text))

    def test_full_server_owned_suffix_aliases_bind_only_their_exact_names(self):
        for property_name in (
            "Olive Commerce Park II",
            "Olive Commerce Park Building B",
            "Olive Commerce Park East",
            "Olive Commerce Park Annex",
            "Olive Commerce Park A",
            "Olive Commerce Park Expansion",
            "Olive Commerce Park Redevelopment",
            "Olive Commerce Park Tower",
            "Olive Commerce Park Campus",
            "Olive Commerce Park Warehouse 2",
            "Olive Commerce Park Facility B",
            "Olive Commerce Park Lot 2",
            "Olive Commerce Park Block B",
            "Olive Commerce Park Pod A",
            "Olive Commerce Park Wing A",
            "Olive Commerce Park Addition",
            "Unit A Olive Commerce Park",
            "Pod A Olive Commerce Park",
            "Wing A Olive Commerce Park",
        ):
            with self.subTest(property_name=property_name):
                self.assertTrue(self._applies(
                    f"{property_name} is no longer available.",
                    property_name=property_name,
                ))

    def test_target_alias_in_nonterminal_specs_clause_does_not_steal_competitor_terminal(self):
        self.assertFalse(self._applies(
            "Oak Commerce Center is no longer available, but specs for "
            "Olive Commerce Park are attached."
        ))

    def test_same_clause_target_and_competitor_bindings_fail_closed(self):
        for message_text in (
            "Olive Commerce Park is no longer available, but "
            "Oak Commerce Center remains available.",
            "Olive Commerce Park and Oak Commerce Center are no longer available.",
            "Both Olive Commerce Park and Oak Commerce Center are no longer available.",
            "Olive Commerce Park is no longer available and "
            "Oak Commerce Center is also leased.",
        ):
            with self.subTest(message_text=message_text):
                self.assertFalse(self._applies(message_text))

    def test_alias_and_terminal_must_share_one_fresh_clause(self):
        for message_text in (
            "Olive Commerce Park was reviewed. It is unavailable.",
            "Olive Commerce Park was reviewed; it is unavailable.",
            "Olive Commerce Park was reviewed\nit is unavailable.",
        ):
            with self.subTest(message_text=message_text):
                self.assertFalse(self._applies(message_text))

        self.assertTrue(self._applies(
            "Regarding Olive Commerce Park, it is no longer available."
        ))

    def test_unrecognized_same_clause_competitor_cues_fail_closed(self):
        for competitor in (
            "Another listing",
            "Building A",
            "Project Atlas",
            "Suite 200",
            "Unit A",
            "Parcel 7",
            "Site Alpha",
            "Listing 12",
            "Westgate Tower",
            "Camelback 303",
            "Warehouse 2",
            "Facility B",
        ):
            for message_text in (
                f"{competitor} is no longer available, but specs for "
                "Olive Commerce Park are attached.",
                "Specs for Olive Commerce Park are attached, but "
                f"{competitor} is no longer available.",
            ):
                with self.subTest(message_text=message_text):
                    self.assertFalse(self._applies(message_text))

    def test_target_and_competing_clauses_bind_only_target_terminal_evidence(self):
        self.assertTrue(self._applies(
            "Oak Commerce Center remains available; "
            "Olive Commerce Park is no longer available."
        ))
        self.assertFalse(self._applies(
            "Oak Commerce Center is no longer available; "
            "Olive Commerce Park remains available."
        ))
        self.assertTrue(self._applies(
            "Olive Commerce Park is no longer available; "
            "Oak Commerce Center has also been leased."
        ))

    def test_generic_property_name_fragment_does_not_bind(self):
        aliases = self._aliases()
        self.assertFalse(
            processing._source_mentions_server_owned_row_alias(
                "Commerce Park is no longer available.",
                aliases,
            )
        )
        self.assertFalse(self._applies("Commerce Park is no longer available."))

    def test_exact_alias_does_not_override_tour_or_ancillary_terminal_guards(self):
        for message_text in (
            "The Tuesday tour slot at Olive Commerce Park is no longer available.",
            "Olive Commerce Park's parking lot has been leased to another tenant.",
            "The trailer lot at Olive Commerce Park is no longer available.",
        ):
            with self.subTest(message_text=message_text):
                self.assertFalse(self._applies(message_text))

    def test_blank_or_malformed_property_name_aliases_fail_closed(self):
        malformed_values = (
            None,
            "",
            "   ",
            "-",
            "Olive",
            "Commerce Park",
            123,
            ["Olive Commerce Park"],
            {"name": "Olive Commerce Park"},
        )
        for value in malformed_values:
            with self.subTest(value=value):
                self.assertEqual([], self._aliases(value))

    def test_column_config_mapping_and_safe_header_fallbacks_are_server_owned(self):
        self.assertEqual(
            ["olive commerce park"],
            processing._server_owned_row_aliases(
                ["10675 W Olive Ave", "Houston", "Olive Commerce Park"],
                ["Property Address", "City", "Asset Label"],
                {"mappings": {"property_name": "Asset Label"}},
            ),
        )
        self.assertEqual(
            ["olive commerce park"],
            processing._server_owned_row_aliases(
                ["10675 W Olive Ave", "Houston", "Olive Commerce Park"],
                ["Property Address", "City", "Building Name"],
                None,
            ),
        )
        self.assertEqual(
            ["olive commerce park"],
            processing._server_owned_row_aliases(
                ["10675 W Olive Ave", "Houston", "Olive Commerce Park"],
                ["Property Address", "City", "Building Name"],
                {"mappings": {"property_name": "Building Name"}},
            ),
        )
        self.assertEqual(
            ["olive commerce park"],
            processing._server_owned_row_aliases(
                ["10675 W Olive Ave", "Houston", "Olive Commerce Park"],
                ["Property Address", "City", "Property Name"],
                {"mappings": {"address": "Property Address"}},
            ),
        )
        self.assertEqual(
            [],
            processing._server_owned_row_aliases(
                ["10675 W Olive Ave", "Houston", "Olive Commerce Park"],
                ["Property Address", "City", "Name"],
                None,
            ),
        )

    def test_duplicate_selected_property_name_headers_fail_closed(self):
        cases = (
            (
                ["Olive Commerce Park", "Oak Commerce Center"],
                ["Property Name", "Property Name"],
                None,
            ),
            (
                ["Olive Commerce Park", "Oak Commerce Center"],
                ["Asset Label", "Asset Label"],
                {"mappings": {"property_name": "Asset Label"}},
            ),
        )
        for rowvals, header, column_config in cases:
            with self.subTest(header=header, column_config=column_config):
                self.assertEqual(
                    [],
                    processing._server_owned_row_aliases(
                        rowvals,
                        header,
                        column_config,
                    ),
                )

    def test_explicit_event_address_path_is_unchanged_by_row_alias(self):
        aliases = self._aliases()
        matching_event = {
            **self.EVENT,
            "address": "10675 W Olive Ave",
            "city": "Houston",
        }
        wrong_event = {
            **self.EVENT,
            "address": "200 Oak Ave",
            "city": "Houston",
        }
        message_text = "Olive Commerce Park is no longer available."

        self.assertTrue(processing._property_unavailable_event_applies_to_row(
            matching_event,
            row_anchor=self.ROW_ANCHOR,
            row_aliases=aliases,
            message_text=message_text,
        ))
        self.assertFalse(processing._property_unavailable_event_applies_to_row(
            wrong_event,
            row_anchor=self.ROW_ANCHOR,
            row_aliases=aliases,
            message_text=message_text,
        ))

    def test_explicit_target_street_terminal_has_priority_over_benign_alias_clause(self):
        for message_text in (
            "Specs for Olive Commerce Park are attached; "
            "10675 W Olive Ave is no longer available.",
            "10675 W Olive Ave is no longer available; "
            "specs for Olive Commerce Park are attached.",
        ):
            with self.subTest(message_text=message_text):
                self.assertTrue(self._applies(message_text))

    def test_model_event_property_name_never_bypasses_fresh_alias_grounding(self):
        aliases = self._aliases()
        event = {
            **self.EVENT,
            "address": "OLIVE-COMMERCE PARK",
        }
        for message_text in (
            "Oak Commerce Center is no longer available, but specs for "
            "Olive Commerce Park are attached.",
            "Olive Commerce Park's parking lot has been leased.",
            "Thanks, I attached the current specs.",
        ):
            with self.subTest(message_text=message_text):
                self.assertFalse(processing._property_unavailable_event_applies_to_row(
                    event,
                    row_anchor=self.ROW_ANCHOR,
                    row_aliases=aliases,
                    message_text=message_text,
                ))

    def test_pending_nonviable_staging_uses_the_same_row_alias_binding(self):
        aliases = self._aliases()
        for message_text in (
            "Olive Commerce Park is no longer available.",
            "Olive Commerce Park has been leased.",
        ):
            with self.subTest(message_text=message_text):
                self.assertTrue(self._applies(message_text))
                patch = processing._pending_nonviable_followup_patch(
                    [self.EVENT],
                    row_anchor=self.ROW_ANCHOR,
                    row_aliases=aliases,
                    message_text=message_text,
                )
                self.assertEqual("stopped", patch["followUpStatus"])
                self.assertEqual("no_longer_available", patch["pendingTerminalReason"])

    def test_negated_or_superseded_alias_terminal_does_not_apply_or_stage(self):
        aliases = self._aliases()
        for message_text in (
            "Olive Commerce Park is not leased.",
            "Olive Commerce Park is not unavailable.",
            "Olive Commerce Park was no longer available, but is now still available.",
            "Olive Commerce Park is not leased; it remains available.",
            "Olive Commerce Park is not not leased.",
        ):
            with self.subTest(message_text=message_text):
                self.assertFalse(self._applies(message_text))
                self.assertIsNone(processing._pending_nonviable_followup_patch(
                    [self.EVENT],
                    row_anchor=self.ROW_ANCHOR,
                    row_aliases=aliases,
                    message_text=message_text,
                ))

    def test_alias_terminal_polarity_is_structural(self):
        for message_text in (
            "Olive Commerce Park is not available.",
            "Olive Commerce Park isn't available.",
            "Olive Commerce Park is no longer available.",
            "Olive Commerce Park is not a good fit.",
        ):
            with self.subTest(message_text=message_text):
                self.assertTrue(self._applies(message_text))

        for message_text in (
            "Olive Commerce Park is not leased.",
            "Olive Commerce Park isn't leased.",
            "Olive Commerce Park is not unavailable.",
            "Olive Commerce Park is no longer unavailable.",
            "Olive Commerce Park is not under contract.",
            "Olive Commerce Park is not off market.",
        ):
            with self.subTest(message_text=message_text):
                self.assertFalse(self._applies(message_text))


if __name__ == "__main__":
    unittest.main()
