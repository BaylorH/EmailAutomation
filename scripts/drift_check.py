#!/usr/bin/env python3
"""Measure scope drift: product commits vs proof-apparatus commits.

This project has failed the same way five times. The pattern is not that the
product is hard -- it is that after each refutation a LARGER proof apparatus
gets commissioned, and the apparatus work then crowds out the product work while
everyone involved still feels busy. Activity stays high, delivery stops.

That is hard to notice from inside a turn, because every individual apparatus
commit is defensible. It is trivial to notice from outside, as a ratio:

    2026-06   product  78   apparatus   6     7% apparatus
    2026-07   product  45   apparatus  31    41% apparatus
    2026-08   product  85   apparatus 141    62% apparatus   <- 437 commits, nothing shipped

Monotonic and accelerating. This script exists so the ratio is CHECKED rather
than remembered, because the agents doing the work are exactly the ones who
cannot see it.

Classification is deliberately crude and deliberately conservative: a commit
mentioning both a product noun and an apparatus noun counts as BOTH, never as
product. Crude is fine -- the signal is a trend across months, not a verdict on
any one commit. Do not tune the regexes to make a month look better; that is the
same move as tuning a test to go green.

    python3 scripts/drift_check.py                 # last 6 months
    python3 scripts/drift_check.py --since 2026-08-01
"""

from __future__ import annotations

import argparse
import collections
import re
import subprocess
import sys

# Words naming the machinery that PROVES things about the product.
APPARATUS = re.compile(
    r"certif|scenario|registry|gate|readiness|evidence|proof|instrument|sweep|"
    r"mutation|contract|audit|manifest|twin|rollout|deploy|stamp|ledger|"
    r"handoff|docs:|chore|receipt|frontier|census|"
    # Added after tests/test_drift_check.py caught the classifier scoring
    # "91 canonical-JSON vectors, pinned as constants" as `other`. Apparatus
    # work was being undercounted, which flattered the ratio -- the one
    # direction this script must never drift in.
    r"canonical|vector|pinned|pin\b|interpreter|fixture|harness|socket|"
    r"matrix|taxonomy|verdict|refut|coverage|baseline|regression net|"
    r"blast radius|blocked-socket|test:|tests:",
    re.I,
)

# Words naming things a USER can observe.
PRODUCT = re.compile(
    r"broker|reply|email|extract|follow|thread|row|sheet|property|send|opex|"
    r"rent|pdf|attach|tour|opt-?out|signature|recipient|dedupe|notification|"
    r"dashboard|suite|placeholder|column",
    re.I,
)

# Above this, the sixth failure is forming. Not a hard gate -- a prompt to stop
# and ask what the last three commits made true for a real user.
WARN_SHARE = 0.40

# A month with one or two commits can read 100% and mean nothing. Only months
# with a real sample set the headline. This suppresses NOISE, never a signal:
# a month that is genuinely apparatus-heavy has commits by definition.
MIN_DECIDED = 10


def classify(subject: str) -> str:
    apparatus, product = bool(APPARATUS.search(subject)), bool(PRODUCT.search(subject))
    if apparatus and product:
        return "both"
    if apparatus:
        return "apparatus"
    if product:
        return "product"
    return "other"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", default="6 months ago")
    args = ap.parse_args(argv)

    proc = subprocess.run(
        ["git", "log", f"--since={args.since}", "--format=%ad%x1f%s", "--date=short"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        print(proc.stderr.strip(), file=sys.stderr)
        return 2

    rows = [line.split("\x1f", 1) for line in proc.stdout.strip().splitlines() if "\x1f" in line]
    if not rows:
        print("no commits in range")
        return 0

    by_month: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for date, subject in rows:
        by_month[date[:7]][classify(subject)] += 1

    print(f"{len(rows)} commits since {args.since}\n")
    print(f"{'month':9}{'product':>9}{'apparatus':>11}{'both':>6}{'other':>7}{'apparatus share':>17}")

    worst = 0.0
    for month in sorted(by_month):
        counts = by_month[month]
        product, apparatus = counts["product"], counts["apparatus"]
        decided = product + apparatus
        share = (apparatus / decided) if decided else 0.0
        if decided >= MIN_DECIDED:
            worst = max(worst, share)
        flag = ("  <-- OVER" if share >= WARN_SHARE and decided >= MIN_DECIDED
                else "  (small sample)" if decided < MIN_DECIDED else "")
        print(f"{month:9}{product:>9}{apparatus:>11}{counts['both']:>6}{counts['other']:>7}"
              f"{share:>16.0%}{flag}")

    print()
    if worst >= WARN_SHARE:
        print(f"APPARATUS SHARE {worst:.0%} >= {WARN_SHARE:.0%}.")
        print("Stop. Name what the last three commits made true for a real user.")
        print("If the answer is 'they make it possible to prove something later',")
        print("that is the fifth failure repeating, and it is time for a product fix.")
        return 1

    print(f"apparatus share peaked at {worst:.0%}; under the {WARN_SHARE:.0%} line.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
