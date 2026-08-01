import unittest


try:
    from email_automation.recovery_payload import (
        build_canonical_recovery_payload,
        hash_recovery_payload,
        hash_recovery_script,
        serialize_canonical_recovery_payload,
    )
except ImportError as error:
    _IMPORT_ERROR = error
else:
    _IMPORT_ERROR = None


KNOWN_INPUT = {
    "uid": " synthetic-user-a ",
    "clientId": " synthetic-client-a ",
    "outboxId": " synthetic-outbox-a ",
    "recoveryRunId": " synthetic-run-a ",
    "outbox": {
        "assignedEmails": [" Broker@Example.TEST "],
        "script": "Hi Test,\r\n\rAvailability for Café 🏠?\rThanks.",
        "subject": "  100 Test Street 🏠  ",
        "askFields": [
            " Rent/SF/YR ",
            "rent/sf/yr",
            "CAP Rate",
            " cap rate ",
        ],
        "rowNumber": 3,
    },
}

EXPECTED_PAYLOAD = {
    "schemaVersion": 1,
    "recoveryProfile": "managedInitialOutreachN1",
    "uid": "synthetic-user-a",
    "clientId": "synthetic-client-a",
    "outboxId": "synthetic-outbox-a",
    "recoveryRunId": "synthetic-run-a",
    "source": "managed_initial_outreach_n1",
    "actionType": "campaign_launch",
    "assignedEmails": ["broker@example.test"],
    "script": "Hi Test,\n\nAvailability for Café 🏠?\nThanks.",
    "subject": "100 Test Street 🏠",
    "askFields": ["Rent/SF/YR", "CAP Rate"],
    "rowNumber": 3,
}

EXPECTED_BYTES = (
    '{"schemaVersion":1,"recoveryProfile":"managedInitialOutreachN1",'
    '"uid":"synthetic-user-a","clientId":"synthetic-client-a",'
    '"outboxId":"synthetic-outbox-a","recoveryRunId":"synthetic-run-a",'
    '"source":"managed_initial_outreach_n1","actionType":"campaign_launch",'
    '"assignedEmails":["broker@example.test"],'
    '"script":"Hi Test,\\n\\nAvailability for Café 🏠?\\nThanks.",'
    '"subject":"100 Test Street 🏠",'
    '"askFields":["Rent/SF/YR","CAP Rate"],"rowNumber":3}'
).encode("utf-8")
EXPECTED_PAYLOAD_HASH = (
    "964eb6535090ad4d0f82249644cc6f8ac10ba7b1e6306d7c552f38f070769633"
)
EXPECTED_SCRIPT_HASH = (
    "e7cd4fa9b3f5619c4dbc668aac27ca67e1e9dd879f4f34bdcc4a56784bfb499b"
)
PAYLOAD_KEYS = [
    "schemaVersion",
    "recoveryProfile",
    "uid",
    "clientId",
    "outboxId",
    "recoveryRunId",
    "source",
    "actionType",
    "assignedEmails",
    "script",
    "subject",
    "askFields",
    "rowNumber",
]


def clone_known_input():
    return {
        **KNOWN_INPUT,
        "outbox": {
            **KNOWN_INPUT["outbox"],
            "assignedEmails": list(KNOWN_INPUT["outbox"]["assignedEmails"]),
            "askFields": list(KNOWN_INPUT["outbox"]["askFields"]),
        },
    }


