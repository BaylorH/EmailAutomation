import unittest
from unittest import mock

from email_automation.effect_gateway import (
    AttemptLimits,
    EffectGatewayConfig,
    ReceiptState,
)
from email_automation.graph_final_send import (
    GraphFinalSendAdapter,
    execute_graph_draft_final_send,
    execute_graph_final_send,
)
from tests.test_effect_gateway import InMemoryReceiptStore


class FakeResponse:
    def __init__(self, status_code=202):
        self.status_code = status_code
        self.headers = {}
        self.text = ""


class GraphFinalSendContractTests(unittest.TestCase):
    def setUp(self):
        self.config = EffectGatewayConfig(
            enabled=True,
            global_effects_enabled=True,
            limits=AttemptLimits(
                max_attempts=3,
                max_per_run=10,
                max_per_user=10,
                max_per_provider=10,
            ),
        )

    def test_adapter_executes_exactly_one_final_send_and_returns_reference(self):
        calls = []
        adapter = GraphFinalSendAdapter(
            lambda: calls.append("send") or FakeResponse(),
            provider_reference="graph-draft-secret",
        )

        result = adapter.execute(object())

        self.assertEqual(calls, ["send"])
        self.assertEqual(result.provider_reference, "graph-draft-secret")

    def test_production_draft_wrapper_uses_one_raw_post_without_retry(self):
        with mock.patch(
            "email_automation.graph_final_send.requests.post",
            return_value=FakeResponse(),
        ) as post:
            outcome = execute_graph_draft_final_send(
                user_id="uid-exact",
                client_id="client-exact",
                run_id="run-exact",
                effect_type="mail.send",
                effect_key="outbox-doc-1",
                content={"body": "hello", "to": ["broker@example.test"]},
                draft_id="graph-draft-secret",
                headers={"Authorization": "Bearer test"},
                receipt_store=InMemoryReceiptStore(),
                config=self.config,
            )

        self.assertTrue(outcome.accepted)
        post.assert_called_once_with(
            "https://graph.microsoft.com/v1.0/me/messages/"
            "graph-draft-secret/send",
            headers={"Authorization": "Bearer test"},
            timeout=30,
        )

    def test_gateway_authority_read_is_immediately_before_final_send(self):
        events = []
        store = InMemoryReceiptStore(events)

        outcome = execute_graph_final_send(
            user_id="uid-exact",
            client_id="client-exact",
            run_id="run-exact",
            effect_type="mail.reply",
            effect_key="thread-1:message-1",
            content={"body": "hello", "to": ["broker@example.test"]},
            provider_reference="graph-draft-secret",
            send_call=lambda: events.append(("graph_final_send",))
            or FakeResponse(),
            receipt_store=store,
            config=self.config,
        )

        self.assertEqual(outcome.receipt.state, ReceiptState.SUCCEEDED)
        authoritative_index = next(
            index
            for index, event in enumerate(events)
            if event[0] == "authoritative_read"
        )
        self.assertEqual(
            events[authoritative_index + 1],
            ("graph_final_send",),
        )

    def test_successful_retry_uses_receipt_without_second_provider_call(self):
        store = InMemoryReceiptStore()
        calls = []
        arguments = {
            "user_id": "uid-exact",
            "client_id": "client-exact",
            "run_id": "run-exact",
            "effect_type": "mail.send",
            "effect_key": "outbox-doc-1",
            "content": {
                "body": "hello",
                "to": ["broker@example.test"],
            },
            "provider_reference": "graph-draft-secret",
            "send_call": lambda: calls.append("send") or FakeResponse(),
            "receipt_store": store,
            "config": self.config,
        }

        first = execute_graph_final_send(**arguments)
        second = execute_graph_final_send(**arguments)

        self.assertEqual(first.receipt.state, ReceiptState.SUCCEEDED)
        self.assertEqual(second.receipt.state, ReceiptState.SUCCEEDED)
        self.assertTrue(first.accepted)
        self.assertFalse(second.accepted)
        self.assertTrue(second.already_applied)
        self.assertEqual(calls, ["send"])
        self.assertNotIn(
            "graph-draft-secret",
            second.receipt.provider_reference,
        )

    def test_http_rejection_is_terminal_and_network_uncertainty_reconciles(self):
        terminal = execute_graph_final_send(
            user_id="uid-exact",
            client_id="client-exact",
            run_id="run-exact",
            effect_type="mail.send",
            effect_key="terminal",
            content={"body": "hello"},
            provider_reference="draft-terminal",
            send_call=lambda: FakeResponse(400),
            receipt_store=InMemoryReceiptStore(),
            config=self.config,
        )

        timed_out = execute_graph_final_send(
            user_id="uid-exact",
            client_id="client-exact",
            run_id="run-exact",
            effect_type="mail.send",
            effect_key="http-timeout",
            content={"body": "hello"},
            provider_reference="draft-http-timeout",
            send_call=lambda: FakeResponse(408),
            receipt_store=InMemoryReceiptStore(),
            config=self.config,
        )

        def uncertain_send():
            raise TimeoutError("provider outcome unknown")

        uncertain = execute_graph_final_send(
            user_id="uid-exact",
            client_id="client-exact",
            run_id="run-exact",
            effect_type="mail.send",
            effect_key="uncertain",
            content={"body": "hello"},
            provider_reference="draft-uncertain",
            send_call=uncertain_send,
            receipt_store=InMemoryReceiptStore(),
            config=self.config,
        )

        self.assertEqual(
            terminal.receipt.state,
            ReceiptState.TERMINAL_FAILED,
        )
        self.assertEqual(terminal.receipt.reason, "provider_terminal")
        self.assertEqual(
            timed_out.receipt.state,
            ReceiptState.RECONCILIATION_REQUIRED,
        )
        self.assertEqual(
            timed_out.receipt.reason,
            "provider_outcome_unknown",
        )
        self.assertEqual(
            uncertain.receipt.state,
            ReceiptState.RECONCILIATION_REQUIRED,
        )
        self.assertEqual(
            uncertain.receipt.reason,
            "provider_outcome_unknown",
        )


if __name__ == "__main__":
    unittest.main()
