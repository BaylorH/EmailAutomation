import contextlib
import io
import unittest

from email_automation import campaign_safety


class _FakeSnapshot:
    def __init__(self, data=None, *, exists=True):
        self._data = data
        self.exists = exists

    def to_dict(self):
        return self._data


class _FakeDocument:
    def __init__(self, collections, document_id):
        self._collections = collections
        self._document_id = document_id

    def get(self):
        value = self._collections.get(self._document_id)
        if isinstance(value, Exception):
            raise value
        if value is None:
            return _FakeSnapshot(exists=False)
        return _FakeSnapshot(value)


class _FakeCollection:
    def __init__(self, collections):
        self._collections = collections

    def document(self, document_id):
        return _FakeDocument(self._collections, document_id)


class _FakeUserDocument:
    def __init__(self, collections):
        self._collections = collections

    def collection(self, name):
        return _FakeCollection(self._collections[name])


class _FakeUsersCollection:
    def __init__(self, users):
        self._users = users

    def document(self, user_id):
        return _FakeUserDocument(self._users[user_id])


class _FakeFirestore:
    def __init__(self, users):
        self._users = users

    def collection(self, name):
        if name != "users":
            raise AssertionError(f"Unexpected root collection: {name}")
        return _FakeUsersCollection(self._users)


class _PathFirestore:
    def __init__(self, values, path=()):
        self._values = values
        self._path = path

    def collection(self, name):
        return _PathFirestore(self._values, self._path + (name,))

    def document(self, document_id):
        return _PathFirestore(self._values, self._path + (document_id,))

    def get(self):
        value = self._values.get(self._path)
        if isinstance(value, Exception):
            raise value
        if self._path not in self._values:
            return _FakeSnapshot(exists=False)
        return _FakeSnapshot(value)


