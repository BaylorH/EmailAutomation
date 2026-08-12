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

from email_automation import ai_processing, processing


def _conversation(body):
    return [{"direction": "inbound", "content": body}]


class JillLiveCampaignRegressionTests(unittest.TestCase):
    def test_rent_only_nnn_shorthand_is_not_opex(self):
        examples = (
            "For Space Center, we can offer 18,750 SF at $14.10 NNN.",
            "The suite is available at $14.10 NNN.",
            "18,750 SF @ $14.10 NNN.",
            "The suite is available @ $14.10 NNN.",
            "18,750 SF — $14.10 NNN.",
            "The suite is available – $14.10 NNN.",
            "The suite is offered at $14.10 NNN.",
            "We are offering the suite at $14.10 NNN.",
            "Availability at $14.10 NNN.",
            "Availability: $14.10 NNN.",
            "We can offer the suite for $14.10 NNN.",
            "The suite is available for $14.10 NNN.",
            "18,750 SF for $14.10 NNN.",
            "18750 SF at $14.10 NNN.",
            "18,750 sq. ft. at $14.10 NNN.",
            "The suite is available at approximately $14.10 NNN.",
            "The suite is available for about $14.10 NNN.",
            "18,750 SF at roughly $14.10 NNN.",
            "We are offering the suite at around $14.10 NNN.",
            "The suite is available at approx. $14.10 NNN.",
        )

        self.assertEqual(
            [("14.10", None)] * len(examples),
            [
                (
                    ai_processing._extract_rent_sf_yr_from_text(text),
                    ai_processing._extract_ops_ex_sf_from_text(text),
                )
                for text in examples
            ],
        )

    def test_rent_only_nnn_shorthand_never_survives_as_proposal_opex(self):
        examples = (
            "For Space Center, we can offer 18,750 SF at $14.10 NNN.",
            "The suite is available at $14.10 NNN.",
        )
        header = ["Property Address", "Rent/SF/Yr", "Ops Ex / SF"]
        config = {"mappings": {"rent_sf_yr": "Rent/SF/Yr", "ops_ex_sf": "Ops Ex / SF"}}

        results = []
        for text in examples:
            for updates in (
                [],
                [{"column": "Ops Ex / SF", "value": "14.10"}],
            ):
                proposal = {"updates": [dict(update) for update in updates], "events": []}
                result = ai_processing._augment_proposal_with_deterministic_extractions(
                    proposal,
                    ["4800 Space Center Blvd", "", ""],
                    header,
                    config,
                    _conversation(text),
                )
                results.append(
                    ai_processing._proposal_update_for_column(result, "Ops Ex / SF")
                )

        self.assertEqual([None, None, None, None], results)

    def test_later_availability_context_outranks_pending_cam(self):
        examples = (
            "CAM: TBD | Availability: $14.10 NNN.",
            "CAM is pending: the suite is available at $14.10 NNN.",
            "CAM is pending — the suite is available at $14.10 NNN.",
            "CAM: pending - availability at $14.10 NNN.",
            "CAM is pending; Availability: $14.10 NNN.",
            "CAM is pending. Availability: $14.10 NNN.",
            "CAM is pending\nAvailability: $14.10 NNN.",
            "Availability: $14.10 NNN.",
        )
        header = ["Property Address", "Rent/SF/Yr", "Ops Ex / SF"]
        config = {"mappings": {"rent_sf_yr": "Rent/SF/Yr", "ops_ex_sf": "Ops Ex / SF"}}
        results = []

        for text in examples:
            augmented = ai_processing._augment_proposal_with_deterministic_extractions(
                {
                    "updates": [
                        {"column": "Rent/SF/Yr", "value": "14.10"},
                        {"column": "Ops Ex / SF", "value": "14.10"},
                    ]
                },
                ["4800 Space Center Blvd", "", ""],
                header,
                config,
                _conversation(text),
            )
            rent_update = ai_processing._proposal_update_for_column(
                augmented,
                "Rent/SF/Yr",
            )
            opex_update = ai_processing._proposal_update_for_column(
                augmented,
                "Ops Ex / SF",
            )
            results.append((
                ai_processing._extract_rent_sf_yr_from_text(text),
                ai_processing._extract_ops_ex_sf_from_text(text),
                rent_update["value"] if rent_update is not None else None,
                opex_update["value"] if opex_update is not None else None,
            ))

        self.assertEqual(
            [("14.10", None, "14.10", None)] * len(examples),
            results,
        )

    def test_pending_cam_structural_separators_allow_later_availability(self):
        examples = (
            "CAM is pending | Availability at $14.10 NNN.",
            "CAM is pending: Availability at $14.10 NNN.",
            "CAM is pending - Availability at $14.10 NNN.",
            "CAM is pending – Availability at $14.10 NNN.",
            "CAM is pending — Availability at $14.10 NNN.",
        )
        header = ["Property Address", "Rent/SF/Yr", "Ops Ex / SF"]
        config = {"mappings": {"rent_sf_yr": "Rent/SF/Yr", "ops_ex_sf": "Ops Ex / SF"}}
        results = []

        for text in examples:
            augmented = ai_processing._augment_proposal_with_deterministic_extractions(
                {
                    "updates": [
                        {"column": "Rent/SF/Yr", "value": "14.10"},
                        {"column": "Ops Ex / SF", "value": "14.10"},
                    ]
                },
                ["4800 Space Center Blvd", "", ""],
                header,
                config,
                _conversation(text),
            )
            rent_update = ai_processing._proposal_update_for_column(
                augmented,
                "Rent/SF/Yr",
            )
            opex_update = ai_processing._proposal_update_for_column(
                augmented,
                "Ops Ex / SF",
            )
            results.append((
                ai_processing._extract_rent_sf_yr_from_text(text),
                ai_processing._extract_ops_ex_sf_from_text(text),
                rent_update["value"] if rent_update is not None else None,
                opex_update["value"] if opex_update is not None else None,
            ))

        self.assertEqual(
            [("14.10", None, "14.10", None)] * len(examples),
            results,
        )

    def test_direct_cam_colon_nnn_remains_expense_owned(self):
        text = "CAM: $3.65 NNN."
        header = ["Property Address", "Rent/SF/Yr", "Ops Ex / SF"]
        config = {"mappings": {"rent_sf_yr": "Rent/SF/Yr", "ops_ex_sf": "Ops Ex / SF"}}
        augmented = ai_processing._augment_proposal_with_deterministic_extractions(
            {
                "updates": [
                    {"column": "Rent/SF/Yr", "value": "3.65"},
                    {"column": "Ops Ex / SF", "value": "3.65"},
                ]
            },
            ["4800 Space Center Blvd", "", ""],
            header,
            config,
            _conversation(text),
        )

        self.assertEqual(
            (None, "3.65", None, "3.65"),
            (
                ai_processing._extract_rent_sf_yr_from_text(text),
                ai_processing._extract_ops_ex_sf_from_text(text),
                ai_processing._proposal_update_for_column(
                    augmented,
                    "Rent/SF/Yr",
                ),
                ai_processing._proposal_update_for_column(
                    augmented,
                    "Ops Ex / SF",
                )["value"],
            ),
        )

    def test_figure_first_explicit_opex_formats_remain_supported(self):
        examples = (
            "Separate operating expenses are $3.65 NNN.",
            "Separate operating expenses are $3.65 CAM.",
            "Separate operating expenses are $3.65 opex.",
            "Separate operating expenses are $3.65 TMI.",
        )
        self.assertEqual(
            ["3.65", "3.65", "3.65", "3.65"],
            [ai_processing._extract_ops_ex_sf_from_text(text) for text in examples],
        )

    def test_explicit_expense_owner_overrides_contextual_area_syntax(self):
        examples = (
            "Operating expenses for 18,750 SF at $3.65 NNN.",
            "CAM for 18,750 SF @ $3.65 NNN.",
            "OpEx for 18750 sq.ft. — $3.65 NNN.",
        )
        header = ["Property Address", "Rent/SF/Yr", "Ops Ex / SF"]
        config = {"mappings": {"rent_sf_yr": "Rent/SF/Yr", "ops_ex_sf": "Ops Ex / SF"}}
        results = []

        for text in examples:
            proposal = {"updates": [{"column": "Ops Ex / SF", "value": "3.65"}]}
            augmented = ai_processing._augment_proposal_with_deterministic_extractions(
                proposal,
                ["4800 Space Center Blvd", "", ""],
                header,
                config,
                _conversation(text),
            )
            opex_update = ai_processing._proposal_update_for_column(
                augmented,
                "Ops Ex / SF",
            )
            results.append((
                ai_processing._extract_rent_sf_yr_from_text(text),
                ai_processing._extract_ops_ex_sf_from_text(text),
                ai_processing._proposal_update_for_column(
                    augmented,
                    "Rent/SF/Yr",
                ),
                opex_update["value"] if opex_update is not None else None,
            ))

        self.assertEqual(
            [(None, "3.65", None, "3.65")] * len(examples),
            results,
        )

    def test_expense_owned_per_sf_nnn_never_duplicates_into_rent(self):
        examples = (
            "Expenses are $3.65/SF NNN.",
            "Pass-throughs are $3.65/SF NNN.",
            "TMI is $3.65/SF NNN.",
            "Separate NNN charges are $3.65 per square foot.",
        )
        header = ["Property Address", "Rent/SF/Yr", "Ops Ex / SF"]
        config = {"mappings": {"rent_sf_yr": "Rent/SF/Yr", "ops_ex_sf": "Ops Ex / SF"}}
        results = []

        for text in examples:
            augmented = ai_processing._augment_proposal_with_deterministic_extractions(
                {"updates": []},
                ["4800 Space Center Blvd", "", ""],
                header,
                config,
                _conversation(text),
            )
            rent_update = ai_processing._proposal_update_for_column(
                augmented,
                "Rent/SF/Yr",
            )
            opex_update = ai_processing._proposal_update_for_column(
                augmented,
                "Ops Ex / SF",
            )
            results.append((
                ai_processing._extract_rent_sf_yr_from_text(text),
                ai_processing._extract_ops_ex_sf_from_text(text),
                rent_update["value"] if rent_update is not None else None,
                opex_update["value"] if opex_update is not None else None,
            ))

        self.assertEqual(
            [(None, "3.65", None, "3.65")] * len(examples),
            results,
        )

    def test_long_form_per_sf_nnn_is_admitted_for_expense_owners(self):
        examples = (
            "Expenses are $3.65 per SF NNN.",
            "Expenses are $3.65 per square foot NNN.",
            "Pass-throughs are $3.65 per SF NNN.",
            "Operating costs are $3.65 per square foot NNN.",
            "Expenses are $3.65/SF NNN.",
            "Operating expenses are $3.65 per SF NNN.",
            "CAM is $3.65 per square foot NNN.",
        )
        header = ["Property Address", "Rent/SF/Yr", "Ops Ex / SF"]
        config = {"mappings": {"rent_sf_yr": "Rent/SF/Yr", "ops_ex_sf": "Ops Ex / SF"}}
        results = []

        for text in examples:
            augmented = ai_processing._augment_proposal_with_deterministic_extractions(
                {
                    "updates": [
                        {"column": "Rent/SF/Yr", "value": "3.65"},
                        {"column": "Ops Ex / SF", "value": "3.65"},
                    ]
                },
                ["4800 Space Center Blvd", "", ""],
                header,
                config,
                _conversation(text),
            )
            rent_update = ai_processing._proposal_update_for_column(
                augmented,
                "Rent/SF/Yr",
            )
            opex_update = ai_processing._proposal_update_for_column(
                augmented,
                "Ops Ex / SF",
            )
            results.append((
                ai_processing._extract_rent_sf_yr_from_text(text),
                ai_processing._extract_ops_ex_sf_from_text(text),
                rent_update["value"] if rent_update is not None else None,
                opex_update["value"] if opex_update is not None else None,
            ))

        self.assertEqual(
            [(None, "3.65", None, "3.65")] * len(examples),
            results,
        )

    def test_long_form_expense_nnn_rates_keep_explicit_basis(self):
        examples = (
            ("Expenses are $0.34 per SF/month NNN.", "4.08", "0.34"),
            ("Expenses are $0.34 per SF per month NNN.", "4.08", "0.34"),
            ("Expenses are $3.65 per SF/year NNN.", "3.65", "3.65"),
        )
        header = ["Property Address", "Rent/SF/Yr", "Ops Ex / SF"]
        config = {"mappings": {"rent_sf_yr": "Rent/SF/Yr", "ops_ex_sf": "Ops Ex / SF"}}
        results = []

        for text, expected_opex, preseeded_opex in examples:
            augmented = ai_processing._augment_proposal_with_deterministic_extractions(
                {
                    "updates": [
                        {"column": "Rent/SF/Yr", "value": expected_opex},
                        {"column": "Ops Ex / SF", "value": preseeded_opex},
                    ],
                    "events": [],
                },
                ["4800 Space Center Blvd", "", ""],
                header,
                config,
                _conversation(text),
            )
            normalized = ai_processing._augment_proposal_opex_basis(
                augmented,
                ["4800 Space Center Blvd", "", ""],
                header,
                config,
                _conversation(text),
            )
            rent_update = ai_processing._proposal_update_for_column(
                normalized,
                "Rent/SF/Yr",
            )
            opex_update = ai_processing._proposal_update_for_column(
                normalized,
                "Ops Ex / SF",
            )
            results.append((
                ai_processing._extract_rent_sf_yr_from_text(text),
                ai_processing._extract_ops_ex_sf_from_text(text),
                rent_update["value"] if rent_update is not None else None,
                opex_update["value"] if opex_update is not None else None,
            ))

        self.assertEqual(
            [
                (None, expected_opex, None, expected_opex)
                for _, expected_opex, _ in examples
            ],
            results,
        )

    def test_long_form_expense_nnn_before_billed_basis_remains_supported(self):
        text = "Expenses are $0.34 per SF NNN, billed monthly."
        self.assertEqual(
            (None, "4.08"),
            (
                ai_processing._extract_rent_sf_yr_from_text(text),
                ai_processing._extract_ops_ex_sf_from_text(text),
            ),
        )

    def test_expense_rate_compounds_are_owned_only_by_opex(self):
        examples = (
            "Operating expense rate is $3.65 NNN.",
            "CAM rate is $3.65 NNN.",
            "OpEx rate is $3.65 NNN.",
            "TMI rate is $3.65 NNN.",
            "Operating expense rate is $3.65/SF.",
            "CAM rate is $3.65/SF.",
            "OpEx rate is $3.65/SF.",
            "TMI rate is $3.65/SF.",
        )
        header = ["Property Address", "Rent/SF/Yr", "Ops Ex / SF"]
        config = {"mappings": {"rent_sf_yr": "Rent/SF/Yr", "ops_ex_sf": "Ops Ex / SF"}}
        results = []

        for text in examples:
            augmented = ai_processing._augment_proposal_with_deterministic_extractions(
                {
                    "updates": [
                        {"column": "Rent/SF/Yr", "value": "3.65"},
                        {"column": "Ops Ex / SF", "value": "3.65"},
                    ]
                },
                ["4800 Space Center Blvd", "", ""],
                header,
                config,
                _conversation(text),
            )
            rent_update = ai_processing._proposal_update_for_column(
                augmented,
                "Rent/SF/Yr",
            )
            opex_update = ai_processing._proposal_update_for_column(
                augmented,
                "Ops Ex / SF",
            )
            results.append((
                ai_processing._extract_rent_sf_yr_from_text(text),
                ai_processing._extract_ops_ex_sf_from_text(text),
                rent_update["value"] if rent_update is not None else None,
                opex_update["value"] if opex_update is not None else None,
            ))

        self.assertEqual(
            [(None, "3.65", None, "3.65")] * len(examples),
            results,
        )

    def test_explicit_rent_rate_controls_remain_rent_owned(self):
        examples = (
            "Rate is $14.10 NNN.",
            "Asking rate is $14.10 NNN.",
            "Lease rate is $14.10 NNN.",
            "Rental rate is $14.10 NNN.",
            "Rate is $14.10/SF.",
            "Asking rate is $14.10/SF.",
            "Lease rate is $14.10/SF.",
            "Rental rate is $14.10/SF.",
        )
        header = ["Property Address", "Rent/SF/Yr", "Ops Ex / SF"]
        config = {"mappings": {"rent_sf_yr": "Rent/SF/Yr", "ops_ex_sf": "Ops Ex / SF"}}
        results = []

        for text in examples:
            augmented = ai_processing._augment_proposal_with_deterministic_extractions(
                {
                    "updates": [
                        {"column": "Rent/SF/Yr", "value": "14.10"},
                        {"column": "Ops Ex / SF", "value": "14.10"},
                    ]
                },
                ["4800 Space Center Blvd", "", ""],
                header,
                config,
                _conversation(text),
            )
            rent_update = ai_processing._proposal_update_for_column(
                augmented,
                "Rent/SF/Yr",
            )
            opex_update = ai_processing._proposal_update_for_column(
                augmented,
                "Ops Ex / SF",
            )
            results.append((
                ai_processing._extract_rent_sf_yr_from_text(text),
                ai_processing._extract_ops_ex_sf_from_text(text),
                rent_update["value"] if rent_update is not None else None,
                opex_update["value"] if opex_update is not None else None,
            ))

        self.assertEqual(
            [("14.10", None, "14.10", None)] * len(examples),
            results,
        )

    def test_rent_keyword_boundaries_reject_unrelated_word_substrings(self):
        explicit_opex_examples = (
            "The corporate NNN charge is $3.65 per square foot.",
            "The accurate NNN figure is $3.65 per square foot.",
        )
        abstaining_example = "This operates at $3.65 per square foot for CAM."
        examples = explicit_opex_examples + (abstaining_example,)
        header = ["Property Address", "Rent/SF/Yr", "Ops Ex / SF"]
        config = {"mappings": {"rent_sf_yr": "Rent/SF/Yr", "ops_ex_sf": "Ops Ex / SF"}}
        results = []

        for text in examples:
            augmented = ai_processing._augment_proposal_with_deterministic_extractions(
                {"updates": []},
                ["4800 Space Center Blvd", "", ""],
                header,
                config,
                _conversation(text),
            )
            results.append((
                ai_processing._extract_rent_sf_yr_from_text(text),
                ai_processing._proposal_update_for_column(
                    augmented,
                    "Rent/SF/Yr",
                ),
            ))

        self.assertEqual([(None, None)] * len(examples), results)
        self.assertEqual(
            ["3.65"] * len(explicit_opex_examples),
            [
                ai_processing._extract_ops_ex_sf_from_text(text)
                for text in explicit_opex_examples
            ],
        )

    def test_bare_figure_first_nnn_is_neutral_without_field_ownership(self):
        text = "$3.65 NNN."
        self.assertIsNone(ai_processing._extract_rent_sf_yr_from_text(text))
        self.assertIsNone(ai_processing._extract_ops_ex_sf_from_text(text))

    def test_same_field_nnn_expense_suffix_owns_figure_as_opex(self):
        examples = (
            "$3.65 NNN/CAM.",
            "$3.65 NNN/OpEx.",
            "$3.65 NNN/TMI.",
        )
        self.assertEqual(
            [(None, "3.65")] * len(examples),
            [
                (
                    ai_processing._extract_rent_sf_yr_from_text(text),
                    ai_processing._extract_ops_ex_sf_from_text(text),
                )
                for text in examples
            ],
        )

    def test_conflicting_rent_and_expense_ownership_abstains(self):
        text = "Asking $14.10 NNN/CAM."
        self.assertIsNone(ai_processing._extract_rent_sf_yr_from_text(text))
        self.assertIsNone(ai_processing._extract_ops_ex_sf_from_text(text))

    def test_relational_expense_modifiers_preserve_governing_nnn_subject(self):
        examples = (
            ("Rent before expenses is $14.10 NNN.", "14.10", None),
            (
                "Base rent, excluding operating expenses, is $14.10 NNN.",
                "14.10",
                None,
            ),
            (
                "Asking rent does not include CAM and is $14.10 NNN.",
                "14.10",
                None,
            ),
            ("Rent net of expenses is $14.10 NNN.", "14.10", None),
            ("Rent net-of expenses is $14.10 NNN.", "14.10", None),
            ("Rent excluding pass-throughs is $14.10 NNN.", "14.10", None),
            ("Expenses are separate; rent is $14.10 NNN.", "14.10", None),
            (
                "Operating expenses excluding rent are $3.65 NNN.",
                None,
                "3.65",
            ),
            ("Operating expenses net-of rent are $3.65 NNN.", None, "3.65"),
            ("CAM does not include rent and is $3.65 NNN.", None, "3.65"),
        )
        header = ["Property Address", "Rent/SF/Yr", "Ops Ex / SF"]
        config = {"mappings": {"rent_sf_yr": "Rent/SF/Yr", "ops_ex_sf": "Ops Ex / SF"}}
        results = []

        for text, expected_rent, expected_opex in examples:
            augmented = ai_processing._augment_proposal_with_deterministic_extractions(
                {"updates": []},
                ["4800 Space Center Blvd", "", ""],
                header,
                config,
                _conversation(text),
            )
            rent_update = ai_processing._proposal_update_for_column(
                augmented,
                "Rent/SF/Yr",
            )
            opex_update = ai_processing._proposal_update_for_column(
                augmented,
                "Ops Ex / SF",
            )
            results.append((
                ai_processing._extract_rent_sf_yr_from_text(text),
                ai_processing._extract_ops_ex_sf_from_text(text),
                rent_update["value"] if rent_update is not None else None,
                opex_update["value"] if opex_update is not None else None,
            ))

        self.assertEqual(
            [
                (expected_rent, expected_opex, expected_rent, expected_opex)
                for _, expected_rent, expected_opex in examples
            ],
            results,
        )

    def test_opex_rent_modifiers_do_not_duplicate_into_rent(self):
        examples = (
            "CAM, on top of base rent, is $3.65 NNN.",
            "CAM, on top of base rent, is $3.65 per square foot.",
            "CAM (in addition to base rent) is $3.65 NNN.",
        )
        header = ["Property Address", "Rent/SF/Yr", "Ops Ex / SF"]
        config = {"mappings": {"rent_sf_yr": "Rent/SF/Yr", "ops_ex_sf": "Ops Ex / SF"}}
        results = []

        for text in examples:
            augmented = ai_processing._augment_proposal_with_deterministic_extractions(
                {
                    "updates": [
                        {"column": "Rent/SF/Yr", "value": "3.65"},
                        {"column": "Ops Ex / SF", "value": "3.65"},
                    ]
                },
                ["4800 Space Center Blvd", "", ""],
                header,
                config,
                _conversation(text),
            )
            rent_update = ai_processing._proposal_update_for_column(
                augmented,
                "Rent/SF/Yr",
            )
            opex_update = ai_processing._proposal_update_for_column(
                augmented,
                "Ops Ex / SF",
            )
            results.append((
                ai_processing._extract_rent_sf_yr_from_text(text),
                ai_processing._extract_ops_ex_sf_from_text(text),
                rent_update["value"] if rent_update is not None else None,
                opex_update["value"] if opex_update is not None else None,
            ))

        self.assertEqual(
            [(None, "3.65", None, "3.65")] * len(examples),
            results,
        )

    def test_coordinated_nnn_clause_uses_figure_governing_subject(self):
        examples = (
            ("Rent is separate and CAM is $3.65 NNN.", None, "3.65"),
            ("CAM is separate and rent is $14.10 NNN.", "14.10", None),
        )
        header = ["Property Address", "Rent/SF/Yr", "Ops Ex / SF"]
        config = {"mappings": {"rent_sf_yr": "Rent/SF/Yr", "ops_ex_sf": "Ops Ex / SF"}}
        results = []

        for text, _, _ in examples:
            augmented = ai_processing._augment_proposal_with_deterministic_extractions(
                {"updates": []},
                ["4800 Space Center Blvd", "", ""],
                header,
                config,
                _conversation(text),
            )
            rent_update = ai_processing._proposal_update_for_column(
                augmented,
                "Rent/SF/Yr",
            )
            opex_update = ai_processing._proposal_update_for_column(
                augmented,
                "Ops Ex / SF",
            )
            results.append((
                ai_processing._extract_rent_sf_yr_from_text(text),
                ai_processing._extract_ops_ex_sf_from_text(text),
                rent_update["value"] if rent_update is not None else None,
                opex_update["value"] if opex_update is not None else None,
            ))

        self.assertEqual(
            [
                (expected_rent, expected_opex, expected_rent, expected_opex)
                for _, expected_rent, expected_opex in examples
            ],
            results,
        )

    def test_relational_rent_owner_applies_to_explicit_per_sf_rates(self):
        examples = (
            "Base rent, excluding operating expenses, is $14.10/SF.",
            "Asking rent does not include CAM and is $14.10/SF.",
            "Base rent, excluding operating expenses, is $14.10/SF NNN.",
            "Asking rent does not include CAM and is $14.10/SF NNN.",
        )
        header = ["Property Address", "Rent/SF/Yr", "Ops Ex / SF"]
        config = {"mappings": {"rent_sf_yr": "Rent/SF/Yr", "ops_ex_sf": "Ops Ex / SF"}}
        results = []

        for text in examples:
            augmented = ai_processing._augment_proposal_with_deterministic_extractions(
                {"updates": []},
                ["4800 Space Center Blvd", "", ""],
                header,
                config,
                _conversation(text),
            )
            rent_update = ai_processing._proposal_update_for_column(
                augmented,
                "Rent/SF/Yr",
            )
            opex_update = ai_processing._proposal_update_for_column(
                augmented,
                "Ops Ex / SF",
            )
            results.append((
                ai_processing._extract_rent_sf_yr_from_text(text),
                ai_processing._extract_ops_ex_sf_from_text(text),
                rent_update["value"] if rent_update is not None else None,
                opex_update["value"] if opex_update is not None else None,
            ))

        self.assertEqual(
            [("14.10", None, "14.10", None)] * len(examples),
            results,
        )

    def test_governing_owner_removes_preseeded_wrong_field_update(self):
        examples = (
            ("Expenses are $3.65/SF NNN.", "3.65", None, "3.65"),
            (
                "Rent is separate and CAM is $3.65 NNN.",
                "3.65",
                None,
                "3.65",
            ),
            (
                "Base rent, excluding operating expenses, is $14.10/SF.",
                "14.10",
                "14.10",
                None,
            ),
            (
                "CAM is separate and rent is $14.10 NNN.",
                "14.10",
                "14.10",
                None,
            ),
        )
        header = ["Property Address", "Rent/SF/Yr", "Ops Ex / SF"]
        config = {"mappings": {"rent_sf_yr": "Rent/SF/Yr", "ops_ex_sf": "Ops Ex / SF"}}
        results = []

        for text, value, _, _ in examples:
            augmented = ai_processing._augment_proposal_with_deterministic_extractions(
                {
                    "updates": [
                        {"column": "Rent/SF/Yr", "value": value},
                        {"column": "Ops Ex / SF", "value": value},
                    ]
                },
                ["4800 Space Center Blvd", "", ""],
                header,
                config,
                _conversation(text),
            )
            rent_update = ai_processing._proposal_update_for_column(
                augmented,
                "Rent/SF/Yr",
            )
            opex_update = ai_processing._proposal_update_for_column(
                augmented,
                "Ops Ex / SF",
            )
            results.append((
                rent_update["value"] if rent_update is not None else None,
                opex_update["value"] if opex_update is not None else None,
            ))

        self.assertEqual(
            [
                (expected_rent, expected_opex)
                for _, _, expected_rent, expected_opex in examples
            ],
            results,
        )

    def test_monthly_rent_rejects_raw_and_annualized_preseeded_opex(self):
        text = "Rent is $1.20/SF/month."
        header = ["Property Address", "Rent/SF/Yr", "Ops Ex / SF"]
        config = {"mappings": {"rent_sf_yr": "Rent/SF/Yr", "ops_ex_sf": "Ops Ex / SF"}}
        results = []

        for preseeded_opex in ("1.20", "14.40"):
            augmented = ai_processing._augment_proposal_with_deterministic_extractions(
                {
                    "updates": [
                        {"column": "Ops Ex / SF", "value": preseeded_opex},
                    ]
                },
                ["4800 Space Center Blvd", "", ""],
                header,
                config,
                _conversation(text),
            )
            rent_update = ai_processing._proposal_update_for_column(
                augmented,
                "Rent/SF/Yr",
            )
            results.append((
                rent_update["value"] if rent_update is not None else None,
                ai_processing._proposal_update_for_column(
                    augmented,
                    "Ops Ex / SF",
                ),
            ))

        self.assertEqual([("14.40", None), ("14.40", None)], results)

    def test_explicit_opex_wins_over_earlier_nnn_rent_basis(self):
        examples = {
            (
                "We are marketing the Units at $14.00 psf NNN, "
                "OPEX approximately $4.00 psf."
            ): "4.00",
            (
                "The lease price is $15.50 psf nnn and estimated "
                "Taxes & CAM are $3.00 psf."
            ): "3.00",
            (
                "The NNN lease rate is $14.00 per SF; OPEX is $4.00 per SF."
            ): "4.00",
            (
                "The asking rental rate for the space is $14.00/SF NNN, and "
                "$4.00/SF in operating expenses."
            ): "4.00",
        }

        for text, expected in examples.items():
            with self.subTest(text=text):
                self.assertEqual(
                    expected,
                    ai_processing._extract_ops_ex_sf_from_text(text),
                )

    def test_explicit_cam_figure_wins_over_earlier_nnn_rent_basis(self):
        text = (
            "For Space Center, we can offer 18,750 SF at $14.10 NNN. "
            "CAM, taxes, and insurance are running roughly $3.90 per square foot. "
            "The suite has one drive-in and two dock-high doors, 26 feet clear, "
            "277/480V three-phase 600-amp service, and was completed in 2008."
        )
        self.assertEqual("14.10", ai_processing._extract_rent_sf_yr_from_text(text))
        self.assertEqual("3.90", ai_processing._extract_ops_ex_sf_from_text(text))

    def test_explicit_cam_replaces_conflicting_model_opex_before_sheet_write(self):
        text = (
            "For Space Center, we can offer 18,750 SF at $14.10 NNN. "
            "CAM, taxes, and insurance are running roughly $3.90 per square foot."
        )
        proposal = {
            "updates": [
                {"column": "Rent/SF/Yr", "value": "14.10"},
                {"column": "Ops Ex / SF", "value": "14.10"},
            ],
            "events": [],
        }
        header = ["Property Address", "Rent/SF/Yr", "Ops Ex / SF"]
        config = {"mappings": {"rent_sf_yr": "Rent/SF/Yr", "ops_ex_sf": "Ops Ex / SF"}}
        result = ai_processing._augment_proposal_with_deterministic_extractions(
            proposal, ["4800 Space Center Blvd", "", ""], header, config, _conversation(text)
        )
        self.assertEqual(
            "3.90",
            ai_processing._proposal_update_for_column(result, "Ops Ex / SF")["value"],
        )

    def test_pending_cam_clause_does_not_capture_later_asking_rent(self):
        text = "CAM is still pending; the asking rent is $14.10 per square foot NNN."
        self.assertIsNone(ai_processing._extract_ops_ex_sf_from_text(text))

    def test_pending_cam_does_not_cross_semicolon_into_quoted_rent(self):
        text = "CAM is still pending; the quoted rate is $14.10 per square foot NNN."
        proposal = {"updates": [], "events": []}
        header = ["Property Address", "Rent/SF/Yr", "Ops Ex / SF"]
        config = {"mappings": {"rent_sf_yr": "Rent/SF/Yr", "ops_ex_sf": "Ops Ex / SF"}}
        self.assertIsNone(ai_processing._extract_ops_ex_sf_from_text(text))
        result = ai_processing._augment_proposal_with_deterministic_extractions(
            proposal, ["4800 Space Center Blvd", "", ""], header, config, _conversation(text)
        )
        self.assertIsNone(ai_processing._proposal_update_for_column(result, "Ops Ex / SF"))

    def test_relational_base_rent_phrase_keeps_explicit_cam_figure(self):
        text = (
            "For Space Center, we can offer 18,750 SF at $14.10 NNN. "
            "CAM, on top of the base rent, is $3.90 per square foot."
        )
        proposal = {"updates": [{"column": "Ops Ex / SF", "value": "14.10"}], "events": []}
        header = ["Property Address", "Rent/SF/Yr", "Ops Ex / SF"]
        config = {"mappings": {"rent_sf_yr": "Rent/SF/Yr", "ops_ex_sf": "Ops Ex / SF"}}
        self.assertEqual("3.90", ai_processing._extract_ops_ex_sf_from_text(text))
        result = ai_processing._augment_proposal_with_deterministic_extractions(
            proposal, ["4800 Space Center Blvd", "", ""], header, config, _conversation(text)
        )
        self.assertEqual("3.90", ai_processing._proposal_update_for_column(result, "Ops Ex / SF")["value"])

    def test_cam_plus_base_rent_total_is_not_opex(self):
        text = "CAM plus base rent equals $18.00 per square foot."
        self.assertIsNone(ai_processing._extract_ops_ex_sf_from_text(text))

    def test_combined_total_phrasings_are_not_opex(self):
        examples = (
            "CAM plus base rent equals $18.00 per square foot.",
            "CAM on top of base rent totals $18.00 per square foot.",
            "CAM in addition to base rent equals $18.00 per square foot.",
            "CAM and base rent total $18.00 per square foot.",
            "Base rent plus CAM equals $18.00 per square foot.",
        )
        for text in examples:
            with self.subTest(text=text):
                self.assertIsNone(ai_processing._extract_ops_ex_sf_from_text(text))

    def test_monthly_combined_total_is_not_opex(self):
        text = "CAM plus rent is $1.50/SF/month."
        self.assertIsNone(ai_processing._extract_ops_ex_sf_from_text(text))

    def test_rejected_combined_total_removes_model_opex_before_event_return(self):
        text = "CAM plus base rent equals $18.00 per square foot."
        proposal = {
            "updates": [{"column": "Ops Ex / SF", "value": "18.00"}],
            "events": [{"type": "property_unavailable", "reason": "leased"}],
        }
        header = ["Property Address", "Rent/SF/Yr", "Ops Ex / SF"]
        config = {"mappings": {"rent_sf_yr": "Rent/SF/Yr", "ops_ex_sf": "Ops Ex / SF"}}
        result = ai_processing._augment_proposal_with_deterministic_extractions(
            proposal, ["4800 Space Center Blvd", "", ""], header, config, _conversation(text)
        )
        self.assertIsNone(ai_processing._proposal_update_for_column(result, "Ops Ex / SF"))

    def test_monthly_combined_total_removes_raw_and_annualized_model_opex(self):
        text = "CAM plus rent is $1.50/SF/month."
        header = ["Property Address", "Rent/SF/Yr", "Ops Ex / SF"]
        config = {"mappings": {"rent_sf_yr": "Rent/SF/Yr", "ops_ex_sf": "Ops Ex / SF"}}

        for proposed_value in ("1.50", "18.00"):
            with self.subTest(proposed_value=proposed_value):
                proposal = {
                    "updates": [{"column": "Ops Ex / SF", "value": proposed_value}],
                    "events": [{"type": "property_unavailable", "reason": "leased"}],
                }
                result = ai_processing._augment_proposal_with_deterministic_extractions(
                    proposal,
                    ["4800 Space Center Blvd", "", ""],
                    header,
                    config,
                    _conversation(text),
                )
                self.assertIsNone(
                    ai_processing._proposal_update_for_column(result, "Ops Ex / SF")
                )

    def test_reversed_conflicting_basis_combined_total_removes_monthly_values(self):
        examples = (
            "CAM plus rent is $1.50/SF/year/month.",
            "CAM plus rent is $1.50/SF per year/month.",
            "CAM plus rent is $1.50/SF/year per month.",
            "CAM plus rent is $1.50/SF/year, billed monthly.",
        )
        header = ["Property Address", "Rent/SF/Yr", "Ops Ex / SF"]
        config = {"mappings": {"rent_sf_yr": "Rent/SF/Yr", "ops_ex_sf": "Ops Ex / SF"}}

        for text in examples:
            for proposed_value in ("1.50", "18.00"):
                with self.subTest(text=text, proposed_value=proposed_value):
                    proposal = {
                        "updates": [{"column": "Ops Ex / SF", "value": proposed_value}],
                        "events": [{"type": "property_unavailable", "reason": "leased"}],
                    }
                    result = ai_processing._augment_proposal_with_deterministic_extractions(
                        proposal,
                        ["4800 Space Center Blvd", "", ""],
                        header,
                        config,
                        _conversation(text),
                    )
                    self.assertIsNone(
                        ai_processing._proposal_update_for_column(result, "Ops Ex / SF")
                    )

    def test_later_standalone_opex_wins_over_rejected_combined_total(self):
        text = (
            "CAM plus base rent totals $18.00/SF. "
            "CAM alone is $3.90/SF."
        )
        proposal = {"updates": [{"column": "Ops Ex / SF", "value": "18.00"}], "events": []}
        header = ["Property Address", "Rent/SF/Yr", "Ops Ex / SF"]
        config = {"mappings": {"rent_sf_yr": "Rent/SF/Yr", "ops_ex_sf": "Ops Ex / SF"}}
        self.assertEqual("3.90", ai_processing._extract_ops_ex_sf_from_text(text))
        result = ai_processing._augment_proposal_with_deterministic_extractions(
            proposal, ["4800 Space Center Blvd", "", ""], header, config, _conversation(text)
        )
        self.assertEqual(
            "3.90",
            ai_processing._proposal_update_for_column(result, "Ops Ex / SF")["value"],
        )

    def test_pending_or_unknown_opex_does_not_capture_later_costs(self):
        examples = (
            "CAM is not finalized; rent is $14.10 per square foot.",
            "CAM is pending; the asking rate is $14.10 per square foot.",
            "CAM is unknown; total occupancy cost is $18.00 per square foot.",
            "CAM is pending; taxes are $3.90 per square foot.",
        )
        for text in examples:
            with self.subTest(text=text):
                self.assertIsNone(ai_processing._extract_ops_ex_sf_from_text(text))

    def test_current_opex_wins_over_prior_figure(self):
        text = "Prior CAM was $4.25/SF; current CAM is $3.90/SF."
        self.assertEqual("3.90", ai_processing._extract_ops_ex_sf_from_text(text))

    def test_correction_discourse_marks_later_opex_as_current(self):
        examples = (
            "CAM is $4.25/SF; correction: CAM is $3.90/SF.",
            "CAM is $4.25/SF; corrected CAM is $3.90/SF.",
            "CAM is $4.25/SF; the correct CAM is $3.90/SF.",
            "CAM is $4.25/SF; actually, CAM is $3.90/SF.",
            "CAM is $4.25/SF; revised CAM is $3.90/SF.",
            "CAM is $4.25/SF; updated CAM is $3.90/SF.",
            "CAM is $4.25/SF; now CAM is $3.90/SF.",
            (
                "CAM is $4.25/SF; correction: CAM is $4.00/SF; "
                "correction: CAM is $3.90/SF."
            ),
            (
                "CAM is $4.25/SF; corrected CAM is $4.00/SF; "
                "actually, CAM is $3.90/SF."
            ),
        )
        header = ["Property Address", "Rent/SF/Yr", "Ops Ex / SF"]
        config = {"mappings": {"rent_sf_yr": "Rent/SF/Yr", "ops_ex_sf": "Ops Ex / SF"}}
        results = []

        for text in examples:
            augmented = ai_processing._augment_proposal_with_deterministic_extractions(
                {"updates": [{"column": "Ops Ex / SF", "value": "3.90"}]},
                ["4800 Space Center Blvd", "", ""],
                header,
                config,
                _conversation(text),
            )
            results.append((
                ai_processing._extract_ops_ex_sf_from_text(text),
                ai_processing._proposal_update_for_column(
                    augmented,
                    "Ops Ex / SF",
                )["value"],
            ))

        self.assertEqual([("3.90", "3.90")] * len(examples), results)

    def test_elliptical_opex_corrections_bind_to_prior_expense_field(self):
        examples = (
            "CAM is $4.25/SF, corrected to $3.90/SF.",
            "CAM is $4.25/SF; correction: $3.90/SF.",
            "CAM is $4.25/SF; actually $3.90/SF.",
            "CAM is $4.25/SF, now $3.90/SF.",
        )
        header = ["Property Address", "Rent/SF/Yr", "Ops Ex / SF"]
        config = {"mappings": {"rent_sf_yr": "Rent/SF/Yr", "ops_ex_sf": "Ops Ex / SF"}}
        results = []

        for text in examples:
            augmented = ai_processing._augment_proposal_with_deterministic_extractions(
                {
                    "updates": [
                        {"column": "Rent/SF/Yr", "value": "3.90"},
                        {"column": "Ops Ex / SF", "value": "3.90"},
                    ]
                },
                ["4800 Space Center Blvd", "", ""],
                header,
                config,
                _conversation(text),
            )
            rent_update = ai_processing._proposal_update_for_column(
                augmented,
                "Rent/SF/Yr",
            )
            opex_update = ai_processing._proposal_update_for_column(
                augmented,
                "Ops Ex / SF",
            )
            results.append((
                ai_processing._extract_rent_sf_yr_from_text(text),
                ai_processing._extract_ops_ex_sf_from_text(text),
                rent_update["value"] if rent_update is not None else None,
                opex_update["value"] if opex_update is not None else None,
            ))

        self.assertEqual(
            [(None, "3.90", None, "3.90")] * len(examples),
            results,
        )

    def test_elliptical_opex_corrections_keep_prior_rate_suffixes(self):
        examples = (
            (
                "CAM is $4.25/SF NNN, corrected to $3.90/SF NNN.",
                "3.90",
                "4.25",
            ),
            (
                "CAM is $0.34/SF/month, corrected to $0.36/SF/month.",
                "4.32",
                "4.08",
            ),
            (
                "CAM is $4.25/SF/year, corrected to $3.90/SF/year.",
                "3.90",
                "4.25",
            ),
        )
        header = ["Property Address", "Rent/SF/Yr", "Ops Ex / SF"]
        config = {"mappings": {"rent_sf_yr": "Rent/SF/Yr", "ops_ex_sf": "Ops Ex / SF"}}
        results = []

        for text, expected_opex, stale_opex in examples:
            augmented = ai_processing._augment_proposal_with_deterministic_extractions(
                {
                    "updates": [
                        {"column": "Rent/SF/Yr", "value": expected_opex},
                        {"column": "Ops Ex / SF", "value": stale_opex},
                    ],
                    "events": [],
                },
                ["4800 Space Center Blvd", "", ""],
                header,
                config,
                _conversation(text),
            )
            normalized = ai_processing._augment_proposal_opex_basis(
                augmented,
                ["4800 Space Center Blvd", "", ""],
                header,
                config,
                _conversation(text),
            )
            rent_update = ai_processing._proposal_update_for_column(
                normalized,
                "Rent/SF/Yr",
            )
            opex_update = ai_processing._proposal_update_for_column(
                normalized,
                "Ops Ex / SF",
            )
            results.append((
                ai_processing._extract_rent_sf_yr_from_text(text),
                ai_processing._extract_ops_ex_sf_from_text(text),
                rent_update["value"] if rent_update is not None else None,
                opex_update["value"] if opex_update is not None else None,
            ))

        self.assertEqual(
            [
                (None, expected_opex, None, expected_opex)
                for _, expected_opex, _ in examples
            ],
            results,
        )

    def test_negated_opex_figures_and_pronominal_corrections(self):
        examples = (
            (
                "CAM is not $4.25/SF; it is $3.90/SF.",
                "3.90",
            ),
            (
                "CAM is $3.90/SF, not $4.25/SF.",
                "4.25",
            ),
        )
        header = ["Property Address", "Rent/SF/Yr", "Ops Ex / SF"]
        config = {"mappings": {"rent_sf_yr": "Rent/SF/Yr", "ops_ex_sf": "Ops Ex / SF"}}
        results = []

        for text, preseeded_rent in examples:
            augmented = ai_processing._augment_proposal_with_deterministic_extractions(
                {
                    "updates": [
                        {"column": "Rent/SF/Yr", "value": preseeded_rent},
                        {"column": "Ops Ex / SF", "value": "3.90"},
                    ]
                },
                ["4800 Space Center Blvd", "", ""],
                header,
                config,
                _conversation(text),
            )
            rent_update = ai_processing._proposal_update_for_column(
                augmented,
                "Rent/SF/Yr",
            )
            opex_update = ai_processing._proposal_update_for_column(
                augmented,
                "Ops Ex / SF",
            )
            results.append((
                ai_processing._extract_rent_sf_yr_from_text(text),
                ai_processing._extract_ops_ex_sf_from_text(text),
                rent_update["value"] if rent_update is not None else None,
                opex_update["value"] if opex_update is not None else None,
            ))

        self.assertEqual(
            [(None, "3.90", None, "3.90")] * len(examples),
            results,
        )

    def test_pronominal_opex_corrections_keep_negated_rate_suffixes(self):
        examples = (
            (
                "CAM is not $4.25/SF NNN; it is $3.90/SF NNN.",
                "3.90",
                "4.25",
            ),
            (
                "CAM is not $0.34/SF/month; it is $0.36/SF/month.",
                "4.32",
                "4.08",
            ),
            (
                "CAM is not $4.25/SF/year; it is $3.90/SF/year.",
                "3.90",
                "4.25",
            ),
        )
        header = ["Property Address", "Rent/SF/Yr", "Ops Ex / SF"]
        config = {"mappings": {"rent_sf_yr": "Rent/SF/Yr", "ops_ex_sf": "Ops Ex / SF"}}
        results = []

        for text, expected_opex, stale_opex in examples:
            augmented = ai_processing._augment_proposal_with_deterministic_extractions(
                {
                    "updates": [
                        {"column": "Rent/SF/Yr", "value": expected_opex},
                        {"column": "Ops Ex / SF", "value": stale_opex},
                    ],
                    "events": [],
                },
                ["4800 Space Center Blvd", "", ""],
                header,
                config,
                _conversation(text),
            )
            normalized = ai_processing._augment_proposal_opex_basis(
                augmented,
                ["4800 Space Center Blvd", "", ""],
                header,
                config,
                _conversation(text),
            )
            rent_update = ai_processing._proposal_update_for_column(
                normalized,
                "Rent/SF/Yr",
            )
            opex_update = ai_processing._proposal_update_for_column(
                normalized,
                "Ops Ex / SF",
            )
            results.append((
                ai_processing._extract_rent_sf_yr_from_text(text),
                ai_processing._extract_ops_ex_sf_from_text(text),
                rent_update["value"] if rent_update is not None else None,
                opex_update["value"] if opex_update is not None else None,
            ))

        self.assertEqual(
            [
                (None, expected_opex, None, expected_opex)
                for _, expected_opex, _ in examples
            ],
            results,
        )

    def test_opex_correction_basis_changes_use_current_figure_only(self):
        examples = (
            (
                "CAM is $0.34/SF/month, corrected to $3.90/SF/year.",
                "3.90",
                "4.08",
            ),
            (
                "CAM is $4.25/SF/year, corrected to $0.36/SF/month.",
                "4.32",
                "4.25",
            ),
            (
                "CAM is not $0.34/SF/month; it is $3.90/SF/year.",
                "3.90",
                "4.08",
            ),
            (
                "CAM is not $4.25/SF/year; it is $0.36/SF/month.",
                "4.32",
                "4.25",
            ),
        )
        header = ["Property Address", "Rent/SF/Yr", "Ops Ex / SF"]
        config = {"mappings": {"rent_sf_yr": "Rent/SF/Yr", "ops_ex_sf": "Ops Ex / SF"}}
        results = []

        for text, expected_opex, stale_opex in examples:
            augmented = ai_processing._augment_proposal_with_deterministic_extractions(
                {
                    "updates": [
                        {"column": "Rent/SF/Yr", "value": expected_opex},
                        {"column": "Ops Ex / SF", "value": stale_opex},
                    ],
                    "events": [],
                },
                ["4800 Space Center Blvd", "", ""],
                header,
                config,
                _conversation(text),
            )
            once = ai_processing._augment_proposal_opex_basis(
                augmented,
                ["4800 Space Center Blvd", "", ""],
                header,
                config,
                _conversation(text),
            )
            once_updates = [dict(update) for update in (once.get("updates") or [])]
            twice = ai_processing._augment_proposal_opex_basis(
                once,
                ["4800 Space Center Blvd", "", ""],
                header,
                config,
                _conversation(text),
            )
            self.assertEqual(once_updates, twice.get("updates") or [])
            rent_update = ai_processing._proposal_update_for_column(
                twice,
                "Rent/SF/Yr",
            )
            opex_update = ai_processing._proposal_update_for_column(
                twice,
                "Ops Ex / SF",
            )
            results.append((
                ai_processing._extract_rent_sf_yr_from_text(text),
                ai_processing._extract_ops_ex_sf_from_text(text),
                rent_update["value"] if rent_update is not None else None,
                opex_update["value"] if opex_update is not None else None,
            ))

        self.assertEqual(
            [
                (None, expected_opex, None, expected_opex)
                for _, expected_opex, _ in examples
            ],
            results,
        )

    def test_opex_corrections_accept_nnn_before_basis_suffix(self):
        examples = (
            (
                "CAM is $0.34/SF NNN, billed monthly, corrected to $3.90/SF/year.",
                "3.90",
                "4.08",
            ),
            (
                "CAM is not $0.34/SF NNN, billed monthly; it is $3.90/SF/year.",
                "3.90",
                "4.08",
            ),
            (
                "CAM is $4.25/SF/year, corrected to $0.36/SF NNN/month.",
                "4.32",
                "4.25",
            ),
            (
                "CAM is $4.25/SF/year, corrected to $0.36/SF NNN, billed monthly.",
                "4.32",
                "4.25",
            ),
        )
        header = ["Property Address", "Rent/SF/Yr", "Ops Ex / SF"]
        config = {"mappings": {"rent_sf_yr": "Rent/SF/Yr", "ops_ex_sf": "Ops Ex / SF"}}
        results = []

        for text, expected_opex, stale_opex in examples:
            augmented = ai_processing._augment_proposal_with_deterministic_extractions(
                {
                    "updates": [
                        {"column": "Rent/SF/Yr", "value": expected_opex},
                        {"column": "Ops Ex / SF", "value": stale_opex},
                    ],
                    "events": [],
                },
                ["4800 Space Center Blvd", "", ""],
                header,
                config,
                _conversation(text),
            )
            once = ai_processing._augment_proposal_opex_basis(
                augmented,
                ["4800 Space Center Blvd", "", ""],
                header,
                config,
                _conversation(text),
            )
            once_updates = [dict(update) for update in (once.get("updates") or [])]
            twice = ai_processing._augment_proposal_opex_basis(
                once,
                ["4800 Space Center Blvd", "", ""],
                header,
                config,
                _conversation(text),
            )
            self.assertEqual(once_updates, twice.get("updates") or [])
            rent_update = ai_processing._proposal_update_for_column(
                twice,
                "Rent/SF/Yr",
            )
            opex_update = ai_processing._proposal_update_for_column(
                twice,
                "Ops Ex / SF",
            )
            results.append((
                ai_processing._extract_rent_sf_yr_from_text(text),
                ai_processing._extract_ops_ex_sf_from_text(text),
                rent_update["value"] if rent_update is not None else None,
                opex_update["value"] if opex_update is not None else None,
            ))

        self.assertEqual(
            [
                (None, expected_opex, None, expected_opex)
                for _, expected_opex, _ in examples
            ],
            results,
        )

    def test_negated_rent_figures_and_pronominal_corrections(self):
        examples = (
            "Rent is not $14.10/SF; it is $15.25/SF.",
            "Rent is $15.25/SF, not $14.10/SF.",
        )
        header = ["Property Address", "Rent/SF/Yr", "Ops Ex / SF"]
        config = {"mappings": {"rent_sf_yr": "Rent/SF/Yr", "ops_ex_sf": "Ops Ex / SF"}}
        results = []

        for text in examples:
            augmented = ai_processing._augment_proposal_with_deterministic_extractions(
                {
                    "updates": [
                        {"column": "Rent/SF/Yr", "value": "15.25"},
                        {"column": "Ops Ex / SF", "value": "14.10"},
                    ]
                },
                ["4800 Space Center Blvd", "", ""],
                header,
                config,
                _conversation(text),
            )
            rent_update = ai_processing._proposal_update_for_column(
                augmented,
                "Rent/SF/Yr",
            )
            opex_update = ai_processing._proposal_update_for_column(
                augmented,
                "Ops Ex / SF",
            )
            results.append((
                ai_processing._extract_rent_sf_yr_from_text(text),
                ai_processing._extract_ops_ex_sf_from_text(text),
                rent_update["value"] if rent_update is not None else None,
                opex_update["value"] if opex_update is not None else None,
            ))

        self.assertEqual(
            [("15.25", None, "15.25", None)] * len(examples),
            results,
        )

    def test_unrelated_later_rent_rate_does_not_correct_opex(self):
        examples = (
            "CAM is $4.25/SF; asking rate is $14.10/SF.",
            "CAM is $4.25/SF. The lease rate is $14.10/SF.",
        )
        header = ["Property Address", "Rent/SF/Yr", "Ops Ex / SF"]
        config = {"mappings": {"rent_sf_yr": "Rent/SF/Yr", "ops_ex_sf": "Ops Ex / SF"}}
        results = []

        for text in examples:
            augmented = ai_processing._augment_proposal_with_deterministic_extractions(
                {
                    "updates": [
                        {"column": "Rent/SF/Yr", "value": "14.10"},
                        {"column": "Ops Ex / SF", "value": "4.25"},
                    ]
                },
                ["4800 Space Center Blvd", "", ""],
                header,
                config,
                _conversation(text),
            )
            rent_update = ai_processing._proposal_update_for_column(
                augmented,
                "Rent/SF/Yr",
            )
            opex_update = ai_processing._proposal_update_for_column(
                augmented,
                "Ops Ex / SF",
            )
            results.append((
                ai_processing._extract_rent_sf_yr_from_text(text),
                ai_processing._extract_ops_ex_sf_from_text(text),
                rent_update["value"] if rent_update is not None else None,
                opex_update["value"] if opex_update is not None else None,
            ))

        self.assertEqual(
            [("14.10", "4.25", "14.10", "4.25")] * len(examples),
            results,
        )

    def test_current_cam_outranks_prior_component_list_estimate(self):
        text = (
            "Prior estimate: CAM, taxes, and insurance are estimated around $4.25/SF. "
            "Current CAM is $3.90/SF."
        )
        self.assertEqual("3.90", ai_processing._extract_ops_ex_sf_from_text(text))

    def test_current_cam_preserves_matching_model_opex_over_prior_estimate(self):
        text = (
            "Prior estimate: CAM, taxes, and insurance are estimated around $4.25/SF. "
            "Current CAM is $3.90/SF."
        )
        proposal = {"updates": [{"column": "Ops Ex / SF", "value": "3.90"}], "events": []}
        header = ["Property Address", "Rent/SF/Yr", "Ops Ex / SF"]
        config = {"mappings": {"rent_sf_yr": "Rent/SF/Yr", "ops_ex_sf": "Ops Ex / SF"}}
        result = ai_processing._augment_proposal_with_deterministic_extractions(
            proposal, ["4800 Space Center Blvd", "", ""], header, config, _conversation(text)
        )
        self.assertEqual(
            "3.90",
            ai_processing._proposal_update_for_column(result, "Ops Ex / SF")["value"],
        )

    def test_current_cam_outranks_prior_combined_component(self):
        text = "Prior quote: $14.00 NNN + $4.25 OPEX. Current CAM is $3.90/SF."
        self.assertEqual("3.90", ai_processing._extract_ops_ex_sf_from_text(text))

    def test_current_cam_preserves_model_value_over_prior_combined_component(self):
        text = "Prior quote: $14.00 NNN + $4.25 OPEX. Current CAM is $3.90/SF."
        proposal = {"updates": [{"column": "Ops Ex / SF", "value": "3.90"}], "events": []}
        header = ["Property Address", "Rent/SF/Yr", "Ops Ex / SF"]
        config = {"mappings": {"rent_sf_yr": "Rent/SF/Yr", "ops_ex_sf": "Ops Ex / SF"}}
        result = ai_processing._augment_proposal_with_deterministic_extractions(
            proposal, ["4800 Space Center Blvd", "", ""], header, config, _conversation(text)
        )
        self.assertEqual(
            "3.90",
            ai_processing._proposal_update_for_column(result, "Ops Ex / SF")["value"],
        )

    def test_rent_first_combined_total_with_article_is_not_opex(self):
        text = "Base rent plus the CAM equals $18.00 per square foot."
        self.assertIsNone(ai_processing._extract_ops_ex_sf_from_text(text))

    def test_rent_first_combined_total_with_article_removes_model_opex(self):
        text = "Base rent plus the CAM equals $18.00 per square foot."
        proposal = {
            "updates": [{"column": "Ops Ex / SF", "value": "18.00"}],
            "events": [{"type": "property_unavailable", "reason": "leased"}],
        }
        header = ["Property Address", "Rent/SF/Yr", "Ops Ex / SF"]
        config = {"mappings": {"rent_sf_yr": "Rent/SF/Yr", "ops_ex_sf": "Ops Ex / SF"}}
        result = ai_processing._augment_proposal_with_deterministic_extractions(
            proposal, ["4800 Space Center Blvd", "", ""], header, config, _conversation(text)
        )
        self.assertIsNone(ai_processing._proposal_update_for_column(result, "Ops Ex / SF"))

    def test_unresolved_projected_opex_range_is_not_extracted(self):
        text = "CAM is projected between $3.50 and $4.25 per square foot."
        self.assertIsNone(ai_processing._extract_ops_ex_sf_from_text(text))

    def test_evidence_bounded_opex_positive_matrix(self):
        examples = {
            "CAM, on top of base rent, is $3.90 per square foot.": "3.90",
            "CAM (in addition to base rent) is $3.90 per square foot.": "3.90",
            "Rent is $14.10 and CAM is $3.90 per square foot.": "3.90",
            "2,000 SF: $1.25 NNN + $0.34 OPEX = $1.59 PSF / Month.": "4.08",
            "CAM is $0.34/SF/month.": "4.08",
        }
        for text, expected in examples.items():
            with self.subTest(text=text):
                self.assertEqual(expected, ai_processing._extract_ops_ex_sf_from_text(text))

    def test_combined_component_keeps_trailing_monthly_context(self):
        text = "$1.25 NNN + $0.34 OPEX = $1.59 PSF, billed monthly."
        self.assertEqual("4.08", ai_processing._extract_ops_ex_sf_from_text(text))

    def test_combined_equation_monthly_suffixes_normalize_shared_winner(self):
        examples = (
            "$1.25 NNN + $0.34 OPEX = $1.59/SF/month",
            "$1.25 NNN + $0.34 OPEX = $1.59 per SF/month",
            "$1.25 NNN + $0.34 OPEX = $1.59 per-SF/month",
            "$1.25 NNN + $0.34 OPEX = $1.59 per sq. ft., billed monthly",
            "$1.25 NNN + $0.34 OPEX = $1.59 per square foot, billed monthly",
        )
        header = ["Property Address", "Rent/SF/Yr", "Ops Ex / SF"]
        config = {"mappings": {"rent_sf_yr": "Rent/SF/Yr", "ops_ex_sf": "Ops Ex / SF"}}
        results = []

        for text in examples:
            proposal = {"updates": [{"column": "Ops Ex / SF", "value": "0.34"}]}
            normalized = ai_processing._augment_proposal_opex_basis(
                proposal,
                ["4800 Space Center Blvd", "", ""],
                header,
                config,
                _conversation(text),
            )
            results.append((
                ai_processing._extract_ops_ex_sf_from_text(text),
                ai_processing._proposal_update_for_column(
                    normalized,
                    "Ops Ex / SF",
                )["value"],
            ))

        self.assertEqual([("4.08", "4.08")] * len(examples), results)

    def test_standalone_monthly_basis_phrasings_are_annualized(self):
        examples = (
            "CAM is $0.34 per square foot, billed monthly.",
            "CAM is $0.34 per sq ft., billed monthly.",
            "CAM is $0.34 per sq ft, billed monthly.",
            "CAM is $0.34 per sq ft. monthly.",
            "CAM is $0.34 per sq ft monthly.",
            "CAM is $0.34 per square foot, billed on a monthly basis.",
            "CAM is $0.34 per square foot, billed on the monthly basis.",
        )
        self.assertEqual(
            ["4.08"] * len(examples),
            [ai_processing._extract_ops_ex_sf_from_text(text) for text in examples],
        )

    def test_attached_monthly_basis_survives_following_prose(self):
        examples = (
            "CAM is $0.34/SF/month plus tax.",
            "CAM is $0.34/SF/month under the lease.",
            "CAM is $0.34/SF/month estimated.",
            "CAM is $0.34/SF, billed on a monthly basis under the lease.",
        )
        header = ["Property Address", "Rent/SF/Yr", "Ops Ex / SF"]
        config = {"mappings": {"rent_sf_yr": "Rent/SF/Yr", "ops_ex_sf": "Ops Ex / SF"}}
        results = []

        for text in examples:
            proposal = {"updates": [{"column": "Ops Ex / SF", "value": "0.34"}]}
            normalized = ai_processing._augment_proposal_opex_basis(
                proposal,
                ["4800 Space Center Blvd", "", ""],
                header,
                config,
                _conversation(text),
            )
            results.append((
                ai_processing._extract_ops_ex_sf_from_text(text),
                ai_processing._proposal_update_for_column(
                    normalized,
                    "Ops Ex / SF",
                )["value"],
            ))

        self.assertEqual([("4.08", "4.08")] * len(examples), results)

    def test_attached_monthly_opex_keeps_supporting_tax_context(self):
        examples = (
            "OpEx is $0.34/SF/month for taxes and insurance.",
            "OpEx is $0.34/SF monthly for taxes and insurance.",
            "OpEx is $0.34/SF monthly for property taxes and insurance.",
            "OpEx is $0.34/SF monthly for real estate taxes and insurance.",
            "OpEx is $0.34/SF/month for property taxes and insurance.",
            "OpEx is $0.34/SF, billed monthly for real estate taxes and insurance.",
            "OpEx is $0.34/SF/month for insurance and taxes.",
            "OpEx is $0.34/SF/month for property insurance and real estate taxes.",
        )
        header = ["Property Address", "Rent/SF/Yr", "Ops Ex / SF"]
        config = {"mappings": {"rent_sf_yr": "Rent/SF/Yr", "ops_ex_sf": "Ops Ex / SF"}}
        results = []

        for text in examples:
            proposal = {"updates": [{"column": "Ops Ex / SF", "value": "0.34"}]}
            extracted = ai_processing._augment_proposal_with_deterministic_extractions(
                proposal,
                ["4800 Space Center Blvd", "", ""],
                header,
                config,
                _conversation(text),
            )
            normalized = ai_processing._augment_proposal_opex_basis(
                extracted,
                ["4800 Space Center Blvd", "", ""],
                header,
                config,
                _conversation(text),
            )
            results.append((
                ai_processing._extract_ops_ex_sf_from_text(text),
                ai_processing._proposal_update_for_column(
                    normalized,
                    "Ops Ex / SF",
                )["value"],
            ))

        self.assertEqual([("4.08", "4.08")] * len(examples), results)

    def test_direct_monthly_tax_subject_does_not_annualize_cam(self):
        examples = (
            "CAM is $4.00/SF, per month taxes are $0.50/SF.",
            "CAM is $4.00/SF, per month insurance is $0.50/SF.",
            "CAM is $4.00/SF, per month property taxes are $0.50/SF.",
            "CAM is $4.00/SF, per month real estate taxes are $0.50/SF.",
            "CAM is $4.00/SF, per month property insurance is $0.50/SF.",
            "CAM is $4.00/SF, monthly for property taxes: $0.50/SF.",
            "CAM is $4.00/SF, monthly for real estate taxes: $0.50/SF.",
            "CAM is $4.00/SF, monthly for property insurance: $0.50/SF.",
        )
        self.assertEqual(
            ["4.00"] * len(examples),
            [ai_processing._extract_ops_ex_sf_from_text(text) for text in examples],
        )

    def test_attached_monthly_basis_rejects_competing_subjects(self):
        examples = (
            "CAM is $4.00/SF/month parking is billed separately.",
            "CAM is $4.00/SF/month: report attached.",
            "CAM is $4.00/SF, billed on a monthly basis - parking is separate.",
            "CAM is $4.00/SF, billed monthly (report attached).",
        )
        self.assertEqual(
            ["4.00"] * len(examples),
            [ai_processing._extract_ops_ex_sf_from_text(text) for text in examples],
        )

    def test_attached_monthly_basis_ignores_adjacent_subject_basis_markers(self):
        examples = (
            ("CAM is $0.34/SF/month, per year parking is $12/SF.", "0.34", "4.08"),
            ("CAM is $4.00/SF, billed monthly for parking.", "4.00", "4.00"),
            ("CAM is $4.00/SF/month for property taxes: $0.50/SF.", "4.00", "4.00"),
            ("CAM is $4.00/SF per month for real estate taxes: $0.50/SF.", "4.00", "4.00"),
            ("CAM is $4.00/SF, billed monthly for property insurance: $0.50/SF.", "4.00", "4.00"),
            ("CAM is $4.00/SF, per month asking rate is $1.20/SF.", "4.00", "4.00"),
            ("CAM is $4.00/SF/month for quoted rate: $1.20/SF.", "4.00", "4.00"),
            ("CAM is $4.00/SF, billed monthly for lease price: $1.20/SF.", "4.00", "4.00"),
            ("CAM is $4.00/SF monthly for asking price: $1.20/SF.", "4.00", "4.00"),
        )
        header = ["Property Address", "Rent/SF/Yr", "Ops Ex / SF"]
        config = {"mappings": {"rent_sf_yr": "Rent/SF/Yr", "ops_ex_sf": "Ops Ex / SF"}}
        results = []

        for text, raw_opex, expected in examples:
            proposal = {"updates": [{"column": "Ops Ex / SF", "value": raw_opex}]}
            normalized = ai_processing._augment_proposal_opex_basis(
                proposal,
                ["4800 Space Center Blvd", "", ""],
                header,
                config,
                _conversation(text),
            )
            results.append((
                ai_processing._extract_ops_ex_sf_from_text(text),
                ai_processing._proposal_update_for_column(
                    normalized,
                    "Ops Ex / SF",
                )["value"],
            ))

        self.assertEqual(
            [(expected, expected) for _, _, expected in examples],
            results,
        )

    def test_existing_monthly_basis_controls_remain_owned(self):
        examples = (
            "CAM is $0.34 PSF/month.",
            "CAM is $4.00/SF; monthly: rent is $1.20/SF.",
            "CAM is $0.34/SF/month, parking billed annually.",
            "$1.25 NNN + $0.34 OPEX = $1.59/SF/month, parking billed annually.",
        )
        self.assertEqual(
            ["4.08", "4.00", "4.08", "4.08"],
            [ai_processing._extract_ops_ex_sf_from_text(text) for text in examples],
        )

    def test_combined_component_ignores_prior_clause_monthly_context(self):
        text = "CAM $0.50/month fee. $14.00 NNN + $4.00 OPEX."
        self.assertEqual("4.00", ai_processing._extract_ops_ex_sf_from_text(text))

    def test_combined_component_ignores_standalone_monthly_sentence(self):
        text = "Monthly. $14.00 NNN + $4.00 OPEX."
        self.assertEqual("4.00", ai_processing._extract_ops_ex_sf_from_text(text))

    def test_combined_component_ignores_prior_monthly_sentence(self):
        text = "Prior fee billed monthly. $14.00 NNN + $4.00 OPEX."
        self.assertEqual("4.00", ai_processing._extract_ops_ex_sf_from_text(text))

    def test_standalone_cam_monthly_sq_ft_variants_are_annualized(self):
        examples = (
            "CAM is $0.34 per sq. ft. monthly.",
            "CAM is $0.34 per sq.ft. monthly.",
        )
        for text in examples:
            with self.subTest(text=text):
                self.assertEqual("4.08", ai_processing._extract_ops_ex_sf_from_text(text))

    def test_monthly_rent_after_colon_does_not_annualize_cam(self):
        text = "CAM is $4.00/SF: monthly rent is $1.20/SF."
        self.assertEqual("4.00", ai_processing._extract_ops_ex_sf_from_text(text))

    def test_monthly_rent_after_comma_does_not_annualize_cam(self):
        text = "CAM is $4.00/SF, monthly rent is $1.20/SF."
        self.assertEqual("4.00", ai_processing._extract_ops_ex_sf_from_text(text))

    def test_cam_sq_ft_monthly_suffixes_are_annualized(self):
        examples = (
            "CAM is $0.34 per sq. ft./month",
            "CAM is $0.34 per sq. ft., billed monthly.",
        )
        for text in examples:
            with self.subTest(text=text):
                self.assertEqual("4.08", ai_processing._extract_ops_ex_sf_from_text(text))

    def test_prior_monthly_rent_does_not_annualize_later_cam(self):
        text = "Rent: $1.25/SF/month, CAM: $4.00/SF"
        self.assertEqual("4.00", ai_processing._extract_ops_ex_sf_from_text(text))

    def test_following_rent_billing_does_not_annualize_cam(self):
        text = "CAM: $4.00/SF, rent billed monthly"
        self.assertEqual("4.00", ai_processing._extract_ops_ex_sf_from_text(text))

    def test_following_per_month_rent_does_not_annualize_cam(self):
        text = "CAM is $4.00/SF, per month rent is $1.20/SF."
        self.assertEqual("4.00", ai_processing._extract_ops_ex_sf_from_text(text))

    def test_following_per_year_rent_does_not_conflict_with_monthly_cam(self):
        text = "CAM is $0.34/SF/month, per year rent is $14/SF."
        self.assertEqual("4.08", ai_processing._extract_ops_ex_sf_from_text(text))

    def test_following_rent_billing_does_not_annualize_combined_opex(self):
        text = "$14 NNN + $4 OPEX, rent billed monthly"
        self.assertEqual("4.00", ai_processing._extract_ops_ex_sf_from_text(text))

    def test_combined_opex_sq_ft_billed_monthly_is_annualized(self):
        text = "$14 NNN + $0.34 OPEX per sq. ft., billed monthly."
        self.assertEqual("4.08", ai_processing._extract_ops_ex_sf_from_text(text))

    def test_pending_cam_does_not_annualize_monthly_rent_proposal_opex(self):
        text = "CAM pending; rent is $1.25/SF/month"
        proposal = {"updates": [{"column": "Ops Ex / SF", "value": "1.25"}]}
        header = ["Property Address", "Rent/SF/Yr", "Ops Ex / SF"]
        config = {"mappings": {"rent_sf_yr": "Rent/SF/Yr", "ops_ex_sf": "Ops Ex / SF"}}
        result = ai_processing._augment_proposal_opex_basis(
            proposal,
            ["4800 Space Center Blvd", "", ""],
            header,
            config,
            _conversation(text),
        )
        self.assertEqual(
            "1.25",
            ai_processing._proposal_update_for_column(result, "Ops Ex / SF")["value"],
        )

    def test_proposal_opex_basis_uses_shared_combined_winner_idempotently(self):
        text = "$1.25 NNN + $0.34 OPEX = $1.59 PSF/month."
        header = ["Property Address", "Rent/SF/Yr", "Ops Ex / SF"]
        config = {"mappings": {"rent_sf_yr": "Rent/SF/Yr", "ops_ex_sf": "Ops Ex / SF"}}

        for preseed, expected in (
            ("0.34", "4.08"),
            ("4.08", "4.08"),
            ("2.00", "2.00"),
        ):
            with self.subTest(preseed=preseed):
                proposal = {"updates": [{"column": "Ops Ex / SF", "value": preseed}]}
                once = ai_processing._augment_proposal_opex_basis(
                    proposal,
                    ["4800 Space Center Blvd", "", ""],
                    header,
                    config,
                    _conversation(text),
                )
                twice = ai_processing._augment_proposal_opex_basis(
                    once,
                    ["4800 Space Center Blvd", "", ""],
                    header,
                    config,
                    _conversation(text),
                )
                self.assertEqual(
                    expected,
                    ai_processing._proposal_update_for_column(twice, "Ops Ex / SF")["value"],
                )

    def test_combined_base_values_are_negative_terminal_opex_evidence(self):
        text = "$1.25 NNN + $0.34 OPEX = $1.59/SF/month"
        header = ["Property Address", "Rent/SF/Yr", "Ops Ex / SF"]
        config = {"mappings": {"rent_sf_yr": "Rent/SF/Yr", "ops_ex_sf": "Ops Ex / SF"}}
        results = []

        for proposed_value in ("1.25", "15.00", "0.34", "4.08"):
            proposal = {
                "updates": [{"column": "Ops Ex / SF", "value": proposed_value}],
                "events": [{"type": "property_unavailable", "reason": "leased"}],
            }
            result = ai_processing._augment_proposal_with_deterministic_extractions(
                proposal,
                ["4800 Space Center Blvd", "", ""],
                header,
                config,
                _conversation(text),
            )
            update = ai_processing._proposal_update_for_column(result, "Ops Ex / SF")
            results.append(update["value"] if update is not None else None)

        self.assertEqual([None, None, "0.34", "4.08"], results)

    def test_conflicting_combined_basis_keeps_base_values_negative(self):
        text = "$1.25 NNN + $0.34 OPEX = $1.59/SF/year/month."
        header = ["Property Address", "Rent/SF/Yr", "Ops Ex / SF"]
        config = {"mappings": {"rent_sf_yr": "Rent/SF/Yr", "ops_ex_sf": "Ops Ex / SF"}}

        for proposed_value in ("1.25", "15.00"):
            with self.subTest(proposed_value=proposed_value):
                proposal = {
                    "updates": [{"column": "Ops Ex / SF", "value": proposed_value}],
                    "events": [{"type": "property_unavailable", "reason": "leased"}],
                }
                result = ai_processing._augment_proposal_with_deterministic_extractions(
                    proposal,
                    ["4800 Space Center Blvd", "", ""],
                    header,
                    config,
                    _conversation(text),
                )
                self.assertIsNone(
                    ai_processing._proposal_update_for_column(result, "Ops Ex / SF")
                )

    def test_proposal_opex_basis_ignores_entirely_quoted_monthly_candidate(self):
        proposal = {"updates": [{"column": "Ops Ex / SF", "value": "0.34"}]}
        header = ["Property Address", "Rent/SF/Yr", "Ops Ex / SF"]
        config = {"mappings": {"rent_sf_yr": "Rent/SF/Yr", "ops_ex_sf": "Ops Ex / SF"}}

        result = ai_processing._augment_proposal_opex_basis(
            proposal,
            ["4800 Space Center Blvd", "", ""],
            header,
            config,
            _conversation("> CAM is $0.34/SF/month"),
        )

        self.assertEqual(
            "0.34",
            ai_processing._proposal_update_for_column(result, "Ops Ex / SF")["value"],
        )

    def test_monthly_report_after_cam_is_not_a_basis_marker(self):
        text = "CAM is $4.00/SF, monthly report attached."
        self.assertEqual("4.00", ai_processing._extract_ops_ex_sf_from_text(text))

    def test_punctuated_monthly_subject_does_not_annualize_cam(self):
        examples = (
            "CAM is $4.00/SF, monthly-report attached.",
            "CAM is $4.00/SF, monthly - rent is $1.20/SF.",
            "CAM is $4.00/SF, monthly: rent is $1.20/SF.",
            "CAM is $4.00/SF, monthly (rent is $1.20/SF).",
        )
        self.assertEqual(
            ["4.00"] * len(examples),
            [ai_processing._extract_ops_ex_sf_from_text(text) for text in examples],
        )

    def test_monthly_report_inside_cam_clause_is_not_a_basis_marker(self):
        examples = (
            "CAM monthly report is $4.00/SF.",
            "CAM report monthly is $4.00/SF.",
        )
        for text in examples:
            with self.subTest(text=text):
                self.assertEqual("4.00", ai_processing._extract_ops_ex_sf_from_text(text))

    def test_unrelated_annual_rent_does_not_override_monthly_cam_basis(self):
        text = "Rent: $1.25/SF/year, CAM: $0.34/SF/month"
        self.assertEqual("4.08", ai_processing._extract_ops_ex_sf_from_text(text))

    def test_conflicting_owned_cam_basis_abstains(self):
        text = "CAM is $0.34/SF/month/year"
        self.assertIsNone(ai_processing._extract_ops_ex_sf_from_text(text))

    def test_conflicting_owned_narrow_cam_basis_abstains(self):
        examples = (
            "CAM, taxes, and insurance are estimated around $0.34/SF/month/year.",
            "CAM, on top of base rent, is $0.34/SF/month/year.",
        )
        for text in examples:
            with self.subTest(text=text):
                self.assertIsNone(ai_processing._extract_ops_ex_sf_from_text(text))

    def test_conflicting_owned_bare_annual_basis_abstains(self):
        examples = (
            "CAM is $0.34/SF/month, annual",
            "CAM is $0.34/SF/month, annually",
            "CAM is $0.34/SF/month, yearly",
        )
        for text in examples:
            with self.subTest(text=text):
                self.assertIsNone(ai_processing._extract_ops_ex_sf_from_text(text))

    def test_bare_annually_cam_basis_remains_annual(self):
        text = "CAM is $4.00/SF annually"
        self.assertEqual("4.00", ai_processing._extract_ops_ex_sf_from_text(text))

    def test_unrelated_billed_monthly_subject_does_not_annualize_cam(self):
        examples = (
            "CAM is $4.00/SF, base rate billed monthly.",
            "CAM is $4.00/SF, lease billed monthly.",
            "CAM is $4.00/SF, parking billed monthly.",
            "CAM is $4.00/SF, report billed monthly.",
        )
        for text in examples:
            with self.subTest(text=text):
                self.assertEqual("4.00", ai_processing._extract_ops_ex_sf_from_text(text))

    def test_supported_raw_monthly_opex_survives_rejected_total_then_normalizes(self):
        text = (
            "CAM plus rent is $0.34/SF/month. "
            "CAM alone is $0.34/SF/month."
        )
        proposal = {
            "updates": [{"column": "Ops Ex / SF", "value": "0.34"}],
            "events": [{"type": "property_unavailable", "reason": "leased"}],
        }
        header = ["Property Address", "Rent/SF/Yr", "Ops Ex / SF"]
        config = {"mappings": {"rent_sf_yr": "Rent/SF/Yr", "ops_ex_sf": "Ops Ex / SF"}}
        stripped = ai_processing._augment_proposal_with_deterministic_extractions(
            proposal,
            ["4800 Space Center Blvd", "", ""],
            header,
            config,
            _conversation(text),
        )
        result = ai_processing._augment_proposal_opex_basis(
            stripped,
            ["4800 Space Center Blvd", "", ""],
            header,
            config,
            _conversation(text),
        )
        update = ai_processing._proposal_update_for_column(result, "Ops Ex / SF")
        self.assertIsNotNone(update)
        self.assertEqual(
            "4.08",
            update["value"],
        )

    def test_combined_total_rejection_ignores_unrelated_annual_parking(self):
        text = "CAM plus rent is $1.50/SF/month, per year parking is $12/SF."
        header = ["Property Address", "Rent/SF/Yr", "Ops Ex / SF"]
        config = {"mappings": {"rent_sf_yr": "Rent/SF/Yr", "ops_ex_sf": "Ops Ex / SF"}}
        results = []

        self.assertIsNone(ai_processing._extract_ops_ex_sf_from_text(text))
        for proposed_value in ("1.50", "18.00"):
            proposal = {
                "updates": [{"column": "Ops Ex / SF", "value": proposed_value}],
                "events": [{"type": "property_unavailable", "reason": "leased"}],
            }
            result = ai_processing._augment_proposal_with_deterministic_extractions(
                proposal,
                ["4800 Space Center Blvd", "", ""],
                header,
                config,
                _conversation(text),
            )
            results.append(
                ai_processing._proposal_update_for_column(result, "Ops Ex / SF")
            )

        self.assertEqual([None, None], results)

    def test_conflicting_basis_combined_total_rejects_raw_and_monthly_values(self):
        text = "CAM plus rent is $1.50/SF/month/year."
        header = ["Property Address", "Rent/SF/Yr", "Ops Ex / SF"]
        config = {"mappings": {"rent_sf_yr": "Rent/SF/Yr", "ops_ex_sf": "Ops Ex / SF"}}
        results = []

        for proposed_value in ("1.50", "18.00"):
            proposal = {
                "updates": [{"column": "Ops Ex / SF", "value": proposed_value}],
                "events": [{"type": "property_unavailable", "reason": "leased"}],
            }
            result = ai_processing._augment_proposal_with_deterministic_extractions(
                proposal,
                ["4800 Space Center Blvd", "", ""],
                header,
                config,
                _conversation(text),
            )
            results.append(
                ai_processing._proposal_update_for_column(result, "Ops Ex / SF")
            )

        self.assertEqual([None, None], results)

    def test_rampable_dock_is_not_a_terminal_drive_in_mismatch(self):
        proposal = {
            "updates": [],
            "events": [
                {"type": "property_unavailable", "reason": "requirements_mismatch"}
            ],
            "response_email": "We'll cross this one off.",
        }
        conversation = _conversation(
            "No drive in door. 1 loading dock. The loading dock can be ramped "
            "for drive in. The unit is 7753 sf."
        )

        result = ai_processing._augment_events_with_deterministic_signals(
            proposal,
            conversation,
            target_anchor="102 Iron Mountain Rd, Mine Hill",
        )

        event_types = [event.get("type") for event in result["events"]]
        self.assertNotIn("property_unavailable", event_types)
        self.assertIn("needs_user_input", event_types)
        self.assertIsNone(result["response_email"])

    def test_rampable_dock_does_not_hide_separate_office_mismatch(self):
        self.assertTrue(
            ai_processing._looks_like_requirements_mismatch_nonviable(
                "The space is too office-heavy for the client. The dock could be "
                "ramped for drive-in access, but there is almost no warehouse."
            )
        )

    def test_access_remediation_variants_do_not_terminalize_the_property(self):
        examples = (
            "No drive-in door. The loading dock is rampable for drive-in access.",
            "There is no grade-level door today, but the owner will convert the "
            "loading dock to grade-level access.",
            "This is not a fit as-is because there is no drive-in, but the dock "
            "can be ramped for drive-in access.",
        )

        for body in examples:
            with self.subTest(body=body):
                proposal = {
                    "updates": [],
                    "events": [
                        {
                            "type": "property_unavailable",
                            "reason": "requirements_mismatch",
                        }
                    ],
                    "response_email": "We'll cross this one off.",
                }
                result = ai_processing._augment_events_with_deterministic_signals(
                    proposal,
                    _conversation(body),
                    target_anchor="102 Iron Mountain Rd, Mine Hill",
                )

                event_types = [event.get("type") for event in result["events"]]
                self.assertNotIn("property_unavailable", event_types)
                self.assertIn("needs_user_input", event_types)
                self.assertIsNone(result["response_email"])

    def test_negated_access_remediation_remains_terminal(self):
        examples = (
            "No drive-in door, and the loading dock could not be ramped.",
            "There is no grade-level access and the dock is not rampable.",
        )

        for body in examples:
            with self.subTest(body=body):
                self.assertFalse(ai_processing._looks_like_access_remediation(body))
                self.assertTrue(
                    ai_processing._looks_like_requirements_mismatch_nonviable(body)
                )

    def test_matching_route_address_brochure_is_not_treated_as_competing(self):
        proposal = {
            "updates": [{"column": "Total SF", "value": "7500"}],
            "events": [],
            "response_email": None,
        }
        result = ai_processing._suppress_competing_attachment_updates(
            proposal,
            _conversation("We have the current building available; brochure attached."),
            "3344 S Carolina 51, Fort Mill",
            [{
                "name": "3344 SC-51 brochure.pdf",
                "text": "3344 S Carolina 51, Fort Mill - 7,500 SF",
            }],
        )

        self.assertEqual([{"column": "Total SF", "value": "7500"}], result["updates"])

    def test_target_brochure_does_not_bless_value_from_competing_brochure(self):
        proposal = {
            "updates": [
                {"column": "Ceiling Ht", "value": "32", "confidence": 0.96},
                {"column": "Total SF", "value": "20000", "confidence": 0.98},
            ],
            "events": [],
            "response_email": None,
        }
        target_brochure = {
            "name": "100 Main St brochure.pdf",
            "text": "100 Main St - 20,000 SF industrial building.",
        }
        competing_brochure = {
            "name": "200 Oak Ave brochure.pdf",
            "text": "200 Oak Ave - Ceiling Ht: 32 feet clear.",
        }

        result = ai_processing._suppress_competing_attachment_updates(
            proposal,
            _conversation("We have two options; the brochures are attached."),
            "100 Main St, Phoenix",
            [target_brochure, competing_brochure],
        )

        self.assertEqual(
            [{"column": "Total SF", "value": "20000", "confidence": 0.98}],
            result["updates"],
        )

    def test_named_alternate_in_fresh_text_cannot_be_target_evidence(self):
        proposal = {
            "updates": [{"column": "Ceiling Ht", "value": "32"}],
            "events": [],
            "response_email": "Thanks.",
        }

        result = ai_processing._suppress_competing_attachment_updates(
            proposal,
            _conversation(
                "100 Main St remains available. Another option, Oak Commerce "
                "Center, has 32 feet clear. Brochures attached."
            ),
            "100 Main St, Phoenix",
            [
                {
                    "name": "100 Main St brochure.pdf",
                    "text": "100 Main St - 20,000 SF.",
                },
                {
                    "name": "200 Oak Ave brochure.pdf",
                    "text": "200 Oak Ave - Ceiling Ht: 32 feet clear.",
                },
            ],
        )

        self.assertEqual([], result["updates"])
        self.assertTrue(any(
            event.get("type") == "needs_user_input"
            and event.get("reason") == "multi_property_attachment"
            for event in result["events"]
        ))
        self.assertIsNone(result["response_email"])

    def test_named_addressless_alternate_brochure_is_competing(self):
        proposal = {
            "updates": [{"column": "Ceiling Ht", "value": "32"}],
            "events": [],
            "response_email": "Thanks.",
        }

        result = ai_processing._suppress_competing_attachment_updates(
            proposal,
            _conversation(
                "We also have another option, Oak Commerce Center; brochures "
                "attached."
            ),
            "100 Main St, Phoenix",
            [
                {
                    "name": "100 Main St brochure.pdf",
                    "text": "100 Main St - 20,000 SF.",
                },
                {
                    "name": "Oak Commerce Center brochure.pdf",
                    "text": "Oak Commerce Center - Ceiling Ht: 32 feet clear.",
                },
            ],
        )

        self.assertEqual([], result["updates"])
        self.assertTrue(any(
            event.get("type") == "needs_user_input"
            and event.get("reason") == "multi_property_attachment"
            for event in result["events"]
        ))
        self.assertIsNone(result["response_email"])

    def test_addressless_target_attachment_survives_parseable_competitor(self):
        expected_update = {"column": "Ceiling Ht", "value": "28"}
        proposal = {
            "updates": [expected_update],
            "events": [],
            "response_email": None,
        }

        result = ai_processing._suppress_competing_attachment_updates(
            proposal,
            _conversation(
                "For 100 Main St, use current-property specs.pdf: it has the target "
                "property specifications. 200 Oak Ave is the other option."
            ),
            "100 Main St, Phoenix",
            [
                {
                    "name": "current-property specs.pdf",
                    "text": "Ceiling Ht: 28 feet clear.",
                },
                {
                    "name": "200 Oak Ave brochure.pdf",
                    "text": "200 Oak Ave - Ceiling Ht: 32 feet clear.",
                },
            ],
        )

        self.assertEqual([expected_update], result["updates"])

    def test_postfixed_alternate_cue_cannot_supply_target_evidence(self):
        proposal = {
            "updates": [{"column": "Ceiling Ht", "value": "32"}],
            "events": [],
            "response_email": "Thanks.",
        }

        result = ai_processing._suppress_competing_attachment_updates(
            proposal,
            _conversation(
                "100 Main St remains available. Oak Commerce Center has 32 feet "
                "clear and is another option."
            ),
            "100 Main St, Phoenix",
            [
                {
                    "name": "100 Main St brochure.pdf",
                    "text": "100 Main St - 20,000 SF.",
                },
                {
                    "name": "200 Oak Ave brochure.pdf",
                    "text": "200 Oak Ave - Ceiling Ht: 32 feet clear.",
                },
            ],
        )

        self.assertEqual([], result["updates"])
        self.assertTrue(any(
            event.get("type") == "needs_user_input"
            and event.get("reason") == "multi_property_attachment"
            for event in result["events"]
        ))
        self.assertIsNone(result["response_email"])

    def test_separate_named_property_clause_cannot_supply_target_evidence(self):
        proposal = {
            "updates": [{"column": "Ceiling Ht", "value": "32"}],
            "events": [],
            "response_email": "Thanks.",
        }

        result = ai_processing._suppress_competing_attachment_updates(
            proposal,
            _conversation(
                "100 Main St remains available. Separately, Oak Commerce Center "
                "has 32 feet clear."
            ),
            "100 Main St, Phoenix",
            [
                {
                    "name": "100 Main St brochure.pdf",
                    "text": "100 Main St - 20,000 SF.",
                },
                {
                    "name": "200 Oak Ave brochure.pdf",
                    "text": "200 Oak Ave - Ceiling Ht: 32 feet clear.",
                },
            ],
        )

        self.assertEqual([], result["updates"])
        self.assertTrue(any(
            event.get("type") == "needs_user_input"
            and event.get("reason") == "multi_property_attachment"
            for event in result["events"]
        ))
        self.assertIsNone(result["response_email"])

    def test_named_property_in_target_address_pdf_is_mixed_evidence(self):
        proposal = {
            "updates": [{"column": "Ceiling Ht", "value": "32"}],
            "events": [],
            "response_email": "Thanks.",
        }

        result = ai_processing._suppress_competing_attachment_updates(
            proposal,
            _conversation("The brochure is attached."),
            "100 Main St, Phoenix",
            [{
                "name": "mixed brochure.pdf",
                "text": (
                    "100 Main St - 20,000 SF. Oak Commerce Center - Ceiling Ht: "
                    "32 feet clear."
                ),
            }],
        )

        self.assertEqual([], result["updates"])
        self.assertTrue(any(
            event.get("type") == "needs_user_input"
            and event.get("reason") == "multi_property_attachment"
            for event in result["events"]
        ))
        self.assertIsNone(result["response_email"])

    def test_named_addressless_second_choice_brochure_is_competing(self):
        proposal = {
            "updates": [{"column": "Ceiling Ht", "value": "32"}],
            "events": [],
            "response_email": "Thanks.",
        }

        result = ai_processing._suppress_competing_attachment_updates(
            proposal,
            _conversation("Oak Commerce Center is a second choice."),
            "100 Main St, Phoenix",
            [
                {
                    "name": "100 Main St brochure.pdf",
                    "text": "100 Main St - 20,000 SF.",
                },
                {
                    "name": "Oak Commerce Center brochure.pdf",
                    "text": "Oak Commerce Center - Ceiling Ht: 32 feet clear.",
                },
            ],
        )

        self.assertEqual([], result["updates"])
        self.assertTrue(any(
            event.get("type") == "needs_user_input"
            and event.get("reason") == "multi_property_attachment"
            for event in result["events"]
        ))
        self.assertIsNone(result["response_email"])

    def test_versioned_named_brochure_with_postfixed_alternate_cue_is_competing(self):
        proposal = {
            "updates": [{"column": "Ceiling Ht", "value": "32"}],
            "events": [],
            "response_email": "Thanks.",
        }

        result = ai_processing._suppress_competing_attachment_updates(
            proposal,
            _conversation(
                "oak commerce center has 32 feet clear and is an alternative "
                "property."
            ),
            "100 Main St, Phoenix",
            [
                {
                    "name": "100 Main St brochure.pdf",
                    "text": "100 Main St - 20,000 SF.",
                },
                {
                    "name": "Oak Commerce Center brochure v2.pdf",
                    "text": "Oak Commerce Center - Ceiling Ht: 32 feet clear.",
                },
            ],
        )

        self.assertEqual([], result["updates"])
        self.assertTrue(any(
            event.get("type") == "needs_user_input"
            and event.get("reason") == "multi_property_attachment"
            for event in result["events"]
        ))
        self.assertIsNone(result["response_email"])

    def test_addressless_attachment_is_untrusted_with_multiple_attachments(self):
        introductions = (
            "As a fallback, Oak Commerce Center is attached.",
            "The backup is Oak Commerce Center.",
            "For comparison, Oak Commerce Center is attached.",
            "Plan B is Oak Commerce Center.",
            "Instead, consider Oak Commerce Center.",
        )

        for introduction in introductions:
            with self.subTest(introduction=introduction):
                proposal = {
                    "updates": [{"column": "Ceiling Ht", "value": "32"}],
                    "events": [{
                        "type": "needs_user_input",
                        "reason": "multi_property_attachment",
                        "question": "Which property is this for?",
                    }],
                    "response_email": "Thanks.",
                }

                result = ai_processing._suppress_competing_attachment_updates(
                    proposal,
                    _conversation(introduction),
                    "100 Main St, Phoenix",
                    [
                        {
                            "name": "100 Main St brochure.pdf",
                            "text": "100 Main St - 20,000 SF.",
                        },
                        {
                            "name": "Oak Commerce Center brochure.pdf",
                            "text": "Ceiling Ht: 32 feet clear.",
                        },
                    ],
                )

                self.assertEqual([], result["updates"])
                self.assertIsNone(result["response_email"])

    def test_generic_fresh_text_cannot_prove_competing_attachment_value(self):
        messages = (
            "This property has 28 feet clear. As a fallback.",
            "In contrast, Oak Commerce Center has 32 feet clear.",
        )

        for message in messages:
            with self.subTest(message=message):
                proposal = {
                    "updates": [{"column": "Ceiling Ht", "value": "32"}],
                    "events": [{
                        "type": "needs_user_input",
                        "reason": "multi_property_attachment",
                        "question": "Which property is this for?",
                    }],
                    "response_email": "Thanks.",
                }

                result = ai_processing._suppress_competing_attachment_updates(
                    proposal,
                    _conversation(message),
                    "100 Main St, Phoenix",
                    [
                        {
                            "name": "100 Main St brochure.pdf",
                            "text": "100 Main St - 20,000 SF.",
                        },
                        {
                            "name": "200 Oak Ave brochure.pdf",
                            "text": "200 Oak Ave - Ceiling Ht: 32 feet clear.",
                        },
                    ],
                )

                self.assertEqual([], result["updates"])
                self.assertIsNone(result["response_email"])

    def test_unbound_identity_clause_makes_single_target_address_pdf_mixed(self):
        alternate_clauses = (
            "oak commerce center - Ceiling Ht: 32 feet clear.",
            "Westgate Logistics Hub - Ceiling Ht: 32 feet clear.",
        )

        for alternate_clause in alternate_clauses:
            with self.subTest(alternate_clause=alternate_clause):
                proposal = {
                    "updates": [{"column": "Ceiling Ht", "value": "32"}],
                    "events": [],
                    "response_email": "Thanks.",
                }

                result = ai_processing._suppress_competing_attachment_updates(
                    proposal,
                    _conversation("The brochure is attached."),
                    "100 Main St, Phoenix",
                    [{
                        "name": "mixed brochure.pdf",
                        "text": (
                            "100 Main St - 20,000 SF. "
                            f"{alternate_clause}"
                        ),
                    }],
                )

                self.assertEqual([], result["updates"])
                self.assertTrue(any(
                    event.get("type") == "needs_user_input"
                    and event.get("reason") == "multi_property_attachment"
                    for event in result["events"]
                ))
                self.assertIsNone(result["response_email"])

    def test_two_word_unbound_identity_clause_makes_target_address_pdf_mixed(self):
        alternate_clauses = (
            "westgate hub - Ceiling Ht: 32 feet clear.",
            "oak center — Clear Height = 32 ft.",
            "river park - Ceiling Clearance: 32 feet.",
            "Oak Center - 32 feet clear.",
            "Westgate: 32 ft clear.",
            "Oak Center | 32 feet clear.",
            "Oak Center • 32 feet clear.",
            "Oak Center / 32 feet clear.",
            "Oak Center, 32 feet clear.",
            "Oak Center  32 feet clear.",
            "Oak Center\n32 feet clear.",
            "Oak Center\nCeiling Ht: 32 feet clear.",
        )

        for alternate_clause in alternate_clauses:
            with self.subTest(alternate_clause=alternate_clause):
                proposal = {
                    "updates": [{"column": "Ceiling Ht", "value": "32"}],
                    "events": [],
                    "response_email": "Thanks.",
                }

                result = ai_processing._suppress_competing_attachment_updates(
                    proposal,
                    _conversation("The brochure is attached."),
                    "100 Main St, Phoenix",
                    [{
                        "name": "mixed brochure.pdf",
                        "text": (
                            "100 Main St - 20,000 SF. "
                            f"{alternate_clause}"
                        ),
                    }],
                )

                self.assertEqual([], result["updates"])
                self.assertTrue(any(
                    event.get("type") == "needs_user_input"
                    and event.get("reason") == "multi_property_attachment"
                    for event in result["events"]
                ))
                self.assertIsNone(result["response_email"])

    def test_two_word_target_spec_label_remains_supported(self):
        target_specs = (
            "clear height - 28 feet clear.",
            "ceiling height - 28 feet clear.",
            "ceiling ht: 28 feet clear.",
            "ceiling clearance = 28 ft clear.",
            "clear height\n28 feet clear.",
            "Property Highlights\n28 feet clear.",
        )

        for target_spec in target_specs:
            with self.subTest(target_spec=target_spec):
                expected_update = {"column": "Ceiling Ht", "value": "28"}
                proposal = {
                    "updates": [expected_update],
                    "events": [],
                    "response_email": None,
                }

                result = ai_processing._suppress_competing_attachment_updates(
                    proposal,
                    _conversation("The target brochure is attached."),
                    "100 Main St, Phoenix",
                    [{
                        "name": "100 Main St brochure.pdf",
                        "text": f"100 Main St - 20,000 SF. {target_spec}",
                    }],
                )

                self.assertEqual([expected_update], result["updates"])

    def test_short_unbound_identity_suppresses_every_mapped_fact_type(self):
        competing_facts = (
            ("Docks", "6", "Oak Center - 6 dock doors."),
            ("Docks", "6", "Oak Center | Docks: 6."),
            ("Docks", "6", "Oak Center | Dock Positions: 6."),
            ("Docks", "6", "Oak Center) Docks: 6."),
            ("Docks", "6", "Oak Center] Docks: 6."),
            ("Docks", "6", "Oak Center} Docks: 6."),
            ("Docks", "6", "Oak Center ~ Docks: 6."),
            ("Drive Ins", "2", "Oak Center - 2 drive-in doors."),
            ("Drive Ins", "2", "Oak Center | Drive Ins: 2."),
            ("Power", "1200A 480V 3-phase", "Oak Center - 1200A 480V 3-phase power."),
            ("Power", "1200A 480V 3-phase", "Oak Center | Power: 1200A 480V 3-phase."),
            ("Rent/SF/Yr", "6.75", "Oak Center - $6.75/SF NNN."),
            ("Rent/SF/Yr", "6.75", "Oak Center | Rent/SF/Yr: $6.75 NNN."),
            ("Ops Ex/SF/Yr", "1.85", "Oak Center - $1.85/SF operating expenses."),
            ("Ops Ex/SF/Yr", "1.85", "Oak Center | Op Ex: $1.85/SF."),
        )

        for column, value, competing_clause in competing_facts:
            with self.subTest(column=column, competing_clause=competing_clause):
                proposal = {
                    "updates": [{"column": column, "value": value}],
                    "events": [],
                    "response_email": "Thanks.",
                }

                result = ai_processing._suppress_competing_attachment_updates(
                    proposal,
                    _conversation("The brochure is attached."),
                    "100 Main St, Phoenix",
                    [{
                        "name": "mixed brochure.pdf",
                        "text": f"100 Main St - 20,000 SF. {competing_clause}",
                    }],
                )

                self.assertEqual([], result["updates"])
                self.assertTrue(any(
                    event.get("type") == "needs_user_input"
                    and event.get("reason") == "multi_property_attachment"
                    for event in result["events"]
                ))
                self.assertIsNone(result["response_email"])

    def test_postfixed_unbound_identity_suppresses_every_mapped_fact_type(self):
        competing_facts = (
            ("Docks", "6", "Docks: 6 | Oak Center"),
            ("Docks", "6", "6 Dock Doors | Oak Center"),
            ("Docks", "6", "Docks: 6. Oak Center"),
            ("Drive Ins", "2", "Grade Level Doors: 2 — Oak Center"),
            ("Power", "1200A", "Electrical Capacity: 1200A • Oak Center"),
            ("Power", "1200A", "Power: 1200A\tOak Center"),
            ("Rent/SF/Yr", "6.75", "Asking Rate: $6.75 / Oak Center"),
            ("Ops Ex/SF/Yr", "1.85", "CAM Charges: $1.85, Oak Center"),
            ("Ceiling Ht", "32", "Clear Height: 32; Oak Center"),
            ("Total SF", "45000", "Available Sq Ft: 45,000\nOak Center"),
        )

        for column, value, competing_clause in competing_facts:
            with self.subTest(column=column, competing_clause=competing_clause):
                proposal = {
                    "updates": [{"column": column, "value": value}],
                    "events": [],
                    "response_email": "Thanks.",
                }

                result = ai_processing._suppress_competing_attachment_updates(
                    proposal,
                    _conversation("The brochure is attached."),
                    "100 Main St, Phoenix",
                    [{
                        "name": "mixed brochure.pdf",
                        "text": (
                            "100 Main St - 20,000 SF. "
                            f"{competing_clause}."
                        ),
                    }],
                )

                self.assertEqual([], result["updates"])
                self.assertTrue(any(
                    event.get("type") == "needs_user_input"
                    and event.get("reason") == "multi_property_attachment"
                    for event in result["events"]
                ))
                self.assertIsNone(result["response_email"])

    def test_postfixed_identity_after_same_fragment_target_fails_closed(self):
        mixed_fragments = (
            "100 Main St | 20,000 SF | Docks: 6 | Oak Center",
            "100 Main St - 20,000 SF, Docks: 6 | Oak Center",
            "100 Main St — 20,000 SF — Docks: 6 — Oak Center",
        )

        for mixed_fragment in mixed_fragments:
            with self.subTest(mixed_fragment=mixed_fragment):
                result = ai_processing._suppress_competing_attachment_updates(
                    {
                        "updates": [{"column": "Docks", "value": "6"}],
                        "events": [],
                        "response_email": "Thanks.",
                    },
                    _conversation("The brochure is attached."),
                    "100 Main St, Phoenix",
                    [{
                        "name": "mixed brochure.pdf",
                        "text": f"{mixed_fragment}.",
                    }],
                )

                self.assertEqual([], result["updates"])
                self.assertTrue(any(
                    event.get("type") == "needs_user_input"
                    and event.get("reason") == "multi_property_attachment"
                    for event in result["events"]
                ))
                self.assertIsNone(result["response_email"])

    def test_three_row_postfixed_identity_suppresses_every_mapped_fact_type(self):
        competing_tables = (
            ("Docks", "6", "Docks\n6\nOak Center"),
            ("Drive Ins", "2", "Drive Ins\n2\nOak Center"),
            ("Power", "1200A", "Power\n1200A\nOak Center"),
            ("Ceiling Ht", "32", "Clear Height\n32\nOak Center"),
            ("Total SF", "45000", "Total SF\n45000\nOak Center"),
            ("Rent/SF/Yr", "6.75", "Asking Rate\n$6.75/SF NNN\nOak Center"),
            ("Ops Ex/SF/Yr", "1.85", "CAM Charges\n$1.85/SF\nOak Center"),
            ("Docks", "6", "Docks\n6\nPower Center"),
            ("Power", "1200A", "Power\n1200A\nWestgate Power"),
        )

        for column, value, competing_table in competing_tables:
            with self.subTest(column=column, competing_table=competing_table):
                result = ai_processing._suppress_competing_attachment_updates(
                    {
                        "updates": [{"column": column, "value": value}],
                        "events": [],
                        "response_email": "Thanks.",
                    },
                    _conversation("The brochure is attached."),
                    "100 Main St, Phoenix",
                    [{
                        "name": "mixed brochure.pdf",
                        "text": f"100 Main St\n{competing_table}",
                    }],
                )

                self.assertEqual([], result["updates"])
                self.assertTrue(any(
                    event.get("type") == "needs_user_input"
                    and event.get("reason") == "multi_property_attachment"
                    for event in result["events"]
                ))
                self.assertIsNone(result["response_email"])

    def test_three_row_target_tables_remain_supported(self):
        target_tables = (
            ("Docks", "6", "Docks\n6"),
            ("Docks", "6", "Docks\n6\n100 Main St"),
            ("Power", "1200A 480V 3-phase", "Power\n1200A 480V 3-phase"),
            ("Rent/SF/Yr", "6.75", "Asking Rate\n$6.75/SF NNN"),
        )

        for column, value, target_table in target_tables:
            with self.subTest(column=column, target_table=target_table):
                expected_update = {"column": column, "value": value}
                result = ai_processing._suppress_competing_attachment_updates(
                    {
                        "updates": [expected_update],
                        "events": [],
                        "response_email": "Thanks.",
                    },
                    _conversation("The target brochure is attached."),
                    "100 Main St, Phoenix",
                    [{
                        "name": "100 Main St brochure.pdf",
                        "text": f"100 Main St\n{target_table}",
                    }],
                )

                self.assertEqual([expected_update], result["updates"])
                self.assertEqual([], result["events"])
                self.assertEqual("Thanks.", result["response_email"])

    def test_multi_column_postfixed_identity_tables_fail_closed(self):
        competing_tables = (
            (
                "Docks | Power\n"
                "6 | 1200A 480V 3-phase\n"
                "Oak Center | Westgate Logistics Hub"
            ),
            (
                "| Docks | Power |\n"
                "| 6 | 1200A 480V 3-phase |\n"
                "| Oak Center | Westgate Logistics Hub |"
            ),
            (
                "Docks, Power\n"
                "6, 1200A 480V 3-phase\n"
                "Oak Center, Westgate Logistics Hub"
            ),
            (
                "Docks\tPower\n"
                "6\t1200A 480V 3-phase\n"
                "Oak Center\tWestgate Logistics Hub"
            ),
            (
                "Power | Docks\n"
                "1200A 480V 3-phase | 6\n"
                "Westgate Logistics Hub | Oak Center"
            ),
            (
                "Docks | Power\n"
                "6 | 1200A\n"
                "Power Center | Westgate Power"
            ),
            (
                "Docks | Power\n"
                "6 | 1200A 480V 3-phase\n"
                "200 Oak Ave | 300 Pine Rd"
            ),
            (
                "Docks | Power\n"
                "6 | 1200A 480V 3-phase\n"
                "100 Main St / Oak Center | Westgate Logistics Hub"
            ),
        )

        for competing_table in competing_tables:
            with self.subTest(competing_table=competing_table):
                result = ai_processing._suppress_competing_attachment_updates(
                    {
                        "updates": [
                            {"column": "Docks", "value": "6"},
                            {"column": "Power", "value": "1200A 480V 3-phase"},
                        ],
                        "events": [],
                        "response_email": "Thanks.",
                    },
                    _conversation("The brochure is attached."),
                    "100 Main St, Phoenix",
                    [{
                        "name": "mixed brochure.pdf",
                        "text": f"100 Main St\n{competing_table}",
                    }],
                )

                self.assertEqual([], result["updates"])
                self.assertTrue(any(
                    event.get("type") == "needs_user_input"
                    and event.get("reason") == "multi_property_attachment"
                    for event in result["events"]
                ))
                self.assertIsNone(result["response_email"])

    def test_multi_column_mixed_target_cells_keep_only_target_updates(self):
        mixed_tables = (
            (
                "Docks | Power\n"
                "6 | 1200A 480V 3-phase\n"
                "100 Main St | Oak Center",
                {"column": "Docks", "value": "6"},
                {"column": "Power", "value": "1200A 480V 3-phase"},
            ),
            (
                "Power | Docks\n"
                "1200A 480V 3-phase | 6\n"
                "Oak Center | 100 Main St",
                {"column": "Docks", "value": "6"},
                {"column": "Power", "value": "1200A 480V 3-phase"},
            ),
            (
                "Drive Ins | Power\n"
                "2 | 1200A\n"
                "100 Main St | Oak Center",
                {"column": "Drive Ins", "value": "2"},
                {"column": "Power", "value": "1200A"},
            ),
            (
                "Docks | Power\n"
                "6 | 1200A 480V 3-phase\n"
                "Oak Center | 100 Main St",
                {"column": "Power", "value": "1200A 480V 3-phase"},
                {"column": "Docks", "value": "6"},
            ),
            (
                "Clear Height | Docks\n"
                "32 | 6\n"
                "100 Main St | Oak Center",
                {"column": "Ceiling Ht", "value": "32"},
                {"column": "Docks", "value": "6"},
            ),
            (
                "Total SF | Docks\n"
                "45,000 | 6\n"
                "100 Main St | Oak Center",
                {"column": "Total SF", "value": "45000"},
                {"column": "Docks", "value": "6"},
            ),
            (
                "Asking Rate | Docks\n"
                "$6.75/SF NNN | 6\n"
                "100 Main St | Oak Center",
                {"column": "Rent/SF/Yr", "value": "6.75"},
                {"column": "Docks", "value": "6"},
            ),
            (
                "CAM Charges | Docks\n"
                "$1.85/SF | 6\n"
                "100 Main St | Oak Center",
                {"column": "Ops Ex/SF/Yr", "value": "1.85"},
                {"column": "Docks", "value": "6"},
            ),
            (
                "Docks | Power\n"
                "6 | 1200A 480V 3-phase\n"
                "100 Main St | 200 Oak Ave",
                {"column": "Docks", "value": "6"},
                {"column": "Power", "value": "1200A 480V 3-phase"},
            ),
        )

        for mixed_table, expected_update, competing_update in mixed_tables:
            with self.subTest(mixed_table=mixed_table, expected_update=expected_update):
                result = ai_processing._suppress_competing_attachment_updates(
                    {
                        "updates": [expected_update, competing_update],
                        "events": [],
                        "response_email": "Thanks.",
                    },
                    _conversation("The brochure is attached."),
                    "100 Main St, Phoenix",
                    [{
                        "name": "mixed brochure.pdf",
                        "text": f"100 Main St\n{mixed_table}",
                    }],
                )

                self.assertEqual([expected_update], result["updates"])
                self.assertTrue(any(
                    event.get("type") == "needs_user_input"
                    and event.get("reason") == "multi_property_attachment"
                    for event in result["events"]
                ))
                self.assertIsNone(result["response_email"])

    def test_comma_table_address_cells_keep_column_alignment(self):
        docks = {"column": "Docks", "value": "6"}
        power = {"column": "Power", "value": "1200A 480V 3-phase"}
        total_sf = {"column": "Total SF", "value": "45000"}
        drive_ins = {"column": "Drive Ins", "value": "2"}
        cases = (
            (
                "target city first",
                "100 Main St, Phoenix",
                "Docks, Power\n"
                "6, 1200A 480V 3-phase\n"
                "100 Main St, Phoenix, Oak Center",
                [docks, power],
                [docks],
            ),
            (
                "target city last",
                "100 Main St, Phoenix",
                "Power, Docks\n"
                "1200A 480V 3-phase, 6\n"
                "Oak Center, 100 Main St, Phoenix",
                [power, docks],
                [docks],
            ),
            (
                "target city state zip in middle column",
                "100 Main St, Phoenix, AZ 85001",
                "Docks, Power, Drive Ins\n"
                "6, 1200A 480V 3-phase, 2\n"
                "Oak Center, 100 Main St, Phoenix, AZ 85001, Westgate",
                [docks, power, drive_ins],
                [power],
            ),
            (
                "short identity row with target city",
                "100 Main St, Phoenix",
                "Docks, Power, Drive Ins\n"
                "6, 1200A 480V 3-phase, 2\n"
                "100 Main St, Phoenix, Oak Center",
                [docks, power, drive_ins],
                [docks],
            ),
            (
                "target and competitor addresses with cities",
                "100 Main St, Phoenix",
                "Docks, Power\n"
                "6, 1200A 480V 3-phase\n"
                "100 Main St, Phoenix, 200 Oak Ave, Tempe",
                [docks, power],
                [docks],
            ),
            (
                "quoted address and unknown cells",
                "100 Main St, Phoenix",
                "Docks, Power\n"
                "6, 1200A 480V 3-phase\n"
                "\"100 Main St, Phoenix\", \"Oak Center\"",
                [docks, power],
                [docks],
            ),
            (
                "compact quoted csv cells",
                "100 Main St, Phoenix",
                "Docks, Power\n"
                "6, 1200A 480V 3-phase\n"
                "\"100 Main St,Phoenix\",\"Oak Center\"",
                [docks, power],
                [docks],
            ),
            (
                "fully quoted csv rows",
                "100 Main St, Phoenix",
                "\"Docks\", \"Power\"\n"
                "\"6\", \"1200A 480V 3-phase\"\n"
                "\"100 Main St, Phoenix\", \"Oak Center\"",
                [docks, power],
                [docks],
            ),
            (
                "mixed quoting and spaces",
                "100 Main St, Phoenix",
                "  \"Docks\"  ,   Power  \n"
                "  \"6\"  ,   1200A 480V 3-phase  \n"
                "  \"100 Main St, Phoenix\"  ,   Oak Center  ",
                [docks, power],
                [docks],
            ),
            (
                "escaped quotes in labels and identities",
                "100 Main St, Phoenix",
                "\"Dock \"\"Doors\"\"\", \"Power\"\n"
                "\"6\", \"1200A 480V 3-phase\"\n"
                "\"100 Main St, Phoenix\", \"Oak \"\"Power\"\" Center\"",
                [docks, power],
                [docks],
            ),
            (
                "fully quoted numeric comma",
                "100 Main St, Phoenix",
                "\"Total SF\",\"Docks\"\n"
                "\"45,000\",\"6\"\n"
                "\"100 Main St,Phoenix\",\"Oak Center\"",
                [total_sf, docks],
                [total_sf],
            ),
            (
                "numeric and address commas",
                "100 Main St, Phoenix",
                "Total SF, Docks\n"
                "45,000, 6\n"
                "100 Main St, Phoenix, Oak Center",
                [total_sf, docks],
                [total_sf],
            ),
        )

        for name, target_anchor, table, updates, expected_updates in cases:
            with self.subTest(name=name):
                result = ai_processing._suppress_competing_attachment_updates(
                    {
                        "updates": list(updates),
                        "events": [],
                        "response_email": "Thanks.",
                    },
                    _conversation("The brochure is attached."),
                    target_anchor,
                    [{
                        "name": "mixed brochure.pdf",
                        "text": f"{target_anchor}\n{table}",
                    }],
                )

                self.assertEqual(expected_updates, result["updates"])
                self.assertTrue(any(
                    event.get("type") == "needs_user_input"
                    and event.get("reason") == "multi_property_attachment"
                    for event in result["events"]
                ))
                self.assertIsNone(result["response_email"])

    def test_malformed_multi_column_table_shapes_fail_closed(self):
        updates = [
            {"column": "Docks", "value": "6"},
            {"column": "Power", "value": "1200A 480V 3-phase"},
            {"column": "Drive Ins", "value": "2"},
        ]
        malformed_tables = (
            (
                "quoted short identity row",
                "\"Docks\",\"Power\",\"Drive Ins\"\n"
                "\"6\",\"1200A 480V 3-phase\",\"2\"\n"
                "\"100 Main St, Phoenix\",\"Oak Center\"",
            ),
            (
                "unquoted short identity row",
                "Docks, Power, Drive Ins\n"
                "6, 1200A 480V 3-phase, 2\n"
                "100 Main St, Oak Center",
            ),
            (
                "pipe extra label",
                "Docks | Power | Drive Ins\n"
                "6 | 1200A 480V 3-phase\n"
                "100 Main St | Oak Center",
            ),
            (
                "tab missing label",
                "Docks\tDrive Ins\n"
                "6\t1200A 480V 3-phase\t2\n"
                "100 Main St\tOak Center\tWestgate",
            ),
            (
                "quoted missing trailing label",
                "\"Docks\",\n"
                "\"6\",\"1200A 480V 3-phase\"\n"
                "\"100 Main St\",\"Oak Center\"",
            ),
            (
                "quoted missing value cell",
                "\"Docks\",\"Power\",\"Drive Ins\"\n"
                "\"6\",,\"2\"\n"
                "\"100 Main St\",\"Oak Center\",\"Westgate\"",
            ),
            (
                "unquoted extra value",
                "Docks, Power\n"
                "6, 1200A 480V 3-phase, 2\n"
                "100 Main St, Oak Center",
            ),
            (
                "quoted two-row extra value",
                "\"Docks\",\"Power\"\n"
                "\"6\",\"1200A 480V 3-phase\",\"2\"",
            ),
            (
                "pipe missing identity cell",
                "Docks | Power | Drive Ins\n"
                "6 | 1200A 480V 3-phase | 2\n"
                "100 Main St | | Oak Center",
            ),
            (
                "tab extra identity",
                "Docks\tPower\n"
                "6\t1200A 480V 3-phase\n"
                "100 Main St\tOak Center\tWestgate",
            ),
            (
                "unquoted extra all-target identity",
                "Docks, Power\n"
                "6, 1200A 480V 3-phase\n"
                "100 Main St, 100 Main St, 100 Main St",
            ),
            (
                "quoted extra all-target identity",
                "\"Docks\",\"Power\"\n"
                "\"6\",\"1200A 480V 3-phase\"\n"
                "\"100 Main St\",\"100 Main St\",\"100 Main St\"",
            ),
            (
                "pipe extra all-target identity",
                "Docks | Power\n"
                "6 | 1200A 480V 3-phase\n"
                "100 Main St | 100 Main St | 100 Main St",
            ),
            (
                "tab extra all-target identity",
                "Docks\tPower\n"
                "6\t1200A 480V 3-phase\n"
                "100 Main St\t100 Main St\t100 Main St",
            ),
        )

        for name, malformed_table in malformed_tables:
            with self.subTest(name=name):
                result = ai_processing._suppress_competing_attachment_updates(
                    {
                        "updates": list(updates),
                        "events": [],
                        "response_email": "Thanks.",
                    },
                    _conversation("The brochure is attached."),
                    "100 Main St, Phoenix",
                    [{
                        "name": "mixed brochure.pdf",
                        "text": f"100 Main St, Phoenix\n{malformed_table}",
                    }],
                )

                self.assertEqual([], result["updates"])
                self.assertTrue(any(
                    event.get("type") == "needs_user_input"
                    and event.get("reason") == "multi_property_attachment"
                    for event in result["events"]
                ))
                self.assertIsNone(result["response_email"])

    def test_balanced_three_column_table_shapes_remain_supported(self):
        updates = [
            {"column": "Docks", "value": "6"},
            {"column": "Power", "value": "1200A 480V 3-phase"},
            {"column": "Drive Ins", "value": "2"},
        ]
        target_tables = (
            (
                "Docks, Power, Drive Ins\n"
                "6, 1200A 480V 3-phase, 2\n"
                "100 Main St, 100 Main St, 100 Main St"
            ),
            (
                "\"Docks\",\"Power\",\"Drive Ins\"\n"
                "\"6\",\"1200A 480V 3-phase\",\"2\"\n"
                "\"100 Main St, Phoenix\",\"100 Main St, Phoenix\","
                "\"100 Main St, Phoenix\""
            ),
            (
                "| Docks | Power | Drive Ins |\n"
                "| 6 | 1200A 480V 3-phase | 2 |\n"
                "| 100 Main St, Phoenix | 100 Main St, Phoenix | "
                "100 Main St, Phoenix |"
            ),
            (
                "Docks\tPower\tDrive Ins\n"
                "6\t1200A 480V 3-phase\t2\n"
                "100 Main St, Phoenix\t100 Main St, Phoenix\t"
                "100 Main St, Phoenix"
            ),
        )

        for target_table in target_tables:
            with self.subTest(target_table=target_table):
                result = ai_processing._suppress_competing_attachment_updates(
                    {
                        "updates": list(updates),
                        "events": [],
                        "response_email": "Thanks.",
                    },
                    _conversation("The brochure is attached."),
                    "100 Main St, Phoenix",
                    [{
                        "name": "100 Main St brochure.pdf",
                        "text": f"100 Main St, Phoenix\n{target_table}",
                    }],
                )

                self.assertEqual(updates, result["updates"])
                self.assertEqual([], result["events"])
                self.assertEqual("Thanks.", result["response_email"])

    def test_markdown_separator_rows_keep_property_cells_aligned(self):
        docks = {"column": "Docks", "value": "6"}
        power = {"column": "Power", "value": "1200A 480V 3-phase"}
        drive_ins = {"column": "Drive Ins", "value": "2"}
        cases = (
            (
                "bordered separator",
                "| Docks | Power |\n"
                "| --- | --- |\n"
                "| 6 | 1200A 480V 3-phase |\n"
                "| 100 Main St | Oak Center |",
                [docks, power],
                [docks],
                True,
            ),
            (
                "unbordered aligned separator",
                "Power | Docks\n"
                ":--- | ---:\n"
                "1200A 480V 3-phase | 6\n"
                "Oak Center | 100 Main St",
                [power, docks],
                [docks],
                True,
            ),
            (
                "centered separator with whitespace",
                "  |  Docks  |  Power  |  \n"
                "  |  :---:  |  ---:  |  \n"
                "  |  6  |  1200A 480V 3-phase  |  \n"
                "  |  100 Main St, Phoenix  |  Oak Center  |  ",
                [docks, power],
                [docks],
                True,
            ),
            (
                "separator has an extra cell",
                "| Docks | Power |\n"
                "| --- | --- | --- |\n"
                "| 6 | 1200A 480V 3-phase |\n"
                "| 100 Main St | Oak Center |",
                [docks, power],
                [docks],
                True,
            ),
            (
                "separator before malformed short identity row",
                "| Docks | Power | Drive Ins |\n"
                "| --- | --- |\n"
                "| 6 | 1200A 480V 3-phase | 2 |\n"
                "| 100 Main St | Oak Center |",
                [docks, power, drive_ins],
                [],
                True,
            ),
            (
                "separator before malformed value row",
                "| Docks | Power | Drive Ins |\n"
                "| :--- | :---: | ---: |\n"
                "| 6 | 2 |\n"
                "| 100 Main St | Oak Center | Westgate |",
                [docks, power, drive_ins],
                [],
                True,
            ),
            (
                "balanced all-target separator control",
                "| Docks | Power |\n"
                "| --- | --- |\n"
                "| 6 | 1200A 480V 3-phase |\n"
                "| 100 Main St | 100 Main St |",
                [docks, power],
                [docks, power],
                False,
            ),
            (
                "no-separator control",
                "| Docks | Power |\n"
                "| 6 | 1200A 480V 3-phase |\n"
                "| 100 Main St | Oak Center |",
                [docks, power],
                [docks],
                True,
            ),
        )

        for name, table, updates, expected_updates, escalated in cases:
            with self.subTest(name=name):
                result = ai_processing._suppress_competing_attachment_updates(
                    {
                        "updates": list(updates),
                        "events": [],
                        "response_email": "Thanks.",
                    },
                    _conversation("The brochure is attached."),
                    "100 Main St, Phoenix",
                    [{
                        "name": "mixed brochure.pdf",
                        "text": f"100 Main St, Phoenix\n{table}",
                    }],
                )

                self.assertEqual(expected_updates, result["updates"])
                self.assertEqual(
                    escalated,
                    any(
                        event.get("type") == "needs_user_input"
                        and event.get("reason") == "multi_property_attachment"
                        for event in result["events"]
                    ),
                )
                self.assertEqual(
                    None if escalated else "Thanks.",
                    result["response_email"],
                )

    def test_numbered_document_captions_do_not_create_property_boundaries(self):
        captions = (
            "Table 1: Building Facts",
            "Figure 2 - Building Overview",
            "Page 3 of 12",
            "Section 4.2: Loading Details",
            "Schedule 5 — Property Facts",
            "Exhibit 6: Building Facts",
            "Version 7: Building Facts",
            "Revision 8 - Building Facts",
            "Table IV: Property Summary",
            "Figure A-1 — Building Facts",
            "Exhibit B.2 (Property Summary)",
            "Schedule 1-A: Property Summary",
            "Table (1): Building Facts",
            "fIgUrE (IV) — Property Summary",
            "Section (4.2): Loading Details",
            "Schedule (A-1): Property Summary",
            "Exhibit [B.2] (Property Summary)",
            "VERSION [7A] - Building Facts",
            "Figure I",
            "Figure I Building Facts",
            "Table: Building Facts",
        )
        expected_updates = [
            {"column": "Docks", "value": "6"},
            {"column": "Power", "value": "1200A 480V 3-phase"},
        ]
        target_tables = (
            (
                "markdown",
                "| Docks | Power |\n"
                "| --- | --- |\n"
                "| 6 | 1200A 480V 3-phase |\n"
                "| 100 Main St | 100 Main St |",
            ),
            (
                "csv",
                "Docks, Power\n"
                "6, 1200A 480V 3-phase\n"
                "100 Main St, 100 Main St",
            ),
        )

        for caption in captions:
            for table_format, target_table in target_tables:
                with self.subTest(caption=caption, table_format=table_format):
                    result = ai_processing._suppress_competing_attachment_updates(
                        {
                            "updates": list(expected_updates),
                            "events": [],
                            "response_email": "Thanks.",
                        },
                        _conversation("The brochure is attached."),
                        "100 Main St, Phoenix",
                        [{
                            "name": "100 Main St brochure.pdf",
                            "text": (
                                f"100 Main St, Phoenix\n{caption}\n{target_table}"
                            ),
                        }],
                    )

                    self.assertEqual(expected_updates, result["updates"])
                    self.assertEqual([], result["events"])
                    self.assertEqual("Thanks.", result["response_email"])

    def test_structural_caption_lines_do_not_bind_neighboring_facts(self):
        fragments = (
            "Figure I\nDocks: 6",
            "Docks: 6\nFigure I",
            "Figure I Building Facts\nDocks: 6",
            "Docks: 6\nFigure I Building Facts",
            "Figure (I)\nDocks: 6",
            "Docks: 6\nFigure [I]: Building Facts",
            "Docks\nFigure I\n6\n100 Main St",
        )

        for fragment in fragments:
            with self.subTest(fragment=fragment):
                result = ai_processing._suppress_competing_attachment_updates(
                    {
                        "updates": [{"column": "Docks", "value": "6"}],
                        "events": [],
                        "response_email": "Thanks.",
                    },
                    _conversation("The brochure is attached."),
                    "100 Main St, Phoenix",
                    [{
                        "name": "100 Main St brochure.pdf",
                        "text": f"100 Main St, Phoenix\n{fragment}",
                    }],
                )

                self.assertEqual(
                    [{"column": "Docks", "value": "6"}],
                    result["updates"],
                )
                self.assertEqual([], result["events"])
                self.assertEqual("Thanks.", result["response_email"])

    def test_caption_property_residuals_fail_closed_next_to_facts(self):
        fragments = (
            "Figure I Oak Center\nDocks: 6",
            "Docks: 6\nFigure I Oak Center",
            "Figure (I): Oak Center\nDocks: 6",
            "Docks: 6\nFigure [I]: Oak Center",
            "Oak Center\nFigure I\nDocks: 6",
            "Docks: 6\nFigure I\nOak Center",
        )

        for fragment in fragments:
            with self.subTest(fragment=fragment):
                result = ai_processing._suppress_competing_attachment_updates(
                    {
                        "updates": [{"column": "Docks", "value": "6"}],
                        "events": [],
                        "response_email": "Thanks.",
                    },
                    _conversation("The brochure is attached."),
                    "100 Main St, Phoenix",
                    [{
                        "name": "mixed brochure.pdf",
                        "text": f"100 Main St, Phoenix\n{fragment}",
                    }],
                )

                self.assertEqual([], result["updates"])
                self.assertTrue(any(
                    event.get("type") == "needs_user_input"
                    and event.get("reason") == "multi_property_attachment"
                    for event in result["events"]
                ))
                self.assertIsNone(result["response_email"])

    def test_numbered_captions_preserve_mixed_table_competitor_detection(self):
        docks = {"column": "Docks", "value": "6"}
        power = {"column": "Power", "value": "1200A 480V 3-phase"}
        cases = (
            (
                "Figure 10: Building Facts",
                "| Docks | Power |\n"
                "| :--- | ---: |\n"
                "| 6 | 1200A 480V 3-phase |\n"
                "| 100 Main St | Oak Center |",
            ),
            (
                "Section 11: Property Summary",
                "Docks, Power\n"
                "6, 1200A 480V 3-phase\n"
                "100 Main St, Oak Center",
            ),
        )

        for caption, table in cases:
            with self.subTest(caption=caption):
                result = ai_processing._suppress_competing_attachment_updates(
                    {
                        "updates": [docks, power],
                        "events": [],
                        "response_email": "Thanks.",
                    },
                    _conversation("The brochure is attached."),
                    "100 Main St, Phoenix",
                    [{
                        "name": "mixed brochure.pdf",
                        "text": f"100 Main St, Phoenix\n{caption}\n{table}",
                    }],
                )

                self.assertEqual([docks], result["updates"])
                self.assertTrue(any(
                    event.get("type") == "needs_user_input"
                    and event.get("reason") == "multi_property_attachment"
                    for event in result["events"]
                ))
                self.assertIsNone(result["response_email"])

    def test_structural_caption_rows_do_not_break_mixed_table_alignment(self):
        docks = {"column": "Docks", "value": "6"}
        power = {"column": "Power", "value": "1200A 480V 3-phase"}
        result = ai_processing._suppress_competing_attachment_updates(
            {
                "updates": [docks, power],
                "events": [],
                "response_email": "Thanks.",
            },
            _conversation("The brochure is attached."),
            "100 Main St, Phoenix",
            [{
                "name": "mixed brochure.pdf",
                "text": (
                    "100 Main St, Phoenix\n"
                    "| Docks | Power |\n"
                    "Figure I\n"
                    "| 6 | 1200A 480V 3-phase |\n"
                    "| 100 Main St | Oak Center |"
                ),
            }],
        )

        self.assertEqual([docks], result["updates"])
        self.assertTrue(any(
            event.get("type") == "needs_user_input"
            and event.get("reason") == "multi_property_attachment"
            for event in result["events"]
        ))
        self.assertIsNone(result["response_email"])

    def test_one_cell_caption_rows_do_not_break_target_table_alignment(self):
        caption_rows = (
            "Figure I",
            "| Figure I |",
            "|| Figure I ||",
            '| "Figure I" |',
            '"| Figure I |"',
            "“Figure I”",
        )
        table_formats = (
            "{caption}\n| Docks |\n| --- |\n| 6 |\n| 100 Main St |",
            "| Docks |\n{caption}\n| --- |\n| 6 |\n| 100 Main St |",
            "| Docks |\n| --- |\n| 6 |\n{caption}\n| 100 Main St |",
            "| Docks |\n| --- |\n| 6 |\n| 100 Main St |\n{caption}",
        )

        for caption in caption_rows:
            for table_format in table_formats:
                with self.subTest(caption=caption, table_format=table_format):
                    result = ai_processing._suppress_competing_attachment_updates(
                        {
                            "updates": [{"column": "Docks", "value": "6"}],
                            "events": [],
                            "response_email": "Thanks.",
                        },
                        _conversation("The brochure is attached."),
                        "100 Main St, Phoenix",
                        [{
                            "name": "100 Main St brochure.pdf",
                            "text": (
                                "100 Main St, Phoenix\n"
                                f"{table_format.format(caption=caption)}"
                            ),
                        }],
                    )

                    self.assertEqual(
                        [{"column": "Docks", "value": "6"}],
                        result["updates"],
                    )
                    self.assertEqual([], result["events"])
                    self.assertEqual("Thanks.", result["response_email"])

    def test_one_cell_unsafe_caption_rows_still_fail_closed(self):
        caption_rows = (
            "| Figure I Oak Center |",
            '| "Figure I Oak Center" |',
            '"| Figure I Oak Center |"',
            "| Figure (I]: Building Facts |",
            "| Figure ((I)): Building Facts |",
        )

        for caption in caption_rows:
            with self.subTest(caption=caption):
                result = ai_processing._suppress_competing_attachment_updates(
                    {
                        "updates": [{"column": "Docks", "value": "6"}],
                        "events": [],
                        "response_email": "Thanks.",
                    },
                    _conversation("The brochure is attached."),
                    "100 Main St, Phoenix",
                    [{
                        "name": "mixed brochure.pdf",
                        "text": f"100 Main St, Phoenix\n{caption}\nDocks: 6",
                    }],
                )

                self.assertEqual([], result["updates"])
                self.assertTrue(any(
                    event.get("type") == "needs_user_input"
                    and event.get("reason") == "multi_property_attachment"
                    for event in result["events"]
                ))
                self.assertIsNone(result["response_email"])

    def test_balanced_escaped_caption_quotes_follow_caption_verdict(self):
        cases = (
            (r'| \"Figure I\" |', "structural"),
            (r'| \"Figure I Building Facts\" |', "structural"),
            (r'| \"Figure I Oak Center\" |', "competing"),
            (r'| \"Figure (I]: Building Facts\" |', "competing"),
        )

        for caption, expected_verdict in cases:
            with self.subTest(caption=caption):
                self.assertEqual(
                    expected_verdict,
                    ai_processing._document_caption_verdict(caption),
                )
                escalated = expected_verdict == "competing"
                result = ai_processing._suppress_competing_attachment_updates(
                    {
                        "updates": [{"column": "Docks", "value": "6"}],
                        "events": [],
                        "response_email": "Thanks.",
                    },
                    _conversation("The brochure is attached."),
                    "100 Main St, Phoenix",
                    [{
                        "name": "100 Main St brochure.pdf",
                        "text": (
                            "100 Main St, Phoenix\n"
                            "| Docks |\n"
                            "| --- |\n"
                            "| 6 |\n"
                            f"{caption}\n"
                            "| 100 Main St |"
                        ),
                    }],
                )

                self.assertEqual(
                    [] if escalated else [{"column": "Docks", "value": "6"}],
                    result["updates"],
                )
                self.assertEqual(
                    escalated,
                    any(
                        event.get("type") == "needs_user_input"
                        and event.get("reason") == "multi_property_attachment"
                        for event in result["events"]
                    ),
                )
                self.assertEqual(
                    None if escalated else "Thanks.",
                    result["response_email"],
                )

    def test_unbalanced_caption_quotes_fail_closed_across_positions(self):
        caption_rows = (
            '| "Figure I |',
            '| Figure I" |',
            "| “Figure I Building Facts |",
            "| Figure I Building Facts” |",
            "| 'Figure I Oak Center’ |",
            r'| \"Figure I Building Facts |',
            r'| Figure I Oak Center\" |',
            r'| \"Figure (I]: Building Facts" |',
        )
        fragment_formats = (
            "{caption}\nDocks: 6",
            "Docks: 6\n{caption}",
            "| Docks |\n| --- |\n| 6 |\n{caption}\n| 100 Main St |",
        )

        for caption in caption_rows:
            self.assertEqual(
                "competing",
                ai_processing._document_caption_verdict(caption),
            )
            for fragment_format in fragment_formats:
                fragment = fragment_format.format(caption=caption)
                with self.subTest(caption=caption, fragment=fragment):
                    result = ai_processing._suppress_competing_attachment_updates(
                        {
                            "updates": [{"column": "Docks", "value": "6"}],
                            "events": [],
                            "response_email": "Thanks.",
                        },
                        _conversation("The brochure is attached."),
                        "100 Main St, Phoenix",
                        [{
                            "name": "mixed brochure.pdf",
                            "text": f"100 Main St, Phoenix\n{fragment}",
                        }],
                    )

                    self.assertEqual([], result["updates"])
                    self.assertTrue(any(
                        event.get("type") == "needs_user_input"
                        and event.get("reason") == "multi_property_attachment"
                        for event in result["events"]
                    ))
                    self.assertIsNone(result["response_email"])

    def test_caption_cell_normalization_does_not_collapse_multi_cell_rows(self):
        multi_cell_rows = (
            "| Figure I | Oak Center |",
            "Figure I | Building Facts",
            '| "Figure I | Oak Center |',
        )

        for row in multi_cell_rows:
            with self.subTest(row=row):
                self.assertIsNone(
                    ai_processing._document_caption_verdict(row)
                )

    def test_malformed_caption_designator_wrappers_fail_closed(self):
        caption_formats = (
            "Table (1]: {residual}",
            "fIgUrE [IV) — {residual}",
            "Section (4.2: {residual}",
            "Schedule [A-1 - {residual}",
            "Exhibit B.2) ({residual})",
            "VERSION IV] — {residual}",
            "Figure ((IV)): {residual}",
            "Figure ([IV]): {residual}",
            "Figure [[IV]]: {residual}",
            "Figure (IV)): {residual}",
            "Table ((1)): {residual}",
            "Schedule [[A-1]] — {residual}",
        )
        residuals = ("Building Facts", "Oak Center")

        for caption_format in caption_formats:
            for residual in residuals:
                caption = caption_format.format(residual=residual)
                with self.subTest(caption=caption):
                    result = ai_processing._suppress_competing_attachment_updates(
                        {
                            "updates": [{"column": "Docks", "value": "6"}],
                            "events": [],
                            "response_email": "Thanks.",
                        },
                        _conversation("The brochure is attached."),
                        "100 Main St, Phoenix",
                        [{
                            "name": "mixed brochure.pdf",
                            "text": f"100 Main St, Phoenix\n{caption}\nDocks: 6",
                        }],
                    )

                    self.assertEqual([], result["updates"])
                    self.assertTrue(any(
                        event.get("type") == "needs_user_input"
                        and event.get("reason") == "multi_property_attachment"
                        for event in result["events"]
                    ))
                    self.assertIsNone(result["response_email"])

    def test_caption_syntax_precheck_ignores_property_name_tokens(self):
        property_names = (
            "Figure (Ivy Commerce Park)",
            "Table [Rock Center]",
            "Page (Commerce Center)",
            "Section [Eight Plaza]",
            "Exhibit Westgate Logistics Hub",
        )

        for property_name in property_names:
            with self.subTest(property_name=property_name):
                self.assertIsNone(
                    ai_processing._document_caption_verdict(property_name)
                )

    def test_valid_roman_caption_designators_remain_structural(self):
        roman_numerals = (
            "I", "ii", "III", "iv", "VIII", "ix", "XIV", "xlii",
            "XCIX", "cdxliv", "CMXCIX", "M", "MMXXIV", "mMmCmXcIx",
        )

        for roman_numeral in roman_numerals:
            for wrapper_format in ("({})", "[{}]"):
                caption = (
                    f"Figure {wrapper_format.format(roman_numeral)}: "
                    "Building Facts"
                )
                with self.subTest(caption=caption):
                    self.assertEqual(
                        "structural",
                        ai_processing._document_caption_verdict(caption),
                    )

    def test_invalid_roman_like_caption_tokens_fail_closed(self):
        caption_bases = (
            "Figure (Civic)",
            "Table [Mill]",
            "Page (Civil)",
            "Section [Mid]",
            "Exhibit (Livid)",
            "Figure (MMMM)",
            "Figure ((IIII))",
            "Table ([VV])",
            "Page [IC]]",
            "Section (XM]",
        )
        residuals = ("", ": Building Facts", ": Oak Center")

        for caption_base in caption_bases:
            for residual in residuals:
                caption = f"{caption_base}{residual}"
                with self.subTest(caption=caption):
                    result = ai_processing._suppress_competing_attachment_updates(
                        {
                            "updates": [{"column": "Docks", "value": "6"}],
                            "events": [],
                            "response_email": "Thanks.",
                        },
                        _conversation("The brochure is attached."),
                        "100 Main St, Phoenix",
                        [{
                            "name": "mixed brochure.pdf",
                            "text": f"100 Main St, Phoenix\n{caption}\nDocks: 6",
                        }],
                    )

                    self.assertEqual([], result["updates"])
                    self.assertTrue(any(
                        event.get("type") == "needs_user_input"
                        and event.get("reason") == "multi_property_attachment"
                        for event in result["events"]
                    ))
                    self.assertIsNone(result["response_email"])

    def test_numbered_unknown_property_headings_still_fail_closed(self):
        headings = (
            "Oak Center 1",
            "Oak Center 1: Building Facts",
            "Table Center 1: Building Facts",
            "Table 1: Oak Center",
            "Figure IV — Westgate Logistics Hub",
            "Page A-1: Oak Center",
            "Section 4.2: Oak Center",
            "Schedule B.2 (Oak Center)",
            "Schedule 1-A: Oak Center",
            "Exhibit C-3: Westgate Logistics Hub",
            "Version II: Oak Center",
            "Revision 7A - Oak Center",
            "Table (1): Oak Center",
            "fIgUrE (IV) — Westgate Logistics Hub",
            "Section (4.2): Oak Center",
            "Schedule (A-1): Oak Center",
            "Exhibit [B.2] (Oak Center)",
            "VERSION [7A] - Oak Center",
        )

        for heading in headings:
            with self.subTest(heading=heading):
                result = ai_processing._suppress_competing_attachment_updates(
                    {
                        "updates": [{"column": "Docks", "value": "6"}],
                        "events": [],
                        "response_email": "Thanks.",
                    },
                    _conversation("The brochure is attached."),
                    "100 Main St, Phoenix",
                    [{
                        "name": "mixed brochure.pdf",
                        "text": (
                            f"100 Main St, Phoenix\n{heading}\nDocks: 6"
                        ),
                    }],
                )

                self.assertEqual([], result["updates"])
                self.assertTrue(any(
                    event.get("type") == "needs_user_input"
                    and event.get("reason") == "multi_property_attachment"
                    for event in result["events"]
                ))
                self.assertIsNone(result["response_email"])

    def test_multi_column_target_tables_remain_supported(self):
        target_tables = (
            (
                "Docks | Power\n"
                "6 | 1200A 480V 3-phase"
            ),
            (
                "Docks | Power\n"
                "6 | 1200A 480V 3-phase\n"
                "100 Main St | 100 Main St"
            ),
            (
                "Docks | Power\n"
                "6 | 1200A 480V 3-phase\n"
                "100 Main St, Phoenix | 100 Main St, Phoenix"
            ),
            (
                "Asking Rate | Power\n"
                "$6.75/SF NNN | 1200A 480V 3-phase"
            ),
            (
                "Power, Docks\n"
                "1200A 480V 3-phase, 6"
            ),
            (
                "Docks\tPower\n"
                "6\t1200A 480V 3-phase"
            ),
        )

        for target_table in target_tables:
            with self.subTest(target_table=target_table):
                expected_updates = [
                    {"column": "Docks", "value": "6"},
                    {"column": "Power", "value": "1200A 480V 3-phase"},
                ]
                result = ai_processing._suppress_competing_attachment_updates(
                    {
                        "updates": list(expected_updates),
                        "events": [],
                        "response_email": "Thanks.",
                    },
                    _conversation("The target brochure is attached."),
                    "100 Main St, Phoenix",
                    [{
                        "name": "100 Main St brochure.pdf",
                        "text": f"100 Main St\n{target_table}",
                    }],
                )

                self.assertEqual(expected_updates, result["updates"])
                self.assertEqual([], result["events"])
                self.assertEqual("Thanks.", result["response_email"])

    def test_exact_target_postfix_preserves_target_fact_updates(self):
        target_clauses = (
            "Docks: 6 | 100 Main St",
            "Docks: 6; 100 Main St",
            "Docks: 6\n100 Main St",
        )

        for target_clause in target_clauses:
            with self.subTest(target_clause=target_clause):
                expected_update = {"column": "Docks", "value": "6"}
                result = ai_processing._suppress_competing_attachment_updates(
                    {
                        "updates": [expected_update],
                        "events": [],
                        "response_email": "Thanks.",
                    },
                    _conversation("The target brochure is attached."),
                    "100 Main St, Phoenix",
                    [{
                        "name": "100 Main St brochure.pdf",
                        "text": f"100 Main St - 20,000 SF. {target_clause}.",
                    }],
                )

                self.assertEqual([expected_update], result["updates"])
                self.assertEqual([], result["events"])
                self.assertEqual("Thanks.", result["response_email"])

    def test_postfixed_fact_token_property_names_remain_competing(self):
        competing_headings = (
            ("Docks", "6", "Docks: 6 | Power Center"),
            ("Docks", "6", "Docks: 6 | Oak Docks"),
            ("Power", "1200A", "Power: 1200A | Westgate Power"),
        )

        for column, value, competing_heading in competing_headings:
            with self.subTest(competing_heading=competing_heading):
                result = ai_processing._suppress_competing_attachment_updates(
                    {
                        "updates": [{"column": column, "value": value}],
                        "events": [],
                        "response_email": "Thanks.",
                    },
                    _conversation("The brochure is attached."),
                    "100 Main St, Phoenix",
                    [{
                        "name": "mixed brochure.pdf",
                        "text": f"100 Main St - 20,000 SF. {competing_heading}.",
                    }],
                )

                self.assertEqual([], result["updates"])
                self.assertIsNone(result["response_email"])
                self.assertTrue(any(
                    event.get("reason") == "multi_property_attachment"
                    for event in result["events"]
                ))

    def test_street_suffix_period_cannot_hide_competing_table_heading(self):
        proposal = {
            "updates": [{"column": "Docks", "value": "6"}],
            "events": [],
            "response_email": "Thanks.",
        }

        result = ai_processing._suppress_competing_attachment_updates(
            proposal,
            _conversation("The brochure is attached."),
            "100 Main St, Phoenix",
            [{
                "name": "mixed brochure.pdf",
                "text": "100 Main St. Oak Center | Docks: 6.",
            }],
        )

        self.assertEqual([], result["updates"])
        self.assertTrue(any(
            event.get("type") == "needs_user_input"
            and event.get("reason") == "multi_property_attachment"
            for event in result["events"]
        ))
        self.assertIsNone(result["response_email"])

    def test_exact_target_pdf_preserves_semantic_fact_label_synonym(self):
        target_facts = (
            ("Docks", "6", "Dock Positions: 6"),
            ("Drive Ins", "2", "Grade Level Doors: 2"),
            ("Power", "1200A", "Electrical Capacity: 1200A"),
            ("Power", "1200A 480V 3-phase", "Power: 1200A 480V 3-phase"),
            ("Rent/SF/Yr", "6.75", "Asking Rate: $6.75"),
            ("Rent/SF/Yr", "6.75", "Asking Rate: $6.75/SF NNN"),
            ("Ops Ex/SF/Yr", "1.85", "CAM Charges: $1.85"),
            ("Total SF", "45000", "Available Sq Ft: 45000"),
        )

        for column, value, target_fact in target_facts:
            with self.subTest(column=column, target_fact=target_fact):
                expected_update = {"column": column, "value": value}
                proposal = {
                    "updates": [expected_update],
                    "events": [],
                    "response_email": "Thanks for confirming.",
                }

                result = ai_processing._suppress_competing_attachment_updates(
                    proposal,
                    _conversation("The 100 Main St brochure is attached."),
                    "100 Main St, Phoenix",
                    [{
                        "name": "100 Main St brochure.pdf",
                        "text": f"100 Main St. {target_fact}.",
                    }],
                )

                self.assertEqual([expected_update], result["updates"])
                self.assertEqual([], result["events"])
                self.assertEqual(
                    "Thanks for confirming.",
                    result["response_email"],
                )

    def test_fact_token_property_names_remain_competing(self):
        competing_headings = (
            ("Power", "1200A", "Power Center | 1200A"),
            ("Power", "1200A", "Power Square | 1200A"),
            ("Power", "1200A", "Westgate Power | 1200A"),
            ("Docks", "6", "Oak Docks | 6"),
        )

        for column, value, competing_heading in competing_headings:
            with self.subTest(competing_heading=competing_heading):
                result = ai_processing._suppress_competing_attachment_updates(
                    {
                        "updates": [{"column": column, "value": value}],
                        "events": [],
                        "response_email": "Thanks.",
                    },
                    _conversation("The brochure is attached."),
                    "100 Main St, Phoenix",
                    [{
                        "name": "mixed brochure.pdf",
                        "text": f"100 Main St. {competing_heading}.",
                    }],
                )

                self.assertEqual([], result["updates"])
                self.assertIsNone(result["response_email"])
                self.assertTrue(any(
                    event.get("reason") == "multi_property_attachment"
                    for event in result["events"]
                ))

    def test_versioned_addressless_attachment_is_competing_by_default(self):
        attachment_names = (
            "Oak Commerce Center brochure version 2.pdf",
            "Oak Commerce Center brochure (2).pdf",
        )

        for attachment_name in attachment_names:
            with self.subTest(attachment_name=attachment_name):
                proposal = {
                    "updates": [{"column": "Ceiling Ht", "value": "32"}],
                    "events": [],
                    "response_email": "Thanks.",
                }

                result = ai_processing._suppress_competing_attachment_updates(
                    proposal,
                    _conversation(
                        "Another option is Oak Commerce Center; brochure attached."
                    ),
                    "100 Main St, Phoenix",
                    [
                        {
                            "name": "100 Main St brochure.pdf",
                            "text": "100 Main St - 20,000 SF.",
                        },
                        {
                            "name": attachment_name,
                            "text": "Ceiling Ht: 32 feet clear.",
                        },
                    ],
                )

                self.assertEqual([], result["updates"])
                self.assertIsNone(result["response_email"])

    def test_exact_target_address_clause_preserves_value(self):
        expected_update = {"column": "Ceiling Ht", "value": "28"}
        proposal = {
            "updates": [expected_update],
            "events": [],
            "response_email": None,
        }

        result = ai_processing._suppress_competing_attachment_updates(
            proposal,
            _conversation("100 Main St has 28 feet clear."),
            "100 Main St, Phoenix",
            [
                {
                    "name": "100 Main St brochure.pdf",
                    "text": "100 Main St - 20,000 SF.",
                },
                {
                    "name": "200 Oak Ave brochure.pdf",
                    "text": "200 Oak Ave - Ceiling Ht: 32 feet clear.",
                },
            ],
        )

        self.assertEqual([expected_update], result["updates"])

    def test_independent_target_attachment_evidence_preserves_value(self):
        expected_update = {
            "column": "Ceiling Ht",
            "value": "32",
            "confidence": 0.96,
        }
        proposal = {
            "updates": [expected_update],
            "events": [],
            "response_email": None,
        }

        result = ai_processing._suppress_competing_attachment_updates(
            proposal,
            _conversation("We have two options; the brochures are attached."),
            "100 Main St, Phoenix",
            [
                {
                    "name": "100 Main St brochure.pdf",
                    "text": "100 Main St - Ceiling Ht: 32 feet clear.",
                },
                {
                    "name": "200 Oak Ave brochure.pdf",
                    "text": "200 Oak Ave - Ceiling Ht: 32 feet clear.",
                },
            ],
        )

        self.assertEqual([expected_update], result["updates"])

    def test_generic_this_property_does_not_prove_attachment_value(self):
        proposal = {
            "updates": [{"column": "Ceiling Ht", "value": "32"}],
            "events": [],
            "response_email": None,
        }

        result = ai_processing._suppress_competing_attachment_updates(
            proposal,
            _conversation(
                "This property has 32 feet clear. We also have another option; "
                "the brochures are attached."
            ),
            "100 Main St, Phoenix",
            [
                {
                    "name": "100 Main St brochure.pdf",
                    "text": "100 Main St - 20,000 SF industrial building.",
                },
                {
                    "name": "200 Oak Ave brochure.pdf",
                    "text": "200 Oak Ave - Ceiling Ht: 32 feet clear.",
                },
            ],
        )

        self.assertEqual([], result["updates"])

    def test_attachment_classification_does_not_depend_on_offer_wording(self):
        proposal = {
            "updates": [{"column": "Ceiling Ht", "value": "32"}],
            "events": [],
            "response_email": None,
        }

        result = ai_processing._suppress_competing_attachment_updates(
            proposal,
            _conversation("Please see the attached brochures."),
            "100 Main St, Phoenix",
            [
                {
                    "name": "100 Main St brochure.pdf",
                    "text": "100 Main St - 20,000 SF industrial building.",
                },
                {
                    "name": "200 Oak Ave brochure.pdf",
                    "text": "200 Oak Ave - Ceiling Ht: 32 feet clear.",
                },
            ],
        )

        self.assertEqual([], result["updates"])

    def test_target_fact_before_brokerage_footer_address_remains_supported(self):
        expected_update = {"column": "Ceiling Ht", "value": "28"}
        proposal = {
            "updates": [expected_update],
            "events": [],
            "response_email": None,
        }

        result = ai_processing._suppress_competing_attachment_updates(
            proposal,
            _conversation("Please see the attached brochure."),
            "105 W Dewey Ave, Wharton",
            [{
                "name": "105 W Dewey Ave brochure.pdf",
                "text": (
                    "105 W Dewey Ave FOR LEASE. Ceiling Ht: 28 feet clear. "
                    "Garden State Realty, 204 Passaic Ave, Fairfield."
                ),
            }],
        )

        self.assertEqual([expected_update], result["updates"])

    def test_target_address_number_is_not_ceiling_height_evidence(self):
        proposal = {
            "updates": [{"column": "Ceiling Ht", "value": "32"}],
            "events": [],
            "response_email": None,
        }

        result = ai_processing._suppress_competing_attachment_updates(
            proposal,
            _conversation("Please see the attached brochures."),
            "32 Main St, Phoenix",
            [
                {
                    "name": "32 Main St brochure.pdf",
                    "text": "32 Main St - 20,000 SF industrial building.",
                },
                {
                    "name": "200 Oak Ave brochure.pdf",
                    "text": "200 Oak Ave - Ceiling Ht: 32 feet clear.",
                },
            ],
        )

        self.assertEqual([], result["updates"])

    def test_new_property_event_keeps_attachment_binding_owned_by_event_path(self):
        expected_update = {"column": "Ceiling Ht", "value": "32"}
        proposal = {
            "updates": [expected_update],
            "events": [
                {"type": "new_property", "address": "200 Oak Ave", "city": "Phoenix"}
            ],
            "response_email": "Thanks for the option.",
        }

        result = ai_processing._suppress_competing_attachment_updates(
            proposal,
            _conversation("The alternate brochure is attached."),
            "100 Main St, Phoenix",
            [{
                "name": "200 Oak Ave brochure.pdf",
                "text": "200 Oak Ave - Ceiling Ht: 32 feet clear.",
            }],
        )

        self.assertEqual([expected_update], result["updates"])
        self.assertEqual("new_property", result["events"][0]["type"])
        self.assertEqual("Thanks for the option.", result["response_email"])

    def test_competing_multi_property_brochure_escalates_instead_of_writing_current_row(self):
        proposal = {
            "updates": [
                {"column": "Rent/SF /Yr", "value": "15.75", "confidence": 0.72},
                {"column": "Total SF", "value": "9500", "confidence": 0.92},
            ],
            "events": [{"type": "tour_requested", "question": "Glad to show."}],
            "response_email": "Thanks.",
        }
        brochure = {
            "name": "AUSTIN BUSINESS PARK NEW.pdf",
            "text": (
                "Austin Business Park 3336 SC-51 Fort Mill. "
                "Building 1: 9,500 SF, $18 PSF. "
                "Building 2: 3,000 SF, $13 PSF. "
                "Building 3: 7,500 SF, $15 PSF."
            ),
        }

        result = ai_processing._suppress_competing_attachment_updates(
            proposal,
            _conversation(
                "I have 2 buildings here: 7,500 SF and 9,500 SF. "
                "Brochure with rent info attached."
            ),
            "3344 S Carolina 51, Fort Mill",
            [brochure],
        )

        self.assertEqual([], result["updates"])
        self.assertIn(
            "multi_property_attachment",
            [event.get("reason") for event in result["events"]],
        )
        self.assertIsNone(result["response_email"])

        current, _ = processing._partition_property_attachments(
            [brochure],
            current_anchor="3344 S Carolina 51, Fort Mill",
            events=result["events"],
        )
        self.assertEqual([], current)

    def test_competing_multi_property_brochure_keeps_only_canonical_review_event(self):
        brochure = {
            "name": "Fictional portfolio availability.pdf",
            "text": (
                "100 Main St is the target. "
                "200 Oak Ave has 9,500 SF available."
            ),
        }
        canonical_review = {
            "type": "needs_user_input",
            "reason": "multi_property_attachment",
            "question": (
                "The broker offered multiple properties or suites in an attachment, "
                "but the details could not be bound safely to one row."
            ),
        }
        event_matrix = {
            "unclear": [{
                "type": "needs_user_input",
                "reason": "unclear",
                "question": "Please review the packet.",
            }],
            "call_requested": [{
                "type": "call_requested",
                "question": "Please call about an unrelated issue.",
            }],
            "property_unavailable": [{
                "type": "property_unavailable",
                "reason": "off_market",
            }],
            "close_conversation": [{
                "type": "close_conversation",
                "reason": "all_info_gathered",
            }],
            "all_competing_events_and_duplicate_review": [
                {
                    "type": "call_requested",
                    "question": "Please call about an unrelated issue.",
                },
                {
                    "type": "needs_user_input",
                    "reason": "unclear",
                    "question": "Please review the packet.",
                },
                {
                    "type": "property_unavailable",
                    "reason": "off_market",
                },
                {
                    "type": "close_conversation",
                    "reason": "all_info_gathered",
                },
                {
                    "type": "needs_user_input",
                    "reason": "multi_property_attachment",
                    "question": "Which property should receive the attachment facts?",
                },
                {
                    "type": "needs_user_input",
                    "reason": "multi_property_attachment",
                    "question": "Duplicate attachment review.",
                },
            ],
        }

        for name, events in event_matrix.items():
            with self.subTest(name=name):
                proposal = {
                    "updates": [{"column": "Total SF", "value": "9500"}],
                    "events": events,
                    "response_email": "Thanks.",
                }

                result = ai_processing._suppress_competing_attachment_updates(
                    proposal,
                    _conversation("The attached packet covers several options."),
                    "100 Main St, Phoenix",
                    [brochure],
                )

                self.assertEqual([], result["updates"])
                self.assertEqual([canonical_review], result["events"])
                self.assertIsNone(result["response_email"])

    def test_validated_contact_optout_dominates_competing_attachment_review(self):
        first_optout = {
            "type": "contact_optout",
            "reason": "unsubscribe",
        }
        proposal = {
            "updates": [{"column": "Total SF", "value": "9500"}],
            "events": [
                first_optout,
                {"type": "contact_optout", "reason": "do_not_contact"},
                {"type": "call_requested", "question": "Please call."},
            ],
            "response_email": "Thanks.",
        }
        conversation = _conversation(
            "Please unsubscribe me and do not contact me again. "
            "The attached packet covers several options."
        )
        brochure = {
            "name": "Fictional portfolio availability.pdf",
            "text": (
                "100 Main St is the target. "
                "200 Oak Ave has 9,500 SF available."
            ),
        }

        proposal = ai_processing._augment_events_with_deterministic_signals(
            proposal,
            conversation,
            target_anchor="100 Main St, Phoenix",
        )
        self.assertEqual(first_optout, proposal["events"][0])

        result = ai_processing._suppress_competing_attachment_updates(
            proposal,
            conversation,
            "100 Main St, Phoenix",
            [brochure],
        )

        self.assertEqual([], result["updates"])
        self.assertEqual([first_optout], result["events"])
        self.assertIsNone(result["response_email"])

    def test_replacement_only_reply_cannot_update_original_row(self):
        proposal = {
            "updates": [
                {"column": "Total SF", "value": "8000", "confidence": 0.72},
                {"column": "Drive Ins", "value": "1", "confidence": 0.78},
                {"column": "Ceiling Ht", "value": "12", "confidence": 0.86},
            ],
            "events": [
                {
                    "type": "new_property",
                    "address": "48 Richboynton Road",
                    "city": "Dover",
                }
            ],
        }

        result = ai_processing._suppress_cross_property_current_row_updates(
            proposal,
            _conversation(
                "I have ~8K S.F. at 48 Richboynton Road in Dover. It has a "
                "10' drive-in door. Ceilings are 14' to the deck but only 12' clear."
            ),
            "53 Richboynton Rd, Dover",
        )

        self.assertEqual([], result["updates"])

    def test_mixed_reply_keeps_current_property_updates(self):
        proposal = {
            "updates": [
                {"column": "Total SF", "value": "7200", "confidence": 0.95},
                {"column": "Drive Ins", "value": "3", "confidence": 0.9},
            ],
            "events": [
                {
                    "type": "new_property",
                    "address": "[TBD] Sterling Plaza Phase II",
                    "city": "Ponte Vedra, FL",
                }
            ],
        }

        result = ai_processing._suppress_cross_property_current_row_updates(
            proposal,
            _conversation(
                "Yes, this space meets your criteria and is available for sale. "
                "It is 7,200 sf and has three grade-level doors. We also have a "
                "newly built park adjacent to this location called Sterling Plaza Phase II."
            ),
            "200 Sterling Plaza Dr, Town Of Nocatee",
        )

        self.assertEqual(2, len(result["updates"]))

    def test_replacement_this_building_language_cannot_update_original_row(self):
        proposal = {
            "updates": [
                {"column": "Total SF", "value": "8000", "confidence": 0.93}
            ],
            "events": [
                {"type": "new_property", "address": "Suite B", "city": "Phoenix"}
            ],
        }

        result = ai_processing._suppress_cross_property_current_row_updates(
            proposal,
            _conversation("This building is Suite B and has 8,000 SF."),
            "100 SiteSift Canary Way, Phoenix",
        )

        self.assertEqual([], result["updates"])

    def test_target_mention_does_not_license_alternate_property_updates(self):
        proposal = {
            "updates": [
                {"column": "Total SF", "value": "8000", "confidence": 0.93}
            ],
            "events": [
                {"type": "new_property", "address": "Suite B", "city": "Phoenix"}
            ],
        }

        result = ai_processing._suppress_cross_property_current_row_updates(
            proposal,
            _conversation(
                "For 100 SiteSift Canary Way, see the prior note. The alternative "
                "Suite B has 8,000 SF."
            ),
            "100 SiteSift Canary Way, Phoenix",
        )

        self.assertEqual([], result["updates"])

    def test_explicit_current_facts_before_alternate_remain_on_current_row(self):
        proposal = {
            "updates": [
                {"column": "Total SF", "value": "7200", "confidence": 0.95}
            ],
            "events": [
                {"type": "new_property", "address": "Suite B", "city": "Phoenix"}
            ],
        }

        result = ai_processing._suppress_cross_property_current_row_updates(
            proposal,
            _conversation(
                "100 SiteSift Canary Way is 7,200 SF. We also have Suite B, "
                "which is 8,000 SF."
            ),
            "100 SiteSift Canary Way, Phoenix",
        )

        self.assertEqual(
            [{"column": "Total SF", "value": "7200", "confidence": 0.95}],
            result["updates"],
        )

    def test_sterling_attachments_are_partitioned_by_property(self):
        permit = {
            "name": "2121 American Wall Beds Co PERMIT REV2 11 18 22.pdf",
            "text": "ROF TUO DLIUB TNANET .OC DEB LLAW NACIREMA RD AZALP GNILRETS 002",
        }
        alternate_flyer = {
            "name": "STERLING PLAZA PHASE II FLYER UPDATE 5.8.pdf",
            "text": "STERLING PLAZA PHASE II PONTE VEDRA, FL - 2,400 SF units",
        }
        events = [
            {
                "type": "new_property",
                "address": "[TBD] newly built park adjacent to Sterling Plaza "
                "(FutureFlex / Sterling Plaza Phase II)",
                "city": "Ponte Vedra, FL",
            }
        ]

        current, by_event = processing._partition_property_attachments(
            [permit, alternate_flyer],
            current_anchor="200 Sterling Plaza Dr, Town Of Nocatee",
            events=events,
        )

        self.assertEqual([permit], current)
        self.assertEqual([alternate_flyer], by_event[0])

    def test_replacement_floorplan_does_not_land_on_original_row(self):
        floorplan = {
            "name": "48RichboyntonRoad1stFloor8910.pdf",
            "text": "48 Richboynton Road - 1st Floor - 8,910 S.F.",
        }
        events = [
            {
                "type": "new_property",
                "address": "48 Richboynton Road",
                "city": "Dover",
            }
        ]

        current, by_event = processing._partition_property_attachments(
            [floorplan],
            current_anchor="53 Richboynton Rd, Dover",
            events=events,
        )

        self.assertEqual([], current)
        self.assertEqual([floorplan], by_event[0])

    def test_target_brochure_ignores_brokerage_office_address(self):
        brochure = {
            "name": "105 W Dewey Ave, Bldg B, 9&10, Wharton_Brochure.pdf",
            "text": (
                "105 W Dewey Ave FOR LEASE. 8,000 SF. "
                "Garden State Realty, 204 Passaic Ave, Fairfield."
            ),
        }

        current, by_event = processing._partition_property_attachments(
            [brochure],
            current_anchor="105 W Dewey Ave, Wharton",
            events=[],
        )

        self.assertEqual([brochure], current)
        self.assertEqual([], by_event)

    def test_multi_property_pause_quarantines_mixed_attachment_from_current_row(self):
        mixed = {
            "name": "Fictional portfolio availability.pdf",
            "text": (
                "101 Fictional Forge Road is available, but no confirmed target "
                "figures are provided. 202 Imaginary Industry Avenue Suite A has "
                "12,650 SF at $18.75/SF/YR."
            ),
        }
        events = [{
            "type": "needs_user_input",
            "reason": "multi_property_attachment",
            "question": "Which property should receive the attachment facts?",
        }]

        current, by_event = processing._partition_property_attachments(
            [mixed],
            current_anchor="101 Fictional Forge Road, Exampleton",
            events=events,
        )

        self.assertEqual([], current)
        self.assertEqual([], by_event)

    def test_multi_property_pause_routes_only_target_only_attachment(self):
        target_only = {
            "name": "Target availability.pdf",
            "text": "101 Fictional Forge Road, Exampleton is available for lease.",
        }
        competing = {
            "name": "Alternate availability.pdf",
            "text": "202 Imaginary Industry Avenue, Exampleton has 12,650 SF.",
        }
        addressless = {
            "name": "Unbound availability.pdf",
            "text": "An industrial option has complete specifications but no address.",
        }
        events = [{
            "type": "needs_user_input",
            "reason": "multi_property_attachment",
        }]

        current, by_event = processing._partition_property_attachments(
            [target_only, competing, addressless],
            current_anchor="101 Fictional Forge Road, Exampleton",
            events=events,
        )

        self.assertEqual([target_only], current)
        self.assertEqual([], by_event)

    def test_same_city_phase_attachments_route_to_the_unique_event(self):
        phase_one = {
            "name": "Sterling Plaza Phase I brochure.pdf",
            "text": "Sterling Plaza Phase I, Phoenix - 5,000 SF",
        }
        phase_two = {
            "name": "Sterling Plaza Phase II brochure.pdf",
            "text": "Sterling Plaza Phase II, Phoenix - 8,000 SF",
        }
        events = [
            {
                "type": "new_property",
                "address": "Sterling Plaza Phase I",
                "city": "Phoenix",
            },
            {
                "type": "new_property",
                "address": "Sterling Plaza Phase II",
                "city": "Phoenix",
            },
        ]

        current, by_event = processing._partition_property_attachments(
            [phase_one, phase_two],
            current_anchor="100 SiteSift Canary Way, Phoenix",
            events=events,
        )

        self.assertEqual([], current)
        self.assertEqual([phase_one], by_event[0])
        self.assertEqual([phase_two], by_event[1])

    def test_unresolved_replacement_attachment_is_not_defaulted_to_first_event(self):
        ambiguous = {
            "name": "Phoenix options brochure.pdf",
            "text": "Two industrial options are available in Phoenix.",
        }
        events = [
            {"type": "new_property", "address": "Suite A", "city": "Phoenix"},
            {"type": "new_property", "address": "Suite B", "city": "Phoenix"},
        ]

        current, by_event = processing._partition_property_attachments(
            [ambiguous],
            current_anchor="100 SiteSift Canary Way, Phoenix",
            events=events,
        )

        self.assertEqual([], current)
        self.assertEqual([[], []], by_event)

    def test_mixed_current_and_alternate_brochure_is_left_for_review(self):
        mixed = {
            "name": "Current and Suite B brochure.pdf",
            "text": (
                "100 SiteSift Canary Way - 7,500 SF. "
                "200 Alternate Road Suite B - 8,000 SF."
            ),
        }
        events = [
            {
                "type": "new_property",
                "address": "200 Alternate Road Suite B",
                "city": "Phoenix",
            }
        ]

        current, by_event = processing._partition_property_attachments(
            [mixed],
            current_anchor="100 SiteSift Canary Way, Phoenix",
            events=events,
        )

        self.assertEqual([], current)
        self.assertEqual([[]], by_event)

    def test_requirements_mismatch_has_truthful_terminal_label(self):
        event = {"type": "property_unavailable", "reason": "requirements_mismatch"}

        self.assertEqual(
            "requirements_mismatch",
            processing._nonviable_status_reason(event),
        )
        comment = processing._build_property_unavailable_comment(
            "07/21/2026",
            "requirements_mismatch",
            [event],
        )
        self.assertIn("does not meet client requirements", comment.lower())
        self.assertNotIn("marked unavailable", comment.lower())

    def test_deterministic_mismatch_normalizes_model_reason(self):
        for model_reason in ("physical_non_fit", "Requirements_Mismatch", "bad_fit"):
            with self.subTest(model_reason=model_reason):
                proposal = {
                    "updates": [],
                    "events": [
                        {
                            "type": "property_unavailable",
                            "reason": model_reason,
                        }
                    ],
                    "response_email": "Thanks for the update.",
                }
                result = ai_processing._augment_events_with_deterministic_signals(
                    proposal,
                    _conversation(
                        "The space is too office-heavy and does not meet the "
                        "client's warehouse requirements."
                    ),
                    target_anchor="100 SiteSift Canary Way, Phoenix",
                )

                unavailable = [
                    event
                    for event in result["events"]
                    if event.get("type") == "property_unavailable"
                ]
                self.assertEqual(1, len(unavailable))
                self.assertEqual("requirements_mismatch", unavailable[0]["reason"])
                self.assertIsNone(result["response_email"])

    def test_requirements_mismatch_fallback_never_claims_property_is_unavailable(self):
        body = processing._select_automatic_response_body(
            "requirements_mismatch",
            None,
            {},
            "Baylor",
        )

        self.assertIn("does not meet", body.lower())
        self.assertIn("requirements", body.lower())
        self.assertNotIn("no longer available", body.lower())

    def test_requirements_mismatch_with_alternative_fallback_is_truthful(self):
        body = processing._select_automatic_response_body(
            "requirements_mismatch_with_alternative",
            None,
            {},
            "Baylor",
        )

        self.assertIn("does not meet", body.lower())
        self.assertIn("alternative", body.lower())
        self.assertNotIn("no longer available", body.lower())

    def test_requirements_mismatch_stops_followups_before_sheet_move(self):
        events = [{"type": "property_unavailable", "reason": "requirements_mismatch"}]

        patch = processing._pending_nonviable_followup_patch(
            events,
            row_anchor="111 Canfield Ave, Randolph",
            message_text="The units do not have a drive in door.",
        )

        self.assertEqual("stopped", patch["followUpStatus"])
        self.assertIsNone(patch["followUpConfig.nextFollowUpAt"])
        self.assertEqual("requirements_mismatch", patch["pendingTerminalReason"])


if __name__ == "__main__":
    unittest.main()
