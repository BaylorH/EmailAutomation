"""The allow-list that decides who this system may ever email during a test.

This is the check run before and after every live session, and the reason the last
one was safe. It used to live only in a session scratch directory; a guardrail that
evaporates between sessions is one nobody can rely on, and the run it goes missing
from is exactly the run that needs it.

The predicate is tested rather than trusted because the failure is silent and
one-directional: a wrongly-permitted address means mail reaches a real third party,
and there is no undo for that.
"""
import importlib.util
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

_spec = importlib.util.spec_from_file_location(
    "audit_send_exposure", os.path.join(REPO_ROOT, "scripts", "audit_send_exposure.py")
)
audit_send_exposure = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(audit_send_exposure)

allowed = audit_send_exposure.allowed


class AllowListTests(unittest.TestCase):
    def test_the_self_owned_accounts_are_allowed(self):
        for address in [
            "baylor.freelance@outlook.com",
            "baylor@manifoldengineering.ai",
            "bp21harrison@gmail.com",
            "bp21harrison+baymeadows@gmail.com",
        ]:
            with self.subTest(address=address):
                self.assertTrue(allowed(address))

    def test_case_and_padding_do_not_defeat_it(self):
        self.assertTrue(allowed("  BP21Harrison+Row7@Gmail.com  "))

    def test_a_real_broker_is_never_allowed(self):
        for address in [
            "dgee@gardenstaterealty.net",
            "joel@texascres.com",
            "someone@example-cre.com",
        ]:
            with self.subTest(address=address):
                self.assertFalse(allowed(address))

    def test_a_lookalike_domain_is_not_allowed(self):
        """The reason the domain is matched with endswith and not a substring."""
        for address in [
            "bp21harrison@gmail.com.attacker.net",
            "bp21harrison@gmail.com.evil.co",
            "bp21harrison@notgmail.com",
        ]:
            with self.subTest(address=address):
                self.assertFalse(allowed(address))

    def test_a_lookalike_local_part_is_not_allowed(self):
        self.assertFalse(allowed("notbp21harrison@gmail.com"))

    def test_a_different_gmail_account_is_not_allowed(self):
        self.assertFalse(allowed("someoneelse@gmail.com"))

    def test_an_absent_address_is_not_a_destination(self):
        for address in ["", None, "   "]:
            with self.subTest(address=address):
                self.assertTrue(allowed(address))


class AuditShapeTests(unittest.TestCase):
    """The audit reports exposure rather than raising, and a clean world is clean."""

    class _Doc:
        def __init__(self, doc_id, data):
            self.id = doc_id
            self._data = data
        def to_dict(self):
            return self._data

    class _Col:
        def __init__(self, docs=()):
            self._docs = list(docs)
        def stream(self):
            return iter(self._docs)
        def where(self, *_a, **_k):
            return AuditShapeTests._Col([])
        def document(self, _id):
            return AuditShapeTests._DocRef()

    class _DocRef:
        def collection(self, name):
            return AuditShapeTests._Col([])

    class _FS:
        def __init__(self, users):
            self._users = users
        def collection(self, name):
            if name == "users":
                return AuditShapeTests._Col(self._users)
            return AuditShapeTests._Col([])
        def document(self, _id):
            return AuditShapeTests._DocRef()

    def test_no_users_is_clean(self):
        problems, notes = audit_send_exposure.audit(self._FS([]))
        self.assertEqual(problems, [])
        self.assertEqual(notes, [])


if __name__ == "__main__":
    unittest.main()
