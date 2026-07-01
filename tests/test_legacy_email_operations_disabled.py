import os
import sys
import types
import unittest
from unittest.mock import patch


class _FakeResponse:
    def __init__(self, payload=None, status_code=202):
        self._payload = payload or {}
        self.status_code = status_code
        self.headers = {}
        self.text = ""

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


class _EmptyFirestoreNode:
    def collection(self, *_args, **_kwargs):
        return self

    def document(self, *_args, **_kwargs):
        return self

    def where(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self

    def stream(self):
        return []


def _legacy_indexing_modules():
    utils_module = types.ModuleType("utils")
    utils_module.normalize_message_id = lambda message_id: "normalized-message-id"
    utils_module.safe_preview = lambda content, max_len=200: (content or "")[:max_len]

    messaging_module = types.ModuleType("messaging")
    messaging_module.save_thread_root = lambda *_args, **_kwargs: None
    messaging_module.save_message = lambda *_args, **_kwargs: None
    messaging_module.index_message_id = lambda *_args, **_kwargs: None
    messaging_module.index_conversation_id = lambda *_args, **_kwargs: None

    return {"utils": utils_module, "messaging": messaging_module}


class LegacyEmailOperationsDisabledTests(unittest.TestCase):
    def setUp(self):
        self._env_patch = patch.dict(os.environ, {}, clear=True)
        self._env_patch.start()
        self.addCleanup(self._env_patch.stop)

    def test_legacy_send_helpers_are_disabled_before_graph_by_default(self):
        with patch.dict(
            os.environ,
            {
                "E2E_TEST_MODE": "true",
                "FIRESTORE_EMULATOR_HOST": "localhost:8080",
                "GOOGLE_CLOUD_PROJECT": "email-automation-cache",
            },
            clear=False,
        ):
            from email_automation import email_operations

        self.assertTrue(hasattr(email_operations, "LegacyEmailOperationsDisabled"))

        cases = [
            (
                email_operations.send_remaining_questions_email,
                (
                    "uid-1",
                    "client-1",
                    {"Authorization": "Bearer token"},
                    "broker@example.com",
                    ["Clear height"],
                    "<root@example.com>",
                    3,
                    "row-3",
                ),
            ),
            (
                email_operations.send_closing_email,
                (
                    "uid-1",
                    "client-1",
                    {"Authorization": "Bearer token"},
                    "broker@example.com",
                    "<root@example.com>",
                    3,
                    "row-3",
                ),
            ),
            (
                email_operations.send_new_property_email,
                (
                    "uid-1",
                    "client-1",
                    {"Authorization": "Bearer token"},
                    "broker@example.com",
                    "123 Test Way",
                    "Houston",
                    3,
                ),
            ),
            (
                email_operations.send_thankyou_closing_with_new_property,
                (
                    "uid-1",
                    "client-1",
                    {"Authorization": "Bearer token"},
                    "broker@example.com",
                    "<root@example.com>",
                    3,
                    "row-3",
                    "456 Backup Rd",
                ),
            ),
            (
                email_operations.send_thankyou_ask_alternatives,
                (
                    "uid-1",
                    "client-1",
                    {"Authorization": "Bearer token"},
                    "broker@example.com",
                    "<root@example.com>",
                    3,
                    "row-3",
                ),
            ),
        ]

        for func, args in cases:
            with self.subTest(func=func.__name__), patch.object(
                email_operations.requests,
                "post",
                side_effect=AssertionError("Legacy email helper touched Graph"),
            ):
                with self.assertRaises(email_operations.LegacyEmailOperationsDisabled):
                    func(*args)

    def test_legacy_send_helpers_can_be_explicitly_opted_in_for_migration_tests(self):
        with patch.dict(
            os.environ,
            {
                "E2E_TEST_MODE": "true",
                "FIRESTORE_EMULATOR_HOST": "localhost:8080",
                "GOOGLE_CLOUD_PROJECT": "email-automation-cache",
                "SITESIFT_ENABLE_LEGACY_EMAIL_OPERATIONS": "1",
            },
            clear=False,
        ):
            from email_automation import email_operations

            cases = [
                (
                    email_operations.send_remaining_questions_email,
                    (
                        "uid-1",
                        "client-1",
                        {"Authorization": "Bearer token"},
                        "broker@example.com",
                        ["Clear height"],
                        "<root@example.com>",
                        3,
                        "row-3",
                    ),
                ),
                (
                    email_operations.send_closing_email,
                    (
                        "uid-1",
                        "client-1",
                        {"Authorization": "Bearer token"},
                        "broker@example.com",
                        "<root@example.com>",
                        3,
                        "row-3",
                    ),
                ),
                (
                    email_operations.send_new_property_email,
                    (
                        "uid-1",
                        "client-1",
                        {"Authorization": "Bearer token"},
                        "broker@example.com",
                        "123 Test Way",
                        "Houston",
                        3,
                    ),
                ),
                (
                    email_operations.send_thankyou_closing_with_new_property,
                    (
                        "uid-1",
                        "client-1",
                        {"Authorization": "Bearer token"},
                        "broker@example.com",
                        "<root@example.com>",
                        3,
                        "row-3",
                        "456 Backup Rd",
                    ),
                ),
                (
                    email_operations.send_thankyou_ask_alternatives,
                    (
                        "uid-1",
                        "client-1",
                        {"Authorization": "Bearer token"},
                        "broker@example.com",
                        "<root@example.com>",
                        3,
                        "row-3",
                    ),
                ),
            ]

            def fake_get(url, *_args, **_kwargs):
                if url.endswith("/me/messages/draft-1"):
                    return _FakeResponse(
                        {
                            "internetMessageId": "<new-property@example.com>",
                            "conversationId": "conversation-1",
                        }
                    )
                return _FakeResponse({"value": []})

            for func, args in cases:
                with self.subTest(func=func.__name__), patch.object(
                    email_operations,
                    "_fs",
                    _EmptyFirestoreNode(),
                ), patch.object(
                    email_operations,
                    "_get_user_signature_settings",
                    return_value=(None, None, "operator@example.com"),
                ), patch.object(
                    email_operations,
                    "write_notification",
                ), patch.object(
                    email_operations.requests,
                    "get",
                    side_effect=fake_get,
                ), patch.object(
                    email_operations.requests,
                    "post",
                    return_value=_FakeResponse({"id": "draft-1"}),
                ) as mock_post, patch.dict(
                    sys.modules,
                    _legacy_indexing_modules(),
                ):
                    func(*args)

                    self.assertTrue(
                        mock_post.called,
                        f"{func.__name__} did not reach the mocked Graph request path",
                    )


if __name__ == "__main__":
    unittest.main()