class CampaignAutomationPauseTests(unittest.TestCase):
    BAYLOR_UID = "NO7lVYVp6BaplKYEfMlWCgBnpdh2"

    def _policy_firestore(self, user_id, client_data, policy):
        return _PathFirestore({
            ("users", user_id, "clients", "client-1"): client_data,
            ("systemConfig", "campaignAccess"): policy,
        })

    def test_global_maintenance_blocks_normal_active_client(self):
        decision = campaign_safety.get_client_automation_decision(
            "normal-user",
            "client-1",
            firestore_client=self._policy_firestore(
                "normal-user",
                {"status": "active"},
                {"automationEnabled": False, "allowedUids": [self.BAYLOR_UID]},
            ),
        )

        self.assertEqual(campaign_safety.CAMPAIGN_AUTOMATION_BLOCKED, decision.state)
        self.assertEqual("global_automation_disabled", decision.reason)
        self.assertFalse(decision.metadata["terminal"])

    def test_global_maintenance_allows_allowlisted_baylor_active_client(self):
        decision = campaign_safety.get_client_automation_decision(
            self.BAYLOR_UID,
            "client-1",
            firestore_client=self._policy_firestore(
                self.BAYLOR_UID,
                {"status": "active"},
                {"automationEnabled": False, "allowedUids": [self.BAYLOR_UID]},
            ),
        )

        self.assertEqual(campaign_safety.CAMPAIGN_AUTOMATION_ALLOW, decision.state)

    def test_unreadable_or_malformed_global_config_is_unknown_and_fail_closed(self):
        policies = (
            RuntimeError("Firestore unavailable"),
            {"automationEnabled": "false", "allowedUids": [self.BAYLOR_UID]},
            {"automationEnabled": False, "allowedUids": "not-a-list"},
        )

        for policy in policies:
            with self.subTest(policy=policy), contextlib.redirect_stdout(io.StringIO()):
                decision = campaign_safety.get_client_automation_decision(
                    "normal-user",
                    "client-1",
                    firestore_client=self._policy_firestore(
                        "normal-user", {"status": "active"}, policy
                    ),
                )

            self.assertEqual(campaign_safety.CAMPAIGN_AUTOMATION_UNKNOWN, decision.state)
            self.assertTrue(decision.denies_autonomous_work)

    def test_terminal_client_remains_terminal_when_global_config_is_malformed(self):
        decision = campaign_safety.get_client_automation_decision(
            "normal-user",
            "client-1",
            firestore_client=self._policy_firestore(
                "normal-user",
                {"status": "stopped", "statusReason": "operator_stop"},
                {"automationEnabled": "invalid"},
            ),
        )

        self.assertEqual(campaign_safety.CAMPAIGN_AUTOMATION_BLOCKED, decision.state)
        self.assertEqual("operator_stop", decision.reason)
        self.assertTrue(decision.metadata["terminal"])

    def test_explicit_automation_pause_blocks_client_processing(self):
        self.assertTrue(
            campaign_safety.is_client_automation_paused({
                "status": "live",
                "automationPaused": True,
                "pausedReason": "admin_incident_pause",
            })
        )

    def test_stopped_or_archived_clients_block_client_processing(self):
        self.assertTrue(campaign_safety.is_client_automation_paused({"status": "stopped"}))
        self.assertTrue(campaign_safety.is_client_automation_paused({"status": "archived"}))

    def test_live_client_without_pause_can_process(self):
        self.assertFalse(campaign_safety.is_client_automation_paused({"status": "live"}))

    def test_decision_classifies_active_terminal_and_maintenance_states(self):
        allowed = campaign_safety.classify_client_automation_state({"status": "live"})
        terminal = campaign_safety.classify_client_automation_state({"status": "completed"})
        maintenance = campaign_safety.classify_client_automation_state({
            "status": "active",
            "automationPaused": True,
            "automationPauseReason": "maintenance_window",
        })

        self.assertEqual(campaign_safety.CAMPAIGN_AUTOMATION_ALLOW, allowed.state)
        self.assertTrue(allowed.allows_autonomous_work)
        self.assertEqual(campaign_safety.CAMPAIGN_AUTOMATION_BLOCKED, terminal.state)
        self.assertFalse(terminal.allows_autonomous_work)
        self.assertEqual("terminal_stop", terminal.metadata["stopKind"])
        self.assertEqual(campaign_safety.CAMPAIGN_AUTOMATION_BLOCKED, maintenance.state)
        self.assertEqual("maintenance_pause", maintenance.metadata["stopKind"])
        self.assertFalse(maintenance.metadata["terminal"])

    def test_terminal_statuses_all_block_autonomous_work(self):
        for status in ("stopping", "stopped", "archived", "deleted", "completed"):
            with self.subTest(status=status):
                decision = campaign_safety.classify_client_automation_state({"status": status})
                self.assertEqual(campaign_safety.CAMPAIGN_AUTOMATION_BLOCKED, decision.state)
                self.assertEqual("terminal_stop", decision.metadata["stopKind"])

    def test_explicit_maintenance_pause_blocks_even_without_a_legacy_status_field(self):
        decision = campaign_safety.classify_client_automation_state({
            "automationPaused": True,
            "pauseReason": "maintenance_window",
        })

        self.assertEqual(campaign_safety.CAMPAIGN_AUTOMATION_BLOCKED, decision.state)
        self.assertEqual("maintenance_pause", decision.metadata["stopKind"])
        self.assertEqual("maintenance_window", decision.reason)

    def test_normalizes_all_pause_reason_aliases(self):
        aliases = (
            "automationPauseReason",
            "statusReason",
            "pauseReason",
            "pausedReason",
        )
        for alias in aliases:
            with self.subTest(alias=alias):
                decision = campaign_safety.classify_client_automation_state({
                    "status": "active",
                    "automationPaused": True,
                    alias: "  admin_maintenance  ",
                })
                self.assertEqual("admin_maintenance", decision.reason)
                self.assertEqual(alias, decision.metadata["reasonField"])

    def test_archived_client_lookup_blocks_when_active_doc_is_missing(self):
        firestore = _FakeFirestore({
            "user-1": {
                "clients": {},
                "archivedClients": {
                    "client-1": {
                        "status": "live",
                        "pausedReason": "operator_archived_campaign",
                    },
                },
            },
        })

        decision = campaign_safety.get_client_automation_decision(
            "user-1", "client-1", firestore_client=firestore
        )

        self.assertEqual(campaign_safety.CAMPAIGN_AUTOMATION_BLOCKED, decision.state)
        self.assertEqual("archivedClients", decision.metadata["source"])
        self.assertEqual("operator_archived_campaign", decision.reason)
        self.assertTrue(decision.metadata["terminal"])

    def test_archived_client_wins_during_non_atomic_archive_move(self):
        firestore = _FakeFirestore({
            "user-1": {
                "clients": {
                    "client-1": {"status": "live"},
                },
                "archivedClients": {
                    "client-1": {
                        "status": "live",
                        "statusReason": "operator_archived_campaign",
                    },
                },
            },
        })

        decision = campaign_safety.get_client_automation_decision(
            "user-1", "client-1", firestore_client=firestore
        )

        self.assertEqual(campaign_safety.CAMPAIGN_AUTOMATION_BLOCKED, decision.state)
        self.assertEqual("archivedClients", decision.metadata["source"])
        self.assertTrue(decision.metadata["terminal"])

    def test_archived_client_is_terminal_even_when_legacy_fields_are_malformed(self):
        malformed_archived_records = (
            {"status": [], "automationPaused": "false"},
            {"status": "live", "automationPaused": True, "automation_paused": False},
        )

        for archived_record in malformed_archived_records:
            with self.subTest(archived_record=archived_record):
                decision = campaign_safety.classify_client_automation_state(
                    archived_record,
                    source="archivedClients",
                )

                self.assertEqual(campaign_safety.CAMPAIGN_AUTOMATION_BLOCKED, decision.state)
                self.assertEqual("terminal_stop", decision.metadata["stopKind"])
                self.assertTrue(decision.metadata["terminal"])

    def test_shared_suppression_classifier_covers_every_decision_state(self):
        decisions = (
            (campaign_safety.CAMPAIGN_AUTOMATION_ALLOW, False, None),
            (campaign_safety.CAMPAIGN_AUTOMATION_BLOCKED, True, "terminal"),
            (campaign_safety.CAMPAIGN_AUTOMATION_BLOCKED, False, "maintenance"),
            (campaign_safety.CAMPAIGN_AUTOMATION_UNKNOWN, False, "unknown"),
        )

        for state, terminal, expected in decisions:
            with self.subTest(state=state, terminal=terminal):
                decision = campaign_safety.CampaignAutomationDecision(
                    state=state,
                    reason="test_reason",
                    client_data={},
                    metadata={"terminal": terminal},
                )
                self.assertEqual(expected, campaign_safety.campaign_suppression_kind(decision))

    def test_read_error_log_uses_stable_reason_without_raw_exception_text(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            decision = campaign_safety.get_client_automation_decision(
                "user-1",
                "client-1",
                firestore_client=_FakeFirestore({
                    "user-1": {
                        "clients": {"client-1": RuntimeError("SECRET_DATABASE_PATH")},
                        "archivedClients": {},
                    },
                }),
            )

        self.assertEqual(campaign_safety.CAMPAIGN_AUTOMATION_UNKNOWN, decision.state)
        self.assertIn(campaign_safety.CLIENT_AUTOMATION_STATE_READ_ERROR_REASON, output.getvalue())
        self.assertNotIn("SECRET_DATABASE_PATH", output.getvalue())

    def test_missing_or_unreadable_client_state_is_unknown_and_denied(self):
        missing = campaign_safety.get_client_automation_decision("user-1", None)
        malformed = campaign_safety.classify_client_automation_state({"status": []})
        missing_docs = campaign_safety.get_client_automation_decision(
            "user-1",
            "missing-client",
            firestore_client=_FakeFirestore({
                "user-1": {"clients": {}, "archivedClients": {}},
            }),
        )
        with contextlib.redirect_stdout(io.StringIO()):
            read_error = campaign_safety.get_client_automation_decision(
                "user-1",
                "client-1",
                firestore_client=_FakeFirestore({
                    "user-1": {
                        "clients": {"client-1": RuntimeError("Firestore unavailable")},
                        "archivedClients": {},
                    },
                }),
            )

        for decision in (missing, malformed, missing_docs, read_error):
            self.assertEqual(campaign_safety.CAMPAIGN_AUTOMATION_UNKNOWN, decision.state)
            self.assertFalse(decision.allows_autonomous_work)

    def test_legacy_pause_api_denies_unknown_state_without_changing_its_shape(self):
        paused, reason, client_data = campaign_safety.get_client_automation_pause(
            "user-1",
            "missing-client",
            firestore_client=_FakeFirestore({
                "user-1": {"clients": {}, "archivedClients": {}},
            }),
        )

        self.assertTrue(paused)
        self.assertEqual("client_automation_state_not_found", reason)
        self.assertEqual({}, client_data)


if __name__ == "__main__":
    unittest.main()
