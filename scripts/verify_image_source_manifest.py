#!/usr/bin/env python3
"""Compare the reviewed checkout's deployable source with the image's own record.

Run this BEFORE running any test suite inside the image. A suite that passes
inside a container whose bytes nobody compared to the reviewed source has proved
something about an artifact, not about the artifact that was reviewed.

    # extract the manifest from the exact tested digest, then:
    python3 scripts/verify_image_source_manifest.py --image-manifest ./manifest.json

Exit 0 only when the two sets are byte-for-byte identical.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from email_automation.certification import image_manifest as im  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image-manifest", type=Path, required=True,
                        help=f"the {im.MANIFEST_NAME} extracted from the image")
    parser.add_argument("--checkout", type=Path, default=REPO_ROOT)
    parser.add_argument("--print-digest", action="store_true")
    args = parser.parse_args(argv)

    try:
        image = im.load_manifest(args.image_manifest)
    except im.ManifestError as exc:
        # A missing manifest is a failure, never an empty pass: "no files
        # differed" and "no files were compared" must not look alike.
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2

    checkout = im.manifest_from_checkout(args.checkout)
    differences = im.compare(checkout, image)

    if args.print_digest:
        print(f"checkout manifest digest: {im.manifest_digest(checkout)}")
        print(f"image    manifest digest: {im.manifest_digest(image)}")

    if differences:
        print(f"IMAGE SOURCE MISMATCH ({len(differences)}):", file=sys.stderr)
        for line in differences:
            print(f"  {line}", file=sys.stderr)
        return 1

    print(f"image source matches the reviewed checkout "
          f"({len(checkout['files'])} files, digest {im.manifest_digest(checkout)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