class RecoveryPayloadTests(unittest.TestCase):
    def setUp(self):
        if _IMPORT_ERROR is not None:
            self.fail(f"recovery payload module must be implemented: {_IMPORT_ERROR}")

    def test_builds_shared_normalized_n1_payload_in_fixed_key_order(self):
        payload = build_canonical_recovery_payload(clone_known_input())

        self.assertEqual(EXPECTED_PAYLOAD, payload)
        self.assertEqual(PAYLOAD_KEYS, list(payload))

    def test_emits_shared_compact_utf8_vector_and_exact_hashes(self):
        payload = build_canonical_recovery_payload(clone_known_input())
        serialized = serialize_canonical_recovery_payload(payload)

        self.assertIsInstance(serialized, bytes)
        self.assertEqual(EXPECTED_BYTES, serialized)
        self.assertEqual(EXPECTED_PAYLOAD_HASH, hash_recovery_payload(payload))
        self.assertEqual(EXPECTED_SCRIPT_HASH, hash_recovery_script(payload["script"]))

    def test_normalizes_crlf_and_cr_without_changing_other_script_content(self):
        test_input = clone_known_input()
        test_input["outbox"]["script"] = "  first\r\nsecond\rthird\n  "

        payload = build_canonical_recovery_payload(test_input)

        self.assertEqual("  first\nsecond\nthird\n  ", payload["script"])

    def test_normalizes_target_and_deduplicates_ask_fields(self):
        payload = build_canonical_recovery_payload(clone_known_input())

        self.assertEqual(["broker@example.test"], payload["assignedEmails"])
        self.assertEqual(["Rent/SF/YR", "CAP Rate"], payload["askFields"])

    def test_accepts_null_row_number_and_empty_ask_fields(self):
        test_input = clone_known_input()
        test_input["outbox"]["rowNumber"] = None
        test_input["outbox"]["askFields"] = []

        payload = build_canonical_recovery_payload(test_input)

        self.assertIsNone(payload["rowNumber"])
        self.assertEqual([], payload["askFields"])

    def test_rejects_missing_malformed_and_path_unsafe_ids(self):
        for key in ("uid", "clientId", "outboxId", "recoveryRunId"):
            for value in (None, "", "bad/id", "contains space", "a" * 129, 7):
                with self.subTest(key=key, value=value):
                    test_input = clone_known_input()
                    test_input[key] = value
                    with self.assertRaises((TypeError, ValueError)):
                        build_canonical_recovery_payload(test_input)

            with self.subTest(key=key, value="missing"):
                test_input = clone_known_input()
                del test_input[key]
                with self.assertRaises((TypeError, ValueError)):
                    build_canonical_recovery_payload(test_input)

    def test_rejects_zero_multiple_malformed_or_non_list_recipients(self):
        for value in (
            [],
            ["one@example.test", "two@example.test"],
            "one@example.test",
            ["not-an-email"],
            ["bad@example"],
            ["line\nbreak@example.test"],
            [7],
        ):
            with self.subTest(value=value):
                test_input = clone_known_input()
                test_input["outbox"]["assignedEmails"] = value
                with self.assertRaises((TypeError, ValueError)):
                    build_canonical_recovery_payload(test_input)

    def test_rejects_empty_whitespace_only_or_non_string_scripts(self):
        for value in ("", "  \t\n", 7, None):
            with self.subTest(value=value):
                test_input = clone_known_input()
                test_input["outbox"]["script"] = value
                with self.assertRaises((TypeError, ValueError)):
                    build_canonical_recovery_payload(test_input)

    def test_rejects_multiline_overlong_or_non_string_subjects(self):
        for value in (
            "",
            "  \t ",
            "first\nsecond",
            "first\rsecond",
            "x" * 256,
            7,
            None,
        ):
            with self.subTest(value=value):
                test_input = clone_known_input()
                test_input["outbox"]["subject"] = value
                with self.assertRaises((TypeError, ValueError)):
                    build_canonical_recovery_payload(test_input)

    def test_rejects_malformed_ask_fields(self):
        for value in ("Rent", [""], ["   "], [7], ["field"] * 251):
            with self.subTest(value_type=type(value).__name__, size=len(value)):
                test_input = clone_known_input()
                test_input["outbox"]["askFields"] = value
                with self.assertRaises((TypeError, ValueError)):
                    build_canonical_recovery_payload(test_input)

    def test_rejects_non_positive_and_non_integer_row_numbers(self):
        for value in (0, -1, 1.5, True, "1", float("inf"), 2**53):
            with self.subTest(value=value):
                test_input = clone_known_input()
                test_input["outbox"]["rowNumber"] = value
                with self.assertRaises((TypeError, ValueError)):
                    build_canonical_recovery_payload(test_input)

    def test_rejects_every_extra_top_level_property(self):
        for key in (
            "injected",
            "followUp",
            "cc",
            "replyTo",
            "threadId",
            "source",
            "actionType",
            "status",
            "payloadHash",
            "scriptHash",
        ):
            with self.subTest(key=key):
                test_input = clone_known_input()
                test_input[key] = "hostile-synthetic-value"
                with self.assertRaises((TypeError, ValueError)):
                    build_canonical_recovery_payload(test_input)

    def test_rejects_extra_outbox_properties_and_effect_lanes(self):
        for key in (
            "injected",
            "uid",
            "followUp",
            "nextFollowUp",
            "cc",
            "bcc",
            "replyTo",
            "reply",
            "threadId",
            "source",
            "actionType",
            "status",
            "recoveryProfile",
            "recoveryPayloadHash",
        ):
            with self.subTest(key=key):
                test_input = clone_known_input()
                test_input["outbox"][key] = "hostile-synthetic-value"
                with self.assertRaises((TypeError, ValueError)):
                    build_canonical_recovery_payload(test_input)

    def test_rejects_missing_top_level_and_outbox_fields(self):
        missing_outbox = clone_known_input()
        del missing_outbox["outbox"]
        with self.assertRaises((TypeError, ValueError)):
            build_canonical_recovery_payload(missing_outbox)

        for key in ("assignedEmails", "script", "subject", "askFields", "rowNumber"):
            with self.subTest(key=key):
                test_input = clone_known_input()
                del test_input["outbox"][key]
                with self.assertRaises((TypeError, ValueError)):
                    build_canonical_recovery_payload(test_input)

    def test_serializer_rejects_injected_or_noncanonical_payload_objects(self):
        payload = build_canonical_recovery_payload(clone_known_input())

        with self.assertRaises((TypeError, ValueError)):
            serialize_canonical_recovery_payload({**payload, "injected": True})
        with self.assertRaises((TypeError, ValueError)):
            serialize_canonical_recovery_payload({**payload, "source": "other"})
        with self.assertRaises((TypeError, ValueError)):
            serialize_canonical_recovery_payload(
                {**payload, "assignedEmails": [" Broker@Example.TEST "]}
            )

    def test_hashes_normalized_raw_script(self):
        raw_script = "Hi Test,\r\n\rAvailability for Café 🏠?\rThanks."
        self.assertEqual(EXPECTED_SCRIPT_HASH, hash_recovery_script(raw_script))

    def test_payload_hashing_accepts_canonical_payloads_only(self):
        canonical = build_canonical_recovery_payload(clone_known_input())

        self.assertEqual(EXPECTED_PAYLOAD_HASH, hash_recovery_payload(canonical))
        with self.assertRaises((TypeError, ValueError)):
            hash_recovery_payload(clone_known_input())
        with self.assertRaises((TypeError, ValueError)):
            hash_recovery_payload({**canonical, "subject": " padded subject "})

    def test_requires_plain_top_level_outbox_and_canonical_payload_mappings(self):
        class CustomMapping(dict):
            pass

        for value in (None, [], (), CustomMapping(clone_known_input())):
            with self.subTest(location="top", value_type=type(value).__name__):
                with self.assertRaises((TypeError, ValueError)):
                    build_canonical_recovery_payload(value)

        for value in ([], (), CustomMapping(clone_known_input()["outbox"])):
            with self.subTest(location="outbox", value_type=type(value).__name__):
                test_input = clone_known_input()
                test_input["outbox"] = value
                with self.assertRaises((TypeError, ValueError)):
                    build_canonical_recovery_payload(test_input)

        for value in (None, [], (), CustomMapping(EXPECTED_PAYLOAD)):
            with self.subTest(location="payload", value_type=type(value).__name__):
                with self.assertRaises((TypeError, ValueError)):
                    serialize_canonical_recovery_payload(value)


if __name__ == "__main__":
    unittest.main()
