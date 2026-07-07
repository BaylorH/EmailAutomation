"""Flag-routing + safety tests for the DECOMPOSE_PROMPT prompt-decomposition path.

Phase 4 scaffolding is functionality-neutral: with the flag OFF (default),
propose_sheet_updates must run the unchanged single-call gpt-5.2 monolith; with
the flag ON it must run the decomposed sub-call pipeline that produces the SAME
post-parse proposal dict {updates, events, response_email, notes}.

No live API: the OpenAI client and usage metering are mocked throughout.
"""

import os
import unittest
from unittest import mock

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "service-account.json"),
)

from email_automation import ai_processing as ai  # noqa: E402


def _base_kwargs(**over):
    kw = dict(
        uid="u", client_id="c", email="broker@example.com", sheet_id="s",
        header=["Property Address", "Rent/SF /Yr"], rownum=3,
        rowvals=["1 Randolph Ct", ""], thread_id="t",
        conversation=[{"direction": "inbound", "from": "broker@example.com",
                       "content": "Still available."}],
        contact_name=None, dry_run=True,
    )
    kw.update(over)
    return kw


def _fake_client(output_text='{"updates": [], "events": [], "response_email": null, "notes": ""}'):
    resp = mock.Mock()
    resp.output_text = output_text
    resp.usage = None
    resp.id = "resp_test"
    fake = mock.Mock()
    fake.responses.create.return_value = resp
    return fake


