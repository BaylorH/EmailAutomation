import os

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "service-account.json"),
)

import unittest
from unittest.mock import patch

from email_automation import email as email_module
from email_automation import processing as processing_module


class _FakeRef:
    """Minimal outbox document reference: records datastore-boundary calls."""

    def __init__(self, doc_id):
        self.id = doc_id
        self.deleted = False
        self.set_calls = []

    def delete(self):
        self.deleted = True

    def set(self, payload, merge=False):
        self.set_calls.append((payload, merge))


class _FakeDoc:
    def __init__(self, ref):
        self.reference = ref
        # #20 send-failure observability records item['doc'].id when a send
        # attempt raises; mirror the ref id so the fake carries a doc id.
        self.id = ref.id


class _ResolverReached(Exception):
    """Sentinel raised by the name-resolution spy to prove it was invoked."""


class CoreNameResolutionTerminalStateTests(unittest.TestCase):
    """Rubric cell: feature=core.name_resolution, class=terminal_state.

    Proves that the REAL production launch-send loop
    email._send_multi_property_email performs NO name resolution / launch
    personalization for a terminal (cancelled/stopped) outbox thread: the
    cancellation gate short-circuits the item before the contact-name resolver
    is ever consulted. A live (non-terminal) item is used as the negative
    control to show the same loop DOES reach the resolver -- so the assertion
    is discriminating, not vacuously true.

    Only the datastore/Graph boundary collaborators are patched (opt-out lookup,
    claim, refresh, pause guard, recipient guard, duplicate guard, retry/dead-
    letter audit). The terminal-state gate and control flow under test run for
    real, and the name-resolution collaborator is a spy whose invocation count
    is the measured behavior.
    """

    def _drive(self, item_data, doc_id):
        ref = _FakeRef(doc_id)
        item = {"doc": _FakeDoc(ref), "data": item_data}

        resolver_spy = patch.object(
            email_module,
            "_resolve_campaign_launch_contact_name_result_from_sheet",
            side_effect=_ResolverReached(),
        )

        with resolver_spy as spy, \
             patch.object(processing_module, "is_contact_opted_out", return_value=None), \
             patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_get_current_outbox_data", return_value=item_data), \
             patch.object(email_module, "_pause_client_outbox_item_if_needed", return_value=False), \
             patch.object(email_module, "_dead_letter_campaign_recipient_row_mismatch_if_needed", return_value=False), \
             patch.object(email_module, "_has_existing_thread_for_property", return_value=False), \
             patch.object(email_module, "_mark_outbox_action_audit_retrying", return_value=None), \
             patch.object(email_module, "_move_to_dead_letter", return_value=None):
            email_module._send_multi_property_email(
                "uid-terminal",
                {"Authorization": "Bearer x"},
                "broker@example.com",
                [item],
            )
        return ref, spy

    def test_terminal_thread_gets_no_name_resolution_but_live_thread_does(self):
        # --- Terminal case: a cancelled campaign-launch outbox item. ---
        # Script contains an unresolved [NAME] placeholder and no contactName,
        # so IF the loop reached resolution it WOULD call the resolver.
        terminal_data = {
            "status": "cancelled",
            "clientId": "client-1",
            "rowNumber": 12,
            "source": "dashboard_new_campaign",
            "actionType": "campaign_creation",
            "subject": "123 Main St",
            "script": "Hi [NAME], I'm interested in 123 Main St.",
        }
        terminal_ref, terminal_spy = self._drive(terminal_data, "outbox-terminal")

        # The terminal item was terminalized (deleted) by the cancellation gate...
        self.assertTrue(
            terminal_ref.deleted,
            "cancelled (terminal) outbox item must be deleted by the gate",
        )
        # ...and name resolution NEVER ran for it.
        self.assertEqual(
            0,
            terminal_spy.call_count,
            "no name resolution may occur for a terminal/stopped thread",
        )

        # --- Negative control: an identical but LIVE item. ---
        live_data = dict(terminal_data)
        live_data.pop("status")  # not cancelled -> active thread
        live_ref, live_spy = self._drive(live_data, "outbox-live")

        # The live item is NOT deleted by the cancellation gate...
        self.assertFalse(
            live_ref.deleted,
            "a live outbox item must not be treated as terminal",
        )
        # ...and the SAME loop DOES reach the resolver, proving the terminal
        # skip above is a real gate and not a vacuous no-op path.
        self.assertEqual(
            1,
            live_spy.call_count,
            "a live thread with an unresolved [NAME] must trigger name resolution",
        )


if __name__ == "__main__":
    unittest.main()
