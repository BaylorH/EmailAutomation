import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCANNER_PATH = REPO_ROOT / "scripts" / "scan_clearance_evidence_pii.py"
STATE_PATH = REPO_ROOT / "docs" / "release-safety" / "production-clearance-state.json"
CHECKPOINT_PATH = (
    REPO_ROOT / "docs" / "release-safety" / "production-clearance-checkpoints.jsonl"
)


class ClearanceEvidencePiiScannerTests(unittest.TestCase):
    def _scan_payload(self, payload, **kwargs):
        from scripts.scan_clearance_evidence_pii import scan_evidence_paths

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "evidence.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            return scan_evidence_paths([path], **kwargs)

    def test_rejects_email_addresses_without_echoing_the_value(self):
        sensitive_email = "@".join(("runtime-recipient", "private.invalid"))
        findings = self._scan_payload({"graphReceipt": sensitive_email})

        self.assertEqual(["email_address"], [finding.kind for finding in findings])
        self.assertNotIn(sensitive_email, "\n".join(finding.render() for finding in findings))

    def test_rejects_email_addresses_in_object_keys_without_echoing_the_key(self):
        sensitive_email = "@".join(("runtime-key", "private.invalid"))
        findings = self._scan_payload({sensitive_email: "opaque_value"})

        self.assertEqual(["email_address"], [finding.kind for finding in findings])
        self.assertNotIn(sensitive_email, "\n".join(finding.render() for finding in findings))

    def test_rejects_raw_message_body_fields(self):
        raw_key = "".join(("message", "Body"))
        findings = self._scan_payload({raw_key: "runtime-only sensitive prose"})

        self.assertEqual(["raw_message_body"], [finding.kind for finding in findings])

    def test_rejects_names_from_runtime_deny_list(self):
        sensitive_name = " ".join(("Runtime", "Denylisted"))
        findings = self._scan_payload(
            {"operatorAlias": sensitive_name}, denied_names=[sensitive_name]
        )

        self.assertEqual(["personal_name"], [finding.kind for finding in findings])
        punctuated_name = ", ".join(sensitive_name.split())
        self.assertEqual(
            ["personal_name"],
            [
                finding.kind
                for finding in self._scan_payload(
                    {"operatorAlias": punctuated_name},
                    denied_names=[sensitive_name],
                )
            ],
        )

    def test_rejects_unapproved_property_address_and_allows_approved_one(self):
        sensitive_address = " ".join(("42", "Hidden", "Street"))
        payload = {"propertyEvidence": sensitive_address}

        findings = self._scan_payload(payload)
        self.assertEqual(["property_address"], [finding.kind for finding in findings])
        self.assertEqual(
            [],
            self._scan_payload(payload, approved_addresses=[sensitive_address]),
        )
        qualified_address = ", ".join(
            (sensitive_address, "".join(("Synthetic", "ville")))
        )
        self.assertEqual(
            [],
            self._scan_payload(
                {"propertyEvidence": qualified_address},
                approved_addresses=[qualified_address],
            ),
        )

    def test_allows_hashes_opaque_ids_role_aliases_counts_and_scenario_ids(self):
        body_hash = hashlib.sha256(b"sanitized synthetic body").hexdigest()
        payload = {
            "exactBodyHashes": [body_hash],
            "eventId": "evt_01JABCDEF23456789",
            "actorAlias": "broker_role_1",
            "messageCount": 3,
            "browserScenarioIds": ["availability.quoted-correction.001"],
            "graphReceipt": "no_send",
        }

        self.assertEqual([], self._scan_payload(payload))

    def test_malformed_json_is_a_fail_closed_finding(self):
        from scripts.scan_clearance_evidence_pii import scan_evidence_paths

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "evidence.json"
            path.write_text("{", encoding="utf-8")
            findings = scan_evidence_paths([path])

        self.assertEqual(["parse_error"], [finding.kind for finding in findings])

    def test_non_utf8_evidence_is_a_fail_closed_finding(self):
        from scripts.scan_clearance_evidence_pii import scan_evidence_paths

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "evidence.json"
            path.write_bytes(bytes([255]))
            findings = scan_evidence_paths([path])

        self.assertEqual(["parse_error"], [finding.kind for finding in findings])

    def test_current_sanitized_state_and_checkpoint_ledger_pass(self):
        from scripts.scan_clearance_evidence_pii import scan_evidence_paths

        self.assertEqual([], scan_evidence_paths([STATE_PATH, CHECKPOINT_PATH]))

    def test_cli_uses_runtime_deny_list_and_exits_nonzero_on_finding(self):
        sensitive_name = " ".join(("Runtime", "Denylisted"))
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            evidence_path = temp_root / "evidence.json"
            deny_path = temp_root / "deny-names.txt"
            evidence_path.write_text(
                json.dumps({"operatorAlias": sensitive_name}), encoding="utf-8"
            )
            deny_path.write_text(sensitive_name + "\n", encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCANNER_PATH),
                    "--deny-names-file",
                    str(deny_path),
                    str(evidence_path),
                ],
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(1, result.returncode)
        self.assertEqual("", result.stdout)
        self.assertIn("personal_name", result.stderr)
        self.assertNotIn(sensitive_name, result.stderr)

    def test_cli_rejects_non_utf8_runtime_list_without_a_traceback(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            evidence_path = temp_root / "evidence.json"
            deny_path = temp_root / "deny-names.txt"
            evidence_path.write_text(json.dumps({"status": "BLOCKED"}), encoding="utf-8")
            deny_path.write_bytes(bytes([255]))
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCANNER_PATH),
                    "--deny-names-file",
                    str(deny_path),
                    str(evidence_path),
                ],
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(2, result.returncode)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
