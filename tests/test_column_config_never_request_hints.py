"""Regression test for FIX-17 / M35.

When a field is in `neverRequest`, `build_column_rules_prompt` previously
rendered only `<description>. Accept if provided but NEVER request.` and
DROPPED the field's `extraction_hints`. GPT-5.2 read the bare never-request
line as de-emphasis and skipped PDF-sourced asking rent (M35, rent NOT
extracted in 3/3 live accept-new-property cases while every other spec from
the SAME PDF text extracted). The fix renders the extraction hints alongside
the never-request rule so the model still knows HOW to recognize/normalize the
value it is allowed to accept.
"""

import unittest

from email_automation.column_config import (
    CANONICAL_FIELDS,
    build_column_rules_prompt,
    get_default_column_config,
)


class NeverRequestRendersHintsTest(unittest.TestCase):
    def _rent_line(self, prompt: str) -> str:
        rent_col = CANONICAL_FIELDS["rent_sf_yr"]["default_aliases"][0]
        for line in prompt.splitlines():
            if line.startswith(f'- "{rent_col}"'):
                return line
        self.fail(f"No rendered rule line for rent column {rent_col!r}:\n{prompt}")

    def test_never_request_line_includes_extraction_hints(self):
        config = get_default_column_config()
        # Precondition: rent is a never-request field in the default config.
        self.assertIn("rent_sf_yr", config["neverRequest"])

        prompt = build_column_rules_prompt(config)
        rent_line = self._rent_line(prompt)

        # The never-request rule must still be present...
        self.assertIn("NEVER request", rent_line)
        # ...but the extraction hint text (HOW to recognize/normalize the value)
        # must NOT be dropped. This fragment lives only in extraction_hints,
        # not in the field description.
        self.assertIn("Output plain decimal", rent_line)
        self.assertIn("per SF per YEAR", rent_line)

    def test_all_never_request_extractable_fields_keep_their_hints(self):
        config = get_default_column_config()
        prompt = build_column_rules_prompt(config)
        lines = prompt.splitlines()

        for canonical in config["neverRequest"]:
            field = CANONICAL_FIELDS.get(canonical, {})
            if not field.get("extractable"):
                continue
            hints = field.get("extraction_hints")
            if not hints:
                continue
            col = config["mappings"].get(canonical)
            if not col:
                continue
            rule_line = next(
                (ln for ln in lines if ln.startswith(f'- "{col}"')), None
            )
            self.assertIsNotNone(
                rule_line, f"Missing rule line for never-request field {canonical}"
            )
            self.assertIn("NEVER request", rule_line)
            # A distinctive slice of the hint text must survive rendering.
            hint_fragment = hints.split(".")[0]
            self.assertIn(
                hint_fragment,
                rule_line,
                f"extraction_hints dropped for never-request field {canonical}",
            )


class ExtractableFieldNoneHintsFallsBackToDescription(unittest.TestCase):
    """CodeRabbit PR#15: the extractable (non-never-request) branch must fall back
    to the description when extraction_hints is present-but-None, not only when the
    key is missing. Otherwise a future field with extraction_hints=None +
    extractable=True would emit the literal 'None' into the AI prompt."""

    def test_none_hints_extractable_field_renders_description(self):
        from unittest import mock

        synthetic = {
            "description": "Synthetic field description that must survive.",
            "extraction_hints": None,
            "extractable": True,
            "is_formula": False,
            "default_aliases": ["Synthetic Col"],
            "ai_synonyms": [],
        }
        patched = {**CANONICAL_FIELDS, "synthetic_none_hints": synthetic}
        config = {
            "mappings": {"synthetic_none_hints": "Synthetic Col"},
            "customFields": {},
            "requiredFields": [],
            "neverRequest": [],
        }
        with mock.patch.dict(
            "email_automation.column_config.CANONICAL_FIELDS", patched, clear=True
        ):
            prompt = build_column_rules_prompt(config)

        line = next(
            (ln for ln in prompt.splitlines() if ln.startswith('- "Synthetic Col"')),
            None,
        )
        self.assertIsNotNone(line)
        self.assertIn("Synthetic field description that must survive.", line)
        self.assertNotIn("None", line)


if __name__ == "__main__":
    unittest.main()
