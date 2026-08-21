"""The two live-loop instruments are guardrails, so their guards are tested.

Neither script is imported by the product, which is exactly why they need this:
an untested guardrail is the shape of thing that rots silently and is then
trusted anyway. Both had real bugs found while proving them by hand -- the probe
reported delivered mail as missing twice over, for two different quoting reasons.

The safety-critical parts of both are pure functions, so they are tested directly
rather than by reaching a mail server.
"""
import importlib.util
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(REPO_ROOT, "scripts", filename)
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


probe = _load("probe_recipient_mailbox", "probe_recipient_mailbox.py")
replier = _load("reply_as_broker", "reply_as_broker.py")


class ProbeMailboxAllowListTests(unittest.TestCase):
    """The probe refuses to read any mailbox but the self-owned test account."""

    def test_the_self_owned_account_and_its_aliases_are_allowed(self):
        for address in [
            "bp21harrison@gmail.com",
            "bp21harrison+ev1full@gmail.com",
            "BP21Harrison+Row7@Gmail.com".lower(),
        ]:
            with self.subTest(address=address):
                self.assertTrue(probe.mailbox_allowed(address))

    def test_a_lookalike_domain_is_refused(self):
        """endswith, never substring -- the reason this is not an 'in' check."""
        for address in [
            "bp21harrison@gmail.com.attacker.net",
            "bp21harrison@notgmail.com",
            "bp21harrison@gmail.com.evil.co",
        ]:
            with self.subTest(address=address):
                self.assertFalse(probe.mailbox_allowed(address))

    def test_someone_elses_mailbox_is_refused(self):
        for address in ["someoneelse@gmail.com", "jill.ames@example.com", "", None]:
            with self.subTest(address=address):
                self.assertFalse(probe.mailbox_allowed(address))

    def test_spam_is_among_the_folders_it_searches(self):
        """The whole reason it exists: three false 'not delivered' calls came
        from an instrument that could not see spam."""
        self.assertIn("[Gmail]/Spam", probe.FOLDERS)
        self.assertIn("INBOX", probe.FOLDERS)


class ImapQuotingTests(unittest.TestCase):
    """Both false negatives the probe shipped with were quoting bugs."""

    def test_a_multi_word_value_is_quoted_as_one_token(self):
        for q in (probe.q, replier.q):
            with self.subTest(fn=q.__module__):
                self.assertEqual(q("951 E FM 646"), '"951 E FM 646"')

    def test_a_folder_name_containing_a_space_is_quoted(self):
        self.assertEqual(probe.q("[Gmail]/All Mail"), '"[Gmail]/All Mail"')

    def test_an_embedded_quote_cannot_break_out_of_the_string(self):
        for q in (probe.q, replier.q):
            with self.subTest(fn=q.__module__):
                self.assertEqual(q('a"b'), '"a\\"b"')
                self.assertEqual(q("a\\b"), '"a\\\\b"')


class ReplierDestinationTests(unittest.TestCase):
    """The replier can only ever send between two self-owned addresses."""

    def test_the_only_destination_is_the_self_owned_sender_identity(self):
        self.assertEqual(replier.ALLOWED_TO, "baylor.freelance@outlook.com")

    def test_it_only_sends_from_the_self_owned_account_or_its_aliases(self):
        self.assertTrue(replier.from_allowed("bp21harrison@gmail.com"))
        self.assertTrue(replier.from_allowed("bp21harrison+ev4unavail@gmail.com"))

    def test_it_refuses_to_send_from_anyone_else(self):
        for address in [
            "someoneelse@gmail.com",
            "bp21harrison@gmail.com.attacker.net",
            "notbp21harrison@gmail.com",
            "broker@example-cre.com",
            "",
            None,
        ]:
            with self.subTest(address=address):
                self.assertFalse(replier.from_allowed(address))

    def test_there_is_no_flag_that_widens_the_destination(self):
        """The limit is a constant, not an argument -- deliberately so."""
        import argparse

        parser_flags = set()
        original = argparse.ArgumentParser.add_argument

        def capture(self, *args, **kwargs):
            for arg in args:
                if isinstance(arg, str) and arg.startswith("--"):
                    parser_flags.add(arg)
            return original(self, *args, **kwargs)

        argparse.ArgumentParser.add_argument = capture
        try:
            try:
                replier.main()
            except SystemExit:
                pass
            except Exception:
                pass
        finally:
            argparse.ArgumentParser.add_argument = original

        for flag in parser_flags:
            self.assertNotIn(
                flag.lower().lstrip("-"),
                {"to", "recipient", "dest", "destination", "allow", "force"},
                f"{flag} would let the destination be widened from the command line",
            )


if __name__ == "__main__":
    unittest.main()
