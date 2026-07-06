import os

os.environ.setdefault("E2E_TEST_MODE", "true")

import unittest

from email_automation.outbound_safety import find_unresolved_placeholders


class StressOutboxSendNearMissNegativeTests(unittest.TestCase):
    """stress: core.outbox_send / near_miss_negative.

    The send-path placeholder guard (find_unresolved_placeholders, the same guard
    gating outbound bodies AND now sheet writes) must not FALSE-POSITIVE on inputs
    that merely resemble an unresolved template token. A near-miss guard that fires
    on "[sic]" or on ordinary bracketed prose would block legitimate broker emails,
    so this cell proves the guard discriminates: real placeholders ("[NAME]",
    "[BROKER]") are caught, but near-misses (the whitelisted "[sic]" token, a
    non-placeholder bracketed aside, and plain company/broker copy) are NOT flagged.
    The positive control ([NAME]) is the discriminator: it proves the guard is
    actually active, so the near-miss passes are real, not a dead guard.
    """

    def test_guard_catches_real_placeholders_but_not_near_misses(self):
        # POSITIVE CONTROL: genuine unresolved placeholders ARE caught.
        self.assertEqual(["[NAME]"], find_unresolved_placeholders("Hi [NAME], following up."))
        self.assertEqual(["[BROKER]"], find_unresolved_placeholders("Please connect me with [BROKER]."))

        # NEAR-MISSES: superficially bracket-like but must NOT be flagged.
        # 1. The whitelisted editorial "[sic]" token.
        self.assertEqual([], find_unresolved_placeholders("The listing said 'reciept' [sic] in the flyer."))
        # 2. A non-placeholder bracketed aside (multi-word, no placeholder-hint keyword).
        self.assertEqual([], find_unresolved_placeholders("We toured the space [it was great] last week."))
        # 3. Ordinary broker/company copy with no brackets at all.
        self.assertEqual([], find_unresolved_placeholders("Acme Properties LLC confirmed 2,500 SF is available."))
        # 4. A real resolved greeting - the exact shape a placeholder would have replaced.
        self.assertEqual([], find_unresolved_placeholders("Hi Karsen, following up on 404 New Way."))


if __name__ == "__main__":
    unittest.main()
