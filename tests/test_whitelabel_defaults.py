from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class WhiteLabelDefaultTests(unittest.TestCase):
    def test_ai_response_prompt_does_not_target_jill_style(self):
        source = (PROJECT_ROOT / "email_automation" / "ai_processing.py").read_text()

        self.assertNotIn("Jill Ames' communication style", source)
        self.assertNotIn("matching Jill", source)

    def test_default_client_comment_column_is_generic_for_new_sheets(self):
        from email_automation.column_config import CANONICAL_FIELDS, get_default_column_config

        client_comments = CANONICAL_FIELDS["client_comments"]
        default_mapping = get_default_column_config()["mappings"]["client_comments"]

        self.assertEqual(client_comments["label"], "Client / Team Comments")
        self.assertEqual(default_mapping, "client / team comments")
        self.assertNotIn("jill", client_comments["label"].lower())
        self.assertNotIn("jill", default_mapping.lower())

    def test_legacy_jill_comment_header_still_maps_for_existing_sheets(self):
        from email_automation.column_config import detect_column_mapping

        mapping = detect_column_mapping(
            ["Property Address", "City", "Jill and Clients comments"],
            use_ai=False,
        )

        self.assertEqual(
            mapping["mappings"].get("client_comments"),
            "Jill and Clients comments",
        )

    def test_generic_client_comment_headers_are_found_before_legacy_aliases(self):
        from email_automation.column_config import find_client_comment_column_index

        headers = [
            "Property Address",
            "Jill and Clients comments",
            "Client / Team Comments",
        ]

        self.assertEqual(find_client_comment_column_index(headers), 3)


if __name__ == "__main__":
    unittest.main()