# ---------------------------------------------------------------------------
# Flag reader
# ---------------------------------------------------------------------------
class FlagReaderTests(unittest.TestCase):
    def test_default_off_when_unset(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DECOMPOSE_PROMPT", None)
            self.assertFalse(ai._decompose_prompt_enabled())

    def test_truthy_values_enable(self):
        for val in ("1", "true", "TRUE", "Yes", "on"):
            with mock.patch.dict(os.environ, {"DECOMPOSE_PROMPT": val}):
                self.assertTrue(ai._decompose_prompt_enabled(), val)

    def test_falsy_values_stay_off(self):
        for val in ("", "0", "false", "no", "off", "nope"):
            with mock.patch.dict(os.environ, {"DECOMPOSE_PROMPT": val}):
                self.assertFalse(ai._decompose_prompt_enabled(), val)


# ---------------------------------------------------------------------------
# Routing: OFF -> monolith, ON -> decomposed pipeline
# ---------------------------------------------------------------------------
class RoutingTests(unittest.TestCase):
    def test_flag_off_calls_monolith_gpt52_not_pipeline(self):
        fake = _fake_client()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DECOMPOSE_PROMPT", None)
            with mock.patch.object(ai, "client", fake), \
                 mock.patch.object(ai, "track_openai_usage_safely") as track, \
                 mock.patch.object(ai, "_run_decomposed_pipeline") as pipeline:
                out = ai.propose_sheet_updates(**_base_kwargs())

        # Monolith single-call path was taken.
        self.assertEqual(fake.responses.create.call_count, 1)
        self.assertEqual(fake.responses.create.call_args.kwargs["model"], "gpt-5.2")
        # Decomposed pipeline was NOT entered.
        pipeline.assert_not_called()
        # Monolith metered with its historical operation label.
        self.assertEqual(track.call_args.kwargs["operation"], "ai.extract_sheet_updates")
        self.assertIsInstance(out, dict)

    def test_flag_on_calls_pipeline_not_monolith_gpt52(self):
        fake = _fake_client()
        assembled = {"updates": [], "events": [], "response_email": None, "notes": ""}
        with mock.patch.dict(os.environ, {"DECOMPOSE_PROMPT": "1"}):
            with mock.patch.object(ai, "client", fake), \
                 mock.patch.object(ai, "_run_decomposed_pipeline",
                                   return_value=assembled) as pipeline:
                out = ai.propose_sheet_updates(**_base_kwargs())

        # Decomposed pipeline was entered exactly once.
        pipeline.assert_called_once()
        # Monolith gpt-5.2 single-call path was NOT taken.
        fake.responses.create.assert_not_called()
        # Output still flows through the shared guard ladder and stays a dict
        # carrying the contract keys.
        self.assertIsInstance(out, dict)
        for key in ("updates", "events", "response_email", "notes"):
            self.assertIn(key, out)


# ---------------------------------------------------------------------------
# Pipeline wiring + pure assembly (no LLM)
# ---------------------------------------------------------------------------
class PipelineWiringTests(unittest.TestCase):
    def _pipe_kwargs(self, **over):
        kw = dict(
            event_rules="EVENT_RULES", column_rules="COLUMN_RULES",
            doc_selection_rules="DOC", notes_rules="NOTES",
            response_email_rules="REPLY", target_anchor="1 Randolph Ct",
            last_human_message="Still available.", contact_context="",
            missing_fields=[], header=["Property Address"], rowvals=["x"],
            rownum=3, conversation=[{"direction": "inbound", "content": "hi"}],
            pdf_manifest=None, url_texts=None, extraction_fields=None,
            uid="u", client_id="c", thread_id="t", sheet_id="s",
        )
        kw.update(over)
        return kw

    def test_pipeline_invokes_each_subcall_and_assembles(self):
        with mock.patch.object(ai, "_classify_intent",
                               return_value=[{"type": "call_requested"}]) as ci, \
             mock.patch.object(ai, "_extract_fields",
                               return_value=[{"column": "Rent/SF /Yr", "value": "12.00",
                                              "confidence": 0.9, "reason": "flyer"}]) as ef, \
             mock.patch.object(ai, "_write_notes", return_value="NNN • available") as wn, \
             mock.patch.object(ai, "_draft_reply", return_value="Hi, thanks.") as dr:
            out = ai._run_decomposed_pipeline(**self._pipe_kwargs())

        ci.assert_called_once()
        ef.assert_called_once()
        wn.assert_called_once()
        dr.assert_called_once()
        # notes sub-call is fed the extracted updates (redundancy avoidance).
        self.assertEqual(wn.call_args.kwargs["updates"], ef.return_value)
        # draft sub-call is fed the classified events (gating).
        self.assertEqual(dr.call_args.kwargs["events"], ci.return_value)
        # Pure assembly matches the monolith post-parse contract shape.
        self.assertEqual(out, {
            "updates": [{"column": "Rent/SF /Yr", "value": "12.00",
                         "confidence": 0.9, "reason": "flyer"}],
            "events": [{"type": "call_requested"}],
            "response_email": "Hi, thanks.",
            "notes": "NNN • available",
        })

    def test_pipeline_degrades_to_safe_proposal_when_subcall_raises(self):
        with mock.patch.object(ai, "_classify_intent", return_value=[]), \
             mock.patch.object(ai, "_extract_fields", side_effect=RuntimeError("boom")):
            out = ai._run_decomposed_pipeline(**self._pipe_kwargs())
        # Never raises; returns a valid empty proposal so the downstream guard
        # ladder still runs.
        self.assertEqual(out, {"updates": [], "events": [], "response_email": None, "notes": ""})

    def test_assemble_coerces_bad_types(self):
        out = ai._assemble_proposal(updates=None, events="oops",
                                    response_email=123, notes=None)
        self.assertEqual(out, {"updates": [], "events": [], "response_email": None, "notes": ""})


# ---------------------------------------------------------------------------
# Sub-call defensive degradation + metering (no live API)
# ---------------------------------------------------------------------------
class SubCallSafetyTests(unittest.TestCase):
    def test_classify_intent_parses_and_meters(self):
        fake = _fake_client('{"events": [{"type": "call_requested"}]}')
        with mock.patch.object(ai, "client", fake), \
             mock.patch.object(ai, "track_openai_usage_safely") as track:
            events = ai._classify_intent(
                event_rules="RULES", target_anchor="1 Randolph Ct",
                last_human_message="Call me", conversation=[{"direction": "inbound", "content": "Call me"}],
                uid="u", client_id="c", thread_id="t", sheet_id="s", rownum=3,
            )
        self.assertEqual(events, [{"type": "call_requested"}])
        self.assertEqual(fake.responses.create.call_args.kwargs["model"], "gpt-4o-mini")
        self.assertEqual(track.call_args.kwargs["operation"], "ai.classify_intent")
        self.assertEqual(track.call_args.kwargs["model"], "gpt-4o-mini")

    def test_classify_intent_returns_empty_on_client_error(self):
        fake = mock.Mock()
        fake.responses.create.side_effect = RuntimeError("network down")
        with mock.patch.object(ai, "client", fake):
            events = ai._classify_intent(
                event_rules="RULES", target_anchor="x", last_human_message="hi",
                conversation=[], uid="u", client_id="c", thread_id="t",
                sheet_id="s", rownum=3,
            )
        self.assertEqual(events, [])

    def test_extract_fields_returns_empty_on_bad_json(self):
        fake = _fake_client("not json at all")
        with mock.patch.object(ai, "client", fake), \
             mock.patch.object(ai, "track_openai_usage_safely"):
            updates = ai._extract_fields(
                column_rules="C", doc_selection_rules="D", header=["A"], rowvals=["x"],
                rownum=3, missing_fields=[], target_anchor="x", conversation=[],
                pdf_manifest=None, url_texts=None, uid="u", client_id="c",
                thread_id="t", sheet_id="s", extraction_fields=None,
            )
        self.assertEqual(updates, [])

    def test_extract_fields_preserves_multimodal_input(self):
        fake = _fake_client('{"updates": []}')
        pdf_manifest = [{"name": "flyer.pdf", "text": "Asking $12/SF NNN",
                         "images": ["QUJD", "REVG"], "method": "openai_upload+images",
                         "id": "file_123"}]
        with mock.patch.object(ai, "client", fake), \
             mock.patch.object(ai, "track_openai_usage_safely"):
            ai._extract_fields(
                column_rules="C", doc_selection_rules="D", header=["A"], rowvals=["x"],
                rownum=3, missing_fields=[], target_anchor="x", conversation=[],
                pdf_manifest=pdf_manifest, url_texts=None, uid="u", client_id="c",
                thread_id="t", sheet_id="s", extraction_fields=None,
            )
        content = fake.responses.create.call_args.kwargs["input"][0]["content"]
        kinds = [part["type"] for part in content]
        # Two page images (capped) + input_file fallback + the text prompt.
        self.assertEqual(kinds.count("input_image"), 2)
        self.assertIn("input_file", kinds)
        self.assertEqual(kinds[-1], "input_text")

    def test_draft_reply_gate_skips_model_for_null_reply_intents(self):
        fake = _fake_client('{"response_email": "should not be used"}')
        for etype in ("needs_user_input", "contact_optout", "wrong_contact", "tour_requested"):
            with mock.patch.object(ai, "client", fake), \
                 mock.patch.object(ai, "track_openai_usage_safely"):
                out = ai._draft_reply(
                    response_email_rules="R", events=[{"type": etype}], contact_context="",
                    target_anchor="x", missing_fields=[], last_human_message="hi",
                    conversation=[], uid="u", client_id="c", thread_id="t",
                    sheet_id="s", rownum=3,
                )
            self.assertIsNone(out, etype)
        # Gate short-circuited before any model call.
        fake.responses.create.assert_not_called()

    def test_draft_reply_calls_gpt52_when_permitted(self):
        fake = _fake_client('{"response_email": "Hi, thanks for the details."}')
        with mock.patch.object(ai, "client", fake), \
             mock.patch.object(ai, "track_openai_usage_safely") as track:
            out = ai._draft_reply(
                response_email_rules="R", events=[], contact_context="",
                target_anchor="x", missing_fields=["Total SF"], last_human_message="hi",
                conversation=[], uid="u", client_id="c", thread_id="t",
                sheet_id="s", rownum=3,
            )
        self.assertEqual(out, "Hi, thanks for the details.")
        self.assertEqual(fake.responses.create.call_args.kwargs["model"], "gpt-5.2")
        self.assertEqual(track.call_args.kwargs["operation"], "ai.draft_reply")


class ReplyGateTests(unittest.TestCase):
    def test_empty_events_permit_reply(self):
        self.assertTrue(ai._reply_permitted_by_intent([]))

    def test_null_reply_events_block(self):
        for etype in ("needs_user_input", "contact_optout", "wrong_contact", "tour_requested"):
            self.assertFalse(ai._reply_permitted_by_intent([{"type": etype}]), etype)

    def test_close_conversation_all_info_gathered_permits(self):
        self.assertTrue(ai._reply_permitted_by_intent(
            [{"type": "close_conversation", "notes": "all_info_gathered"}]))

    def test_close_conversation_other_reason_blocks(self):
        self.assertFalse(ai._reply_permitted_by_intent(
            [{"type": "close_conversation", "notes": "natural_end"}]))

    def test_call_and_new_property_permit(self):
        self.assertTrue(ai._reply_permitted_by_intent([{"type": "call_requested"}]))
        self.assertTrue(ai._reply_permitted_by_intent([{"type": "new_property"}]))


if __name__ == "__main__":
    unittest.main()
