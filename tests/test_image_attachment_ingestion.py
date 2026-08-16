import base64
import contextlib
import hashlib
import io
import os
import sys
import unittest
from collections import UserDict
from types import MappingProxyType
from unittest import mock
from urllib.parse import quote

from PIL import Image, PngImagePlugin


os.environ.setdefault("FIRESTORE_EMULATOR_HOST", "127.0.0.1:1")
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "email-automation-cache")
os.environ.setdefault("E2E_TEST_MODE", "true")

from email_automation import file_handling
from email_automation import ai_processing
from email_automation import property_images
from email_automation import processing
from email_automation.campaign_safety import CampaignAutomationDecision


FILE_ATTACHMENT_TYPE = "#microsoft.graph.fileAttachment"
GENERIC_IMAGE_NAME = "Broker property image"
MISSING_INLINE = object()


class _NoEncodeCanonicalBase64(str):
    def encode(self, *args, **kwargs):
        raise AssertionError("over-limit base64 was encoded before size rejection")


class _ExplodingBase64Regex:
    def fullmatch(self, *args, **kwargs):
        raise AssertionError("over-limit base64 reached the full-input regex")


class _ExplodingOversizedAddressText(str):
    def rfind(self, *args, **kwargs):
        raise AssertionError("oversized address text reached path splitting")


class _ExplodingAddressRegex:
    def finditer(self, *args, **kwargs):
        raise AssertionError("oversized address text reached address regex")

    def search(self, *args, **kwargs):
        raise AssertionError("oversized address text reached residual regex")


class _EqualitySpoofingHash:
    def __init__(self, private_sentinel):
        self.private_sentinel = private_sentinel

    def __eq__(self, other):
        return True

    def __ne__(self, other):
        return False

    def __repr__(self):
        return f"<spoofed-hash {self.private_sentinel}>"


class _PrivateHashString(str):
    def __new__(cls, value, private_sentinel):
        instance = super().__new__(cls, value)
        instance.private_sentinel = private_sentinel
        return instance

    def __repr__(self):
        return (
            f"{super().__repr__()}"
            f"<{self.private_sentinel}>"
        )


class _FalseNativeMarkerString(_PrivateHashString):
    def startswith(self, *args, **kwargs):
        return False


class _RaisingNativeMarkerString(_PrivateHashString):
    def startswith(self, *args, **kwargs):
        raise RuntimeError(self.private_sentinel)


class _PrivateNativeManifestDict(dict):
    pass


class _ClaimingNativeManifestDict(dict):
    def get(self, key, default=None):
        if key == "source_type":
            return "native_image"
        return super().get(key, default)


class _ExplodingGetterManifest:
    def __init__(self, private_sentinel):
        self.private_sentinel = private_sentinel

    def get(self, key, default=None):
        raise AssertionError(self.private_sentinel)


class _ExplodingCopyValue:
    def __init__(self, private_sentinel):
        self.private_sentinel = private_sentinel

    def __deepcopy__(self, memo):
        raise AssertionError(self.private_sentinel)

    def __repr__(self):
        return f"<private-copy-value {self.private_sentinel}>"


class _ExplodingEqualityValue:
    def __init__(self, private_sentinel):
        self.private_sentinel = private_sentinel

    def __eq__(self, other):
        raise AssertionError(self.private_sentinel)

    def __ne__(self, other):
        raise AssertionError(self.private_sentinel)

    def __repr__(self):
        return f"<private-equality-value {self.private_sentinel}>"


def _jpeg_bytes(size=(8, 6), *, orientation=None):
    image = Image.new("RGB", size, (28, 96, 164))
    output = io.BytesIO()
    save_kwargs = {}
    if orientation is not None:
        exif = Image.Exif()
        exif[274] = orientation
        save_kwargs["exif"] = exif
    image.save(output, format="JPEG", quality=91, **save_kwargs)
    return output.getvalue()


def _png_bytes(mode="RGB", size=(8, 6), *, alpha=None, metadata=None):
    if mode == "RGBA":
        image = Image.new(
            "RGBA",
            size,
            (28, 96, 164, 255 if alpha is None else alpha),
        )
    else:
        image = Image.new(mode, size, (28, 96, 164))

    pnginfo = None
    if metadata:
        pnginfo = PngImagePlugin.PngInfo()
        for key, value in metadata.items():
            pnginfo.add_text(str(key), str(value))

    output = io.BytesIO()
    image.save(output, format="PNG", pnginfo=pnginfo)
    return output.getvalue()


def _grayscale_png_bytes(size=(8, 6)):
    image = Image.new("L", size, 96)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _attachment(
    name,
    content_type,
    content=None,
    *,
    content_bytes=None,
    discriminator=FILE_ATTACHMENT_TYPE,
    is_inline=False,
):
    attachment = {
        "name": name,
        "contentType": content_type,
    }
    if is_inline is not MISSING_INLINE:
        attachment["isInline"] = is_inline
    if discriminator is not None:
        attachment["@odata.type"] = discriminator
    if content_bytes is None:
        content_bytes = base64.b64encode(content or b"").decode("ascii")
    attachment["contentBytes"] = content_bytes
    return attachment


class NativeImageValidationTests(unittest.TestCase):
    def _validator(self):
        validator = getattr(
            file_handling,
            "validate_and_normalize_native_image_content_batch",
            None,
        )
        self.assertTrue(
            callable(validator),
            "native-image content validator has not been implemented",
        )
        return validator

    def _validate(self, attachments):
        return self._validator()(attachments)

    def _size_guard(self):
        guard = getattr(file_handling, "_native_image_size_failure", None)
        self.assertTrue(
            callable(guard),
            "native-image size guard has not been implemented",
        )
        return guard

    def _assert_failure(self, batch, code):
        self.assertEqual(
            {
                "status": "quarantined",
                "assets": [],
                "failure": {
                    "name": GENERIC_IMAGE_NAME,
                    "code": code,
                },
            },
            batch,
        )

    def test_accepts_matching_jpg_jpeg_and_png_triples(self):
        batch = self._validate(
            [
                _attachment("one.jpg", "image/jpeg", _jpeg_bytes()),
                _attachment("two.jpeg", "image/jpeg", _jpeg_bytes((9, 7))),
                _attachment("three.png", "image/png", _png_bytes(size=(10, 8))),
            ]
        )

        self.assertEqual("accepted", batch["status"])
        self.assertEqual(3, len(batch["assets"]))
        for asset in batch["assets"]:
            self.assertEqual(
                {
                    "name",
                    "content_type",
                    "data",
                    "width",
                    "height",
                    "source_bytes",
                    "normalized_bytes",
                    "normalized_sha256",
                },
                set(asset),
            )
            self.assertEqual(GENERIC_IMAGE_NAME, asset["name"])
            self.assertEqual("image/png", asset["content_type"])
            self.assertTrue(asset["data"].startswith(b"\x89PNG\r\n\x1a\n"))
            self.assertLessEqual(max(asset["width"], asset["height"]), 1400)
            self.assertEqual(len(asset["data"]), asset["normalized_bytes"])
            self.assertEqual(
                hashlib.sha256(asset["data"]).hexdigest(),
                asset["normalized_sha256"],
            )
            self.assertNotIn("filename", asset)
            self.assertNotIn("property_binding", asset)
            self.assertNotIn("binding_method", asset)

    def test_ignores_missing_item_and_reference_discriminators_before_limits(self):
        ignored = []
        for discriminator in (
            None,
            "#microsoft.graph.itemAttachment",
            "#microsoft.graph.referenceAttachment",
        ):
            ignored.extend(
                _attachment(
                    f"ignored-{index}.png",
                    "image/png",
                    content_bytes="not strict base64",
                    discriminator=discriminator,
                )
                for index in range(4)
            )

        batch = self._validate(
            ignored + [_attachment("accepted.png", "image/png", _png_bytes())]
        )

        self.assertEqual("accepted", batch["status"])
        self.assertEqual(1, len(batch["assets"]))

    def test_rejects_non_strict_base64(self):
        valid_with_forbidden_whitespace = (
            base64.b64encode(_png_bytes()).decode("ascii") + "\n"
        )

        self._assert_failure(
            self._validate(
                [
                    _attachment(
                        "broker.png",
                        "image/png",
                        content_bytes=valid_with_forbidden_whitespace,
                    )
                ]
            ),
            "image_attachment_invalid_base64",
        )

    def test_rejects_extension_mime_magic_or_pillow_disagreement(self):
        cases = [
            (
                "extension_mime_pair",
                [_attachment("broker.jpg", "image/png", _png_bytes())],
                "image_attachment_type_mismatch",
                None,
            ),
            (
                "one_sided_supported_type",
                [_attachment("broker.gif", "image/png", _png_bytes())],
                "image_attachment_type_mismatch",
                None,
            ),
            (
                "magic",
                [_attachment("broker.png", "image/png", _jpeg_bytes())],
                "image_attachment_bad_magic",
                None,
            ),
            (
                "verify",
                [_attachment("broker.png", "image/png", _png_bytes()[:-12])],
                "image_attachment_decode_failed",
                None,
            ),
            (
                "pillow_format",
                [_attachment("broker.png", "image/png", _png_bytes())],
                "image_attachment_type_mismatch",
                "JPEG",
            ),
        ]

        for label, attachments, code, pillow_format_override in cases:
            with self.subTest(label=label):
                if pillow_format_override is None:
                    batch = self._validate(attachments)
                else:
                    with mock.patch.object(
                        file_handling,
                        "_inspect_native_image_pillow_format",
                        return_value=(pillow_format_override, 8, 6),
                        create=True,
                    ):
                        batch = self._validate(attachments)
                self._assert_failure(batch, code)

    def test_applies_exif_transpose_before_size_and_pixels(self):
        batch = self._validate(
            [
                _attachment(
                    "portrait.jpg",
                    "image/jpeg",
                    _jpeg_bytes((1600, 800), orientation=6),
                )
            ]
        )

        self.assertEqual("accepted", batch["status"])
        asset = batch["assets"][0]
        self.assertEqual((700, 1400), (asset["width"], asset["height"]))
        with Image.open(io.BytesIO(asset["data"])) as normalized:
            self.assertEqual((700, 1400), normalized.size)
            self.assertNotIn(274, normalized.getexif())

    def test_outputs_rgb_or_real_transparency_rgba_only(self):
        cases = [
            (_jpeg_bytes(), "image/jpeg", "broker.jpg", "RGB"),
            (_png_bytes(mode="RGBA", alpha=255), "image/png", "opaque.png", "RGB"),
            (_png_bytes(mode="RGBA", alpha=64), "image/png", "alpha.png", "RGBA"),
        ]

        for content, content_type, name, expected_mode in cases:
            with self.subTest(name=name):
                batch = self._validate([_attachment(name, content_type, content)])
                self.assertEqual("accepted", batch["status"])
                with Image.open(io.BytesIO(batch["assets"][0]["data"])) as normalized:
                    normalized.load()
                    self.assertEqual(expected_mode, normalized.mode)

    def test_strips_metadata_and_produces_deterministic_png_bytes(self):
        source = _png_bytes(
            mode="RGBA",
            alpha=192,
            metadata={"Comment": "private source note", "Broker": "example"},
        )

        first = self._validate([_attachment("broker.png", "image/png", source)])
        second = self._validate([_attachment("broker.png", "image/png", source)])
        first_asset = first["assets"][0]
        second_asset = second["assets"][0]

        self.assertEqual(first_asset["data"], second_asset["data"])
        self.assertEqual(
            first_asset["normalized_sha256"],
            second_asset["normalized_sha256"],
        )
        with Image.open(io.BytesIO(first_asset["data"])) as normalized:
            normalized.load()
            self.assertEqual({}, normalized.info)
            self.assertEqual(0, len(normalized.getexif()))

    def test_accepts_exactly_ten_mib_and_rejects_one_byte_over(self):
        guard = self._size_guard()
        limit = 10 * 1024 * 1024

        self.assertEqual(limit, file_handling.NATIVE_IMAGE_MAX_SOURCE_BYTES)
        self.assertIsNone(guard([limit], []))
        self.assertEqual(
            "image_attachment_too_large",
            guard([limit + 1], []),
        )

    def test_preflights_source_and_aggregate_size_before_encoding_or_decode(self):
        four_decoded_bytes = _NoEncodeCanonicalBase64("QUJDRA==")
        three_decoded_bytes = _NoEncodeCanonicalBase64("QUJD")
        forbidden_call = AssertionError("over-limit content reached decode/Pillow")

        cases = (
            (
                "source",
                3,
                20,
                [
                    _attachment(
                        "source-over.png",
                        "image/png",
                        content_bytes=four_decoded_bytes,
                    )
                ],
            ),
            (
                "aggregate",
                3,
                5,
                [
                    _attachment(
                        "aggregate-one.png",
                        "image/png",
                        content_bytes=three_decoded_bytes,
                    ),
                    _attachment(
                        "aggregate-two.png",
                        "image/png",
                        content_bytes=three_decoded_bytes,
                    ),
                ],
            ),
        )

        for label, source_limit, aggregate_limit, attachments in cases:
            with self.subTest(label=label), mock.patch.object(
                file_handling,
                "NATIVE_IMAGE_MAX_SOURCE_BYTES",
                source_limit,
            ), mock.patch.object(
                file_handling,
                "NATIVE_IMAGE_MAX_BATCH_SOURCE_BYTES",
                aggregate_limit,
            ), mock.patch.object(
                file_handling,
                "_NATIVE_IMAGE_STRICT_BASE64_RE",
                _ExplodingBase64Regex(),
                create=True,
            ), mock.patch.object(
                file_handling.base64,
                "b64decode",
                side_effect=forbidden_call,
            ), mock.patch.object(
                file_handling,
                "_inspect_native_image_header",
                side_effect=forbidden_call,
            ), mock.patch.object(
                file_handling,
                "_inspect_native_image_pillow_format",
                side_effect=forbidden_call,
            ), mock.patch.object(
                file_handling,
                "_verify_native_image",
                side_effect=forbidden_call,
            ), mock.patch.object(
                file_handling,
                "_normalize_native_image",
                side_effect=forbidden_call,
            ):
                try:
                    batch = self._validate(attachments)
                except AssertionError as exc:
                    self.fail(str(exc))
                else:
                    self._assert_failure(batch, "image_attachment_too_large")

    def test_accepts_exactly_twenty_megapixels_and_rejects_one_pixel_over_before_decode(self):
        guard = self._size_guard()
        limit = 20_000_000

        self.assertEqual(limit, file_handling.NATIVE_IMAGE_MAX_PIXELS)
        self.assertIsNone(guard([1], [limit]))
        self.assertEqual(
            "image_attachment_too_large",
            guard([1], [limit + 1]),
        )

        with mock.patch.object(
            file_handling,
            "_inspect_native_image_header",
            return_value=("PNG", limit + 1, 1),
            create=True,
        ), mock.patch.object(
            file_handling,
            "_verify_native_image",
            side_effect=AssertionError("pixel cap must run before full decode/verify"),
            create=True,
        ):
            batch = self._validate(
                [_attachment("oversized.png", "image/png", _png_bytes())]
            )

        self._assert_failure(batch, "image_attachment_too_large")

    def test_accepts_exactly_three_images_and_rejects_four(self):
        three = [
            _attachment(f"broker-{index}.png", "image/png", _png_bytes())
            for index in range(3)
        ]
        four = three + [
            _attachment(
                "fourth.png",
                "image/png",
                content_bytes="not strict base64",
            )
        ]

        self.assertEqual("accepted", self._validate(three)["status"])
        self._assert_failure(
            self._validate(four),
            "image_attachment_too_many",
        )

    def test_accepts_exactly_twenty_mib_aggregate_and_rejects_one_byte_over(self):
        guard = self._size_guard()
        per_image_limit = 10 * 1024 * 1024
        aggregate_limit = 20 * 1024 * 1024

        self.assertEqual(
            aggregate_limit,
            file_handling.NATIVE_IMAGE_MAX_BATCH_SOURCE_BYTES,
        )
        self.assertIsNone(
            guard([per_image_limit, per_image_limit], [])
        )
        self.assertEqual(
            "image_attachment_too_large",
            guard([per_image_limit, per_image_limit, 1], []),
        )

    def test_ignores_inline_images_before_count_and_binding(self):
        inline = [
            _attachment(
                f"signature-{index}.png",
                "image/png",
                content_bytes="not strict base64",
                is_inline=True,
            )
            for index in range(4)
        ]

        batch = self._validate(
            inline + [_attachment("broker.png", "image/png", _png_bytes())]
        )

        self.assertEqual("accepted", batch["status"])
        self.assertEqual(1, len(batch["assets"]))

    def test_requires_literal_false_inline_flag_before_candidate_work(self):
        ignored = [
            _attachment(
                f"ignored-inline-state-{index}.png",
                "image/png",
                content_bytes="not strict base64",
                is_inline=inline_state,
            )
            for index, inline_state in enumerate(
                (MISSING_INLINE, None, True, 1, "true")
            )
        ]

        batch = self._validate(
            ignored
            + [
                _attachment(
                    "literal-false.png",
                    "image/png",
                    _png_bytes(),
                    is_inline=False,
                )
            ]
        )

        self.assertEqual("accepted", batch["status"])
        self.assertEqual(1, len(batch["assets"]))

    def test_rejects_pillow_dimension_overflow_or_header_disagreement_before_decode(self):
        source = _attachment("broker.png", "image/png", _png_bytes())
        forbidden_call = AssertionError(
            "untrusted dimensions reached verify/normalization"
        )
        cases = (
            (
                "pillow_over_limit",
                ("PNG", 8, 6),
                ("PNG", file_handling.NATIVE_IMAGE_MAX_PIXELS + 1, 1),
            ),
            (
                "crafted_header_disagreement",
                ("PNG", 1, 1),
                ("PNG", 8, 6),
            ),
        )

        for label, raw_header, pillow_header in cases:
            with self.subTest(label=label), mock.patch.object(
                file_handling,
                "_inspect_native_image_header",
                return_value=raw_header,
            ), mock.patch.object(
                file_handling,
                "_inspect_native_image_pillow_format",
                return_value=pillow_header,
            ), mock.patch.object(
                file_handling,
                "_verify_native_image",
                side_effect=forbidden_call,
            ), mock.patch.object(
                file_handling,
                "_normalize_native_image",
                side_effect=forbidden_call,
            ):
                try:
                    batch = self._validate([source])
                except AssertionError as exc:
                    self.fail(str(exc))
                else:
                    self._assert_failure(batch, "image_attachment_too_large")

    def test_compound_invalid_batch_uses_stable_precedence_independent_of_order(self):
        mismatch = _attachment("mismatch.jpg", "image/png", _png_bytes())
        invalid_base64 = _attachment(
            "invalid.png",
            "image/png",
            content_bytes="%%%",
        )
        bad_magic = _attachment("bad-magic.png", "image/png", b"not a png")

        for attachments in (
            [mismatch, invalid_base64, bad_magic],
            [bad_magic, invalid_base64, mismatch],
        ):
            with self.subTest(order=[item["name"] for item in attachments]):
                self._assert_failure(
                    self._validate(attachments),
                    "image_attachment_type_mismatch",
                )

        pixel_oversized = _attachment(
            "pixel-oversized.png",
            "image/png",
            _png_bytes(),
        )
        for attachments in (
            [mismatch, pixel_oversized],
            [pixel_oversized, mismatch],
        ):
            with self.subTest(
                size_order=[item["name"] for item in attachments]
            ), mock.patch.object(
                file_handling,
                "_inspect_native_image_header",
                return_value=(
                    "PNG",
                    file_handling.NATIVE_IMAGE_MAX_PIXELS + 1,
                    1,
                ),
            ):
                self._assert_failure(
                    self._validate(attachments),
                    "image_attachment_too_large",
                )

        wrong_magic = _attachment(
            "wrong-magic.png",
            "image/png",
            _jpeg_bytes(),
        )
        with mock.patch.object(
            file_handling,
            "_inspect_native_image_header",
            return_value=(
                "JPEG",
                file_handling.NATIVE_IMAGE_MAX_PIXELS + 1,
                1,
            ),
        ):
            self._assert_failure(
                self._validate([wrong_magic]),
                "image_attachment_too_large",
            )

        four_invalid = [mismatch, invalid_base64, bad_magic, invalid_base64]
        for attachments in (four_invalid, list(reversed(four_invalid))):
            with self.subTest(count_order=[item["name"] for item in attachments]):
                self._assert_failure(
                    self._validate(attachments),
                    "image_attachment_too_many",
                )


class NativeImageBindingAtomicityTests(unittest.TestCase):
    TARGET = (
        "123 North Main Street, Suite 4B, Phoenix, Arizona 85001-1234"
    )
    MATCHING_FILENAME = (
        "PRIVATE_FILENAME_SENTINEL 123 N. Main St #4B, Phoenix AZ 85001 "
        "exterior.png"
    )

    def _classifier(self):
        classifier = getattr(
            file_handling,
            "classify_native_image_filename_binding",
            None,
        )
        self.assertTrue(
            callable(classifier),
            "native-image filename binding classifier has not been implemented",
        )
        return classifier

    def _batch_validator(self):
        validator = getattr(
            file_handling,
            "validate_and_normalize_native_image_attachments",
            None,
        )
        self.assertTrue(
            callable(validator),
            "bound native-image batch validator has not been implemented",
        )
        return validator

    def _manifest_adapter(self):
        adapter = getattr(
            file_handling,
            "build_native_image_manifest_entry",
            None,
        )
        self.assertTrue(
            callable(adapter),
            "native-image manifest adapter has not been implemented",
        )
        return adapter

    def _classify(self, filename, *, target=None):
        return self._classifier()(
            filename,
            target_property_hint=self.TARGET if target is None else target,
        )

    def _validate(self, attachments, *, target=None):
        return self._batch_validator()(
            attachments,
            target_property_hint=self.TARGET if target is None else target,
        )

    def _assert_failure(self, batch, code):
        self.assertEqual(
            {
                "status": "quarantined",
                "assets": [],
                "failure": {
                    "name": GENERIC_IMAGE_NAME,
                    "code": code,
                },
            },
            batch,
        )

    def _assert_target_binding(self, result):
        self.assertEqual(
            {
                "property_binding": "target",
                "binding_method": "structured_filename_address",
            },
            result,
        )

    def test_accepts_every_filename_claim_matching_complete_row_anchor(self):
        filenames = [
            self.MATCHING_FILENAME,
            (
                "front_123 North Main Street Suite 4-B Phoenix Arizona "
                "85001-9876.jpg"
            ),
            (
                "123 N Main St Unit 4B Phoenix AZ 85001 copy "
                "123 North Main Street #4B Phoenix Arizona 85001.jpeg"
            ),
        ]
        for filename in filenames:
            with self.subTest(filename=filename):
                self._assert_target_binding(self._classify(filename))
        self._assert_target_binding(
            self._classify(
                "123 N.E. Main St #4B San Jose A.Z. 85001.png",
                target=(
                    "123 Northeast Main Street Suite 4B, "
                    "San Jos\N{LATIN SMALL LETTER E WITH ACUTE}, Arizona 85001"
                ),
            )
        )
        self._assert_target_binding(
            self._classify(
                "123-N-Sample-Rd-Ste-200-Example-City-AZ-85001.PNG",
                target=(
                    "123 North Sample Road Suite 200, "
                    "Example City, Arizona 85001"
                ),
            )
        )

        shared_pixels = _png_bytes()
        batch = self._validate(
            [
                _attachment(filenames[0], "image/png", shared_pixels),
                _attachment(filenames[1], "image/jpeg", _jpeg_bytes()),
                _attachment(filenames[2], "image/jpeg", _jpeg_bytes()),
            ]
        )

        self.assertEqual("accepted", batch["status"])
        self.assertEqual(3, len(batch["assets"]))
        for asset in batch["assets"]:
            self.assertEqual("target", asset["property_binding"])
            self.assertEqual(
                "structured_filename_address",
                asset["binding_method"],
            )
            self.assertNotIn("filename", asset)

        manifest = self._manifest_adapter()(batch)
        self.assertEqual(
            {
                "name",
                "text",
                "images",
                "method",
                "source_type",
                "property_binding",
                "binding_method",
                "image_meta",
            },
            set(manifest),
        )
        self.assertEqual(GENERIC_IMAGE_NAME, manifest["name"])
        self.assertEqual("", manifest["text"])
        self.assertEqual("native_image_normalized", manifest["method"])
        self.assertEqual("native_image", manifest["source_type"])
        self.assertEqual("target", manifest["property_binding"])
        self.assertEqual(
            "structured_filename_address",
            manifest["binding_method"],
        )
        self.assertEqual(3, len(manifest["images"]))
        self.assertEqual(3, len(manifest["image_meta"]))
        self.assertNotIn("PRIVATE_FILENAME_SENTINEL", repr(batch))
        self.assertNotIn("PRIVATE_FILENAME_SENTINEL", repr(manifest))
        self.assertNotIn("drive_link", repr(manifest))
        for metadata in manifest["image_meta"]:
            self.assertEqual(
                {
                    "content_type",
                    "width",
                    "height",
                    "source_bytes",
                    "normalized_bytes",
                    "normalized_sha256",
                },
                set(metadata),
            )

    def test_rejects_wrong_property_filename(self):
        wrong = "456 South Oak Road, Tempe AZ 85281 exterior.png"

        self.assertEqual(
            {"failure_code": "image_attachment_wrong_property"},
            self._classify(wrong),
        )
        self._assert_failure(
            self._validate([_attachment(wrong, "image/png", _png_bytes())]),
            "image_attachment_wrong_property",
        )

    def test_rejects_mixed_property_claims_in_one_filename(self):
        target_claim = "123 N Main St #4B Phoenix AZ 85001"
        wrong_claim = "456 S Oak Rd Tempe AZ 85281"
        mixed_name = f"{target_claim} and {wrong_claim}.png"
        duplicate_wrong = f"{wrong_claim} copy {wrong_claim}.png"

        self.assertEqual(
            {"failure_code": "image_attachment_mixed_property"},
            self._classify(mixed_name),
        )
        self.assertEqual(
            {"failure_code": "image_attachment_wrong_property"},
            self._classify(duplicate_wrong),
        )

        target_attachment = _attachment(
            f"{target_claim}.png",
            "image/png",
            _png_bytes(),
        )
        wrong_attachment = _attachment(
            f"{wrong_claim}.png",
            "image/png",
            _png_bytes(),
        )
        for attachments in (
            [target_attachment, wrong_attachment],
            [wrong_attachment, target_attachment],
        ):
            with self.subTest(order=[item["name"] for item in attachments]):
                self._assert_failure(
                    self._validate(attachments),
                    "image_attachment_mixed_property",
                )

    def test_rejects_addressless_or_incomplete_filename(self):
        cases = (
            "front exterior.png",
            "123 N Main St front.png",
            (
                "123 N Main St #4B Phoenix AZ 85001 and "
                "456 Oak Road rear.png"
            ),
            "123 N Main Phoenix AZ 85001.png",
        )

        for filename in cases:
            with self.subTest(filename=filename):
                self.assertEqual(
                    {"failure_code": "image_attachment_unbound_property"},
                    self._classify(filename),
                )

    def test_rejects_same_street_with_different_city_state_or_zip(self):
        cases = (
            "123 N Main St #4B Tucson AZ 85001 exterior.png",
            "123 N Main St #4B Phoenix New Mexico 85001 exterior.png",
            "123 N Main St #4B Phoenix AZ 85002 exterior.png",
        )

        for filename in cases:
            with self.subTest(filename=filename):
                self.assertEqual(
                    {"failure_code": "image_attachment_wrong_property"},
                    self._classify(filename),
                )

    def test_rejects_street_suffix_mismatch(self):
        self.assertEqual(
            {"failure_code": "image_attachment_wrong_property"},
            self._classify(
                "123 N Main Avenue #4B Phoenix AZ 85001 exterior.png"
            ),
        )
        self.assertEqual(
            {"failure_code": "image_attachment_unbound_property"},
            self._classify(
                "123 N Main Str #4B Phoenix AZ 85001 exterior.png"
            ),
        )

    def test_rejects_unit_mismatch_or_one_sided_unit(self):
        mismatch_cases = (
            "123 N Main St Unit 5 Phoenix AZ 85001.png",
            "123 N Main St Phoenix AZ 85001.png",
        )
        for filename in mismatch_cases:
            with self.subTest(filename=filename):
                self.assertEqual(
                    {"failure_code": "image_attachment_wrong_property"},
                    self._classify(filename),
                )

        target_without_unit = "123 N Main St, Phoenix AZ 85001"
        self.assertEqual(
            {"failure_code": "image_attachment_wrong_property"},
            self._classify(
                "123 N Main St Unit 4B Phoenix AZ 85001.png",
                target=target_without_unit,
            ),
        )
        self._assert_target_binding(
            self._classify(
                "123 North Main Street Phoenix Arizona 85001.png",
                target=target_without_unit,
            )
        )

    def test_rejects_incomplete_row_and_filename_address_components(self):
        valid_filename = "123 N Main St #4B Phoenix AZ 85001.png"
        incomplete_targets = (
            "North Main Street Suite 4B Phoenix AZ 85001",
            "123 North Main Suite 4B Phoenix AZ 85001",
            "123 North Main Street Suite 4B AZ 85001",
            "123 North Main Street Suite 4B Phoenix 85001",
            "123 North Main Street Suite 4B Phoenix AZ",
            (
                "123 N Main St #4B Phoenix AZ 85001 and "
                "123 N Main St #4B Phoenix AZ 85001"
            ),
            (
                "123 N Main St #4B Phoenix AZ 85001 and "
                "456 S Oak Rd Tempe AZ 85281"
            ),
        )

        for target in incomplete_targets:
            with self.subTest(target=target):
                self.assertEqual(
                    {"failure_code": "image_attachment_unbound_property"},
                    self._classify(valid_filename, target=target),
                )

    def test_does_not_rescue_single_addressless_image_from_target_text(self):
        self._assert_failure(
            self._validate(
                [_attachment("front-elevation.png", "image/png", _png_bytes())]
            ),
            "image_attachment_unbound_property",
        )

    def test_one_invalid_sibling_quarantines_the_complete_batch(self):
        pixels = _png_bytes()
        valid = _attachment(
            "123 N Main St #4B Phoenix AZ 85001 front.png",
            "image/png",
            pixels,
        )
        addressless = _attachment("rear.png", "image/png", pixels)
        wrong = _attachment(
            "456 S Oak Rd Tempe AZ 85281 rear.png",
            "image/png",
            pixels,
        )

        for sibling, expected in (
            (addressless, "image_attachment_unbound_property"),
            (wrong, "image_attachment_mixed_property"),
        ):
            for attachments in ([valid, sibling], [sibling, valid]):
                with self.subTest(
                    expected=expected,
                    order=[item["name"] for item in attachments],
                ):
                    self._assert_failure(self._validate(attachments), expected)

    def test_complete_batch_validation_precedes_drive_model_and_sheet_calls(self):
        valid = _attachment(
            "123 N Main St #4B Phoenix AZ 85001 front.png",
            "image/png",
            _png_bytes(),
        )
        wrong_bad_magic = _attachment(
            "456 S Oak Rd Tempe AZ 85281 rear.png",
            "image/png",
            b"not a png",
        )
        ignored = [
            _attachment(
                "999 Hostile Ave Other AZ 85999 signature.png",
                "image/png",
                content_bytes="not strict base64",
                is_inline=True,
            ),
            _attachment(
                "888 Hostile Ave Other AZ 85888 item.png",
                "image/png",
                content_bytes="not strict base64",
                discriminator="#microsoft.graph.itemAttachment",
            ),
            _attachment(
                "777 Hostile Ave Other AZ 85777 reference.png",
                "image/png",
                content_bytes="not strict base64",
                discriminator="#microsoft.graph.referenceAttachment",
            ),
        ]

        with mock.patch.object(
            file_handling,
            "upload_property_image_to_drive",
        ) as drive_call, mock.patch.object(
            file_handling.client.responses,
            "create",
        ) as model_call, mock.patch.object(
            property_images,
            "build_property_image_sheet_updates",
        ) as sheet_call:
            for attachments in (
                [valid, wrong_bad_magic],
                [wrong_bad_magic, valid],
            ):
                with self.subTest(
                    order=[item["name"] for item in attachments]
                ):
                    self._assert_failure(
                        self._validate(attachments),
                        "image_attachment_bad_magic",
                    )

            accepted = self._validate(ignored + [valid])
            self.assertEqual("accepted", accepted["status"])
            drive_call.assert_not_called()
            model_call.assert_not_called()
            sheet_call.assert_not_called()

        precedence = getattr(
            file_handling,
            "_select_native_image_failure",
            None,
        )
        self.assertTrue(callable(precedence))
        all_codes = [
            "image_attachment_unbound_property",
            "image_attachment_wrong_property",
            "image_attachment_mixed_property",
            "image_attachment_decode_failed",
            "image_attachment_bad_magic",
            "image_attachment_invalid_base64",
            "image_attachment_type_mismatch",
            "image_attachment_too_large",
            "image_attachment_too_many",
        ]
        self.assertEqual("image_attachment_too_many", precedence(all_codes))
        self.assertEqual(
            "image_attachment_too_many",
            precedence(list(reversed(all_codes))),
        )
        self.assertEqual(
            "image_attachment_mixed_property",
            precedence(all_codes[:3]),
        )
        self.assertEqual(
            "image_attachment_wrong_property",
            precedence(all_codes[:2]),
        )
        self.assertIsNone(self._manifest_adapter()(
            self._validate([valid, wrong_bad_magic])
        ))


class NativeImageBindingContractReviewTests(unittest.TestCase):
    TARGET = (
        "123 North Sample Road Suite 200, Example City, Arizona 85001-1234"
    )
    MATCHING_FILENAME = (
        "123 N Sample Rd Ste 200 Example City AZ 85001 exterior.png"
    )

    def _classify(self, filename, *, target=None):
        return file_handling.classify_native_image_filename_binding(
            filename,
            target_property_hint=self.TARGET if target is None else target,
        )

    def _validate(self, filename, *, target=None):
        return file_handling.validate_and_normalize_native_image_attachments(
            [_attachment(filename, "image/png", _png_bytes())],
            target_property_hint=self.TARGET if target is None else target,
        )

    def _assert_unbound_classification(self, result):
        self.assertEqual(
            {"failure_code": "image_attachment_unbound_property"},
            result,
        )

    def _assert_unbound_batch(self, result):
        self.assertEqual(
            {
                "status": "quarantined",
                "assets": [],
                "failure": {
                    "name": GENERIC_IMAGE_NAME,
                    "code": "image_attachment_unbound_property",
                },
            },
            result,
        )

    def _assert_target(self, result):
        self.assertEqual(
            {
                "property_binding": "target",
                "binding_method": "structured_filename_address",
            },
            result,
        )

    def test_rejects_residual_partial_claims_in_filename_and_target(self):
        residuals = (
            "456 Oak",
            "Oak Road Tempe AZ",
        )
        for residual in residuals:
            filename = (
                "123 N Sample Rd Ste 200 Example City AZ 85001 "
                f"{residual}.png"
            )
            with self.subTest(location="filename", residual=residual):
                self._assert_unbound_classification(self._classify(filename))
                self._assert_unbound_batch(self._validate(filename))

            target = f"{self.TARGET} {residual}"
            with self.subTest(location="target", residual=residual):
                self._assert_unbound_classification(
                    self._classify(self.MATCHING_FILENAME, target=target)
                )
                self._assert_unbound_batch(
                    self._validate(self.MATCHING_FILENAME, target=target)
                )

    def test_rejects_unsupported_street_number_range_without_collapsing(self):
        target = "125 N Sample Rd Ste 200 Example City AZ 85001"
        ranged_filename = (
            "123-125-N-Sample-Rd-Ste-200-Example-City-AZ-85001.png"
        )

        self._assert_unbound_classification(
            self._classify(ranged_filename, target=target)
        )
        self._assert_unbound_batch(
            self._validate(ranged_filename, target=target)
        )
        self._assert_unbound_classification(
            self._classify(
                "125 N Sample Rd Ste 200 Example City AZ 85001.png",
                target=(
                    "123-125 N Sample Rd Ste 200 Example City AZ 85001"
                ),
            )
        )

    def test_rejects_zip_prefix_followed_by_letters(self):
        bad_filename = (
            "123 N Sample Rd Ste 200 Example City AZ 85001evil.png"
        )
        bad_target = (
            "123 N Sample Rd Ste 200 Example City AZ 85001evil"
        )

        self._assert_unbound_classification(self._classify(bad_filename))
        self._assert_unbound_batch(self._validate(bad_filename))
        self._assert_unbound_classification(
            self._classify(self.MATCHING_FILENAME, target=bad_target)
        )
        self._assert_unbound_batch(
            self._validate(self.MATCHING_FILENAME, target=bad_target)
        )

    def test_supports_reviewed_street_suffix_aliases(self):
        cases = (
            (
                "123 Sample Av Example City AZ 85001.png",
                "123 Sample Avenue Example City Arizona 85001",
            ),
            (
                "123 Sample Bnd Example City AZ 85001.png",
                "123 Sample Bend Example City Arizona 85001",
            ),
            (
                "123 Sample Pike Example City AZ 85001.png",
                "123 Sample Pike Example City Arizona 85001",
            ),
            (
                "123 Sample Row Example City AZ 85001.png",
                "123 Sample Row Example City Arizona 85001",
            ),
        )

        for filename, target in cases:
            with self.subTest(filename=filename):
                self._assert_target(self._classify(filename, target=target))

        accepted = self._validate(cases[0][0], target=cases[0][1])
        self.assertEqual("accepted", accepted["status"])

    def test_normalizes_hyphenated_compound_directionals(self):
        target = "123 Northeast Sample Road Example City Arizona 85001"
        filenames = (
            "123 North-East Sample Rd Example City AZ 85001.png",
            "123 N-E Sample Rd Example City AZ 85001.png",
        )

        for filename in filenames:
            with self.subTest(filename=filename):
                self._assert_target(self._classify(filename, target=target))

        accepted = self._validate(filenames[1], target=target)
        self.assertEqual("accepted", accepted["status"])

    def test_caps_filename_and_target_before_unicode_or_regex(self):
        expected_cap = 1024
        oversized_target = _ExplodingOversizedAddressText(
            "x" * (expected_cap + 1)
        )
        oversized_filename = _ExplodingOversizedAddressText(
            ("x" * (expected_cap + 1)) + ".png"
        )
        original_normalize = file_handling.unicodedata.normalize

        def guarded_normalize(form, value):
            if value is oversized_target or value is oversized_filename:
                raise AssertionError(
                    "oversized address text reached Unicode normalization"
                )
            return original_normalize(form, value)

        with mock.patch.object(
            file_handling.unicodedata,
            "normalize",
            side_effect=guarded_normalize,
        ), mock.patch.object(
            file_handling,
            "_NATIVE_IMAGE_ADDRESS_RE",
            _ExplodingAddressRegex(),
        ), mock.patch.object(
            file_handling,
            "_NATIVE_IMAGE_PARTIAL_STREET_RE",
            _ExplodingAddressRegex(),
        ), mock.patch.object(
            file_handling,
            "_NATIVE_IMAGE_PARTIAL_STATE_ZIP_RE",
            _ExplodingAddressRegex(),
        ):
            try:
                target_result = self._classify(
                    self.MATCHING_FILENAME,
                    target=oversized_target,
                )
            except AssertionError as exc:
                self.fail(str(exc))
            self._assert_unbound_classification(target_result)

        with mock.patch.object(
            file_handling.unicodedata,
            "normalize",
            side_effect=guarded_normalize,
        ):
            try:
                filename_result = self._classify(oversized_filename)
            except AssertionError as exc:
                self.fail(str(exc))
            self._assert_unbound_classification(filename_result)

        normal_oversized_filename = ("x" * (expected_cap + 1)) + ".png"
        self._assert_unbound_batch(self._validate(normal_oversized_filename))
        self.assertEqual(
            expected_cap,
            getattr(
                file_handling,
                "NATIVE_IMAGE_MAX_ADDRESS_TEXT_CHARS",
                None,
            ),
        )


class NativeImageManifestIntegrityTests(unittest.TestCase):
    def _asset(self, data=None, **overrides):
        normalized_data = _png_bytes() if data is None else data
        asset = {
            "name": GENERIC_IMAGE_NAME,
            "content_type": "image/png",
            "data": normalized_data,
            "width": 8,
            "height": 6,
            "source_bytes": len(normalized_data),
            "normalized_bytes": len(normalized_data),
            "normalized_sha256": hashlib.sha256(normalized_data).hexdigest(),
            "property_binding": "target",
            "binding_method": "structured_filename_address",
        }
        asset.update(overrides)
        return asset

    def _adapt(self, assets):
        return file_handling.build_native_image_manifest_entry(
            {"status": "accepted", "assets": assets}
        )

    def test_rejects_non_png_or_corrupt_normalized_bytes(self):
        cases = (
            self._asset(data=b"not a PNG"),
            self._asset(data=_png_bytes()[:-12]),
        )
        for asset in cases:
            with self.subTest(data_length=len(asset["data"])):
                self.assertIsNone(self._adapt([asset]))

    def test_rejects_more_than_maximum_asset_count(self):
        self.assertIsNone(
            self._adapt(
                [
                    self._asset()
                    for _ in range(
                        file_handling.NATIVE_IMAGE_MAX_COUNT + 1
                    )
                ]
            )
        )

    def test_rejects_mismatched_nonpositive_or_overlimit_dimensions(self):
        over_edge_data = _png_bytes(
            size=(file_handling.NATIVE_IMAGE_MAX_EDGE + 1, 1)
        )
        cases = (
            self._asset(width=9),
            self._asset(width=0),
            self._asset(height=-1),
            self._asset(width=True),
            self._asset(
                data=over_edge_data,
                width=file_handling.NATIVE_IMAGE_MAX_EDGE + 1,
                height=1,
            ),
        )
        for asset in cases:
            with self.subTest(
                width=asset["width"],
                height=asset["height"],
            ):
                self.assertIsNone(self._adapt([asset]))

    def test_rejects_invalid_or_overlimit_size_metadata(self):
        data = _png_bytes()
        cases = (
            self._asset(data=data, source_bytes=0),
            self._asset(data=data, source_bytes=-1),
            self._asset(data=data, source_bytes=True),
            self._asset(
                data=data,
                source_bytes=file_handling.NATIVE_IMAGE_MAX_SOURCE_BYTES + 1,
            ),
            self._asset(data=data, normalized_bytes=0),
            self._asset(data=data, normalized_bytes=True),
            self._asset(data=data, normalized_bytes=len(data) + 1),
            self._asset(
                data=data,
                normalized_bytes=(
                    file_handling.NATIVE_IMAGE_MAX_SOURCE_BYTES + 1
                ),
            ),
        )
        for asset in cases:
            with self.subTest(
                source_bytes=asset["source_bytes"],
                normalized_bytes=asset["normalized_bytes"],
            ):
                self.assertIsNone(self._adapt([asset]))

        aggregate_over_limit = [
            self._asset(source_bytes=7 * 1024 * 1024)
            for _ in range(3)
        ]
        self.assertIsNone(self._adapt(aggregate_over_limit))

    def test_rejects_normalized_sha256_mismatch(self):
        self.assertIsNone(
            self._adapt([self._asset(normalized_sha256="0" * 64)])
        )

    def test_rejects_unsafe_or_nonexact_asset_shape(self):
        extra_filename = self._asset()
        extra_filename["raw_filename"] = "private-property-name.png"
        extra_exif = self._asset()
        extra_exif["exif"] = {"OwnerName": "private"}
        missing_name = self._asset()
        missing_name.pop("name")
        wrong_name = self._asset(name="private-property-name.png")

        for asset in (
            extra_filename,
            extra_exif,
            missing_name,
            wrong_name,
        ):
            with self.subTest(keys=sorted(asset)):
                self.assertIsNone(self._adapt([asset]))

    def test_requires_exact_plain_lowercase_hash_string(self):
        canonical_data, _, _ = file_handling._normalize_native_image(
            _png_bytes()
        )
        exact_hash = hashlib.sha256(canonical_data).hexdigest()
        honest_manifest = self._adapt(
            [self._asset(data=canonical_data, normalized_sha256=exact_hash)]
        )
        self.assertIsNotNone(honest_manifest)
        projected_hash = honest_manifest["image_meta"][0][
            "normalized_sha256"
        ]
        self.assertIs(type(projected_hash), str)
        self.assertEqual(exact_hash, projected_hash)
        self.assertEqual(exact_hash.lower(), projected_hash)

        private_sentinel = "PRIVATE_HASH_TYPE_SENTINEL"
        hostile_values = (
            _EqualitySpoofingHash(private_sentinel),
            _PrivateHashString(exact_hash, private_sentinel),
        )
        for hostile_value in hostile_values:
            with self.subTest(hostile_type=type(hostile_value).__name__):
                manifest = self._adapt(
                    [
                        self._asset(
                            data=canonical_data,
                            normalized_sha256=hostile_value,
                        )
                    ]
                )
                self.assertIsNone(manifest)
                self.assertNotIn(private_sentinel, repr(manifest))


class NativeImageSecondSuccessorBindingTests(unittest.TestCase):
    TARGET = "123 North Sample Road, Example City, Arizona 85001"
    MATCHING_FILENAME = "123 N Sample Rd Example City AZ 85001.png"

    def _classify(self, filename, *, target=None):
        return file_handling.classify_native_image_filename_binding(
            filename,
            target_property_hint=self.TARGET if target is None else target,
        )

    def _validate(self, filename, *, target=None):
        return file_handling.validate_and_normalize_native_image_attachments(
            [_attachment(filename, "image/png", _png_bytes())],
            target_property_hint=self.TARGET if target is None else target,
        )

    def _assert_unbound_classification(self, result):
        self.assertEqual(
            {"failure_code": "image_attachment_unbound_property"},
            result,
        )

    def _assert_unbound_batch(self, result):
        self.assertEqual(
            {
                "status": "quarantined",
                "assets": [],
                "failure": {
                    "name": GENERIC_IMAGE_NAME,
                    "code": "image_attachment_unbound_property",
                },
            },
            result,
        )

    def _assert_target(self, result):
        self.assertEqual(
            {
                "property_binding": "target",
                "binding_method": "structured_filename_address",
            },
            result,
        )

    def test_rejects_every_reviewed_residual_claim_in_filename_target_and_batch(self):
        residuals = (
            "Oak Road Tempe",
            "Oak Road",
            "456 View",
            "456 7th",
            "456 Front",
        )
        for residual in residuals:
            filename = (
                "123 N Sample Rd Example City AZ 85001 "
                f"{residual}.png"
            )
            with self.subTest(location="filename", residual=residual):
                self._assert_unbound_classification(self._classify(filename))
                self._assert_unbound_batch(self._validate(filename))

            target = f"{self.TARGET} {residual}"
            with self.subTest(location="target", residual=residual):
                self._assert_unbound_classification(
                    self._classify(self.MATCHING_FILENAME, target=target)
                )
                self._assert_unbound_batch(
                    self._validate(self.MATCHING_FILENAME, target=target)
                )

    def test_rejects_numeric_address_and_unit_hyphen_collisions(self):
        target_6789 = "6789 Main Road, Example City, Arizona 85001"
        collided_address = (
            "12345-6789 Main Rd Example City AZ 85001.png"
        )
        self._assert_unbound_classification(
            self._classify(collided_address, target=target_6789)
        )
        self._assert_unbound_batch(
            self._validate(collided_address, target=target_6789)
        )
        self._assert_unbound_classification(
            self._classify(
                "6789 Main Rd Example City AZ 85001.png",
                target="12345-6789 Main Rd Example City AZ 85001",
            )
        )

        target_unit_123 = (
            "123 N Sample Rd Suite 123, Example City, Arizona 85001"
        )
        collided_unit = (
            "123 N Sample Rd Suite 12-3 Example City AZ 85001.png"
        )
        self._assert_unbound_classification(
            self._classify(collided_unit, target=target_unit_123)
        )
        self._assert_unbound_batch(
            self._validate(collided_unit, target=target_unit_123)
        )
        self._assert_unbound_classification(
            self._classify(
                "123 N Sample Rd Suite 123 Example City AZ 85001.png",
                target=(
                    "123 N Sample Rd Suite 12-3 Example City AZ 85001"
                ),
            )
        )

        self._assert_target(
            self._classify(
                "123 N Sample Rd Suite 4-B Example City AZ 85001.png",
                target=(
                    "123 North Sample Road Suite 4B, "
                    "Example City, Arizona 85001"
                ),
            )
        )

    def test_rejects_seven_digit_residual_and_range_claims(self):
        residual = "and 1234567 Oak"
        residual_filename = (
            "123 N Sample Rd Example City AZ 85001 "
            f"{residual}.png"
        )
        self._assert_unbound_classification(
            self._classify(residual_filename)
        )
        self._assert_unbound_batch(self._validate(residual_filename))

        residual_target = f"{self.TARGET} {residual}"
        self._assert_unbound_classification(
            self._classify(
                self.MATCHING_FILENAME,
                target=residual_target,
            )
        )
        self._assert_unbound_batch(
            self._validate(
                self.MATCHING_FILENAME,
                target=residual_target,
            )
        )

        target_6789 = "6789 Main Road, Example City, Arizona 85001"
        range_collision = (
            "1234567-6789 Main Rd Example City AZ 85001.png"
        )
        self._assert_unbound_classification(
            self._classify(range_collision, target=target_6789)
        )
        self._assert_unbound_batch(
            self._validate(range_collision, target=target_6789)
        )
        self._assert_unbound_classification(
            self._classify(
                "6789 Main Rd Example City AZ 85001.png",
                target=(
                    "1234567-6789 Main Rd Example City AZ 85001"
                ),
            )
        )

    def test_rejects_unicode_dash_street_number_ranges(self):
        target_125 = "125 Main Road, Example City, Arizona 85001"
        matching_filename = "125 Main Rd Example City AZ 85001.png"
        dash_cases = (
            ("en_dash", "\N{EN DASH}"),
            ("em_dash", "\N{EM DASH}"),
            ("minus_sign", "\N{MINUS SIGN}"),
        )

        for label, dash in dash_cases:
            ranged = (
                f"123{dash}125 Main Rd Example City AZ 85001"
            )
            with self.subTest(label=label, surface="filename_classifier"):
                self._assert_unbound_classification(
                    self._classify(f"{ranged}.png", target=target_125)
                )
            with self.subTest(label=label, surface="filename_batch"):
                self._assert_unbound_batch(
                    self._validate(f"{ranged}.png", target=target_125)
                )
            with self.subTest(label=label, surface="target_classifier"):
                self._assert_unbound_classification(
                    self._classify(matching_filename, target=ranged)
                )
            with self.subTest(label=label, surface="target_batch"):
                self._assert_unbound_batch(
                    self._validate(matching_filename, target=ranged)
                )

    def test_rejects_punctuation_and_ordinal_range_collisions(self):
        cases = (
            (
                "underscore_after_range",
                "123-125_ Main Rd Example City AZ 85001",
                "125 Main Road, Example City, Arizona 85001",
                "125 Main Rd Example City AZ 85001.png",
            ),
            (
                "comma_after_range",
                "123-125, Main Rd Example City AZ 85001",
                "125 Main Road, Example City, Arizona 85001",
                "125 Main Rd Example City AZ 85001.png",
            ),
            (
                "slash_after_range",
                "123-125/ Main Rd Example City AZ 85001",
                "125 Main Road, Example City, Arizona 85001",
                "125 Main Rd Example City AZ 85001.png",
            ),
            (
                "ordinal_street",
                "123-125 7th St Example City AZ 85001",
                "125 7th Street, Example City, Arizona 85001",
                "125 7th St Example City AZ 85001.png",
            ),
            (
                "seven_digit_underscore",
                "1234567_6789 Main Rd Example City AZ 85001",
                "6789 Main Road, Example City, Arizona 85001",
                "6789 Main Rd Example City AZ 85001.png",
            ),
        )

        for label, ranged, target, matching_filename in cases:
            with self.subTest(label=label, surface="filename_classifier"):
                self._assert_unbound_classification(
                    self._classify(f"{ranged}.png", target=target)
                )
            with self.subTest(label=label, surface="filename_batch"):
                self._assert_unbound_batch(
                    self._validate(f"{ranged}.png", target=target)
                )
            with self.subTest(label=label, surface="target_classifier"):
                self._assert_unbound_classification(
                    self._classify(matching_filename, target=ranged)
                )
            with self.subTest(label=label, surface="target_batch"):
                self._assert_unbound_batch(
                    self._validate(matching_filename, target=ranged)
                )

    def test_never_drops_unicode_decimal_competing_prefixes(self):
        target_125 = "125 Main Road, Example City, Arizona 85001"
        matching_filename = "125 Main Rd Example City AZ 85001.png"
        digit_cases = (
            ("arabic_indic", "\u0661\u0662\u0663"),
            ("eastern_arabic", "\u06f1\u06f2\u06f3"),
            ("devanagari", "\u0967\u0968\u0969"),
            ("bengali", "\u09e7\u09e8\u09e9"),
            ("fullwidth_control", "\uff11\uff12\uff13"),
        )

        for label, competing_number in digit_cases:
            ranged = (
                f"{competing_number}-125 Main Rd "
                "Example City AZ 85001"
            )
            with self.subTest(label=label, surface="filename_classifier"):
                self._assert_unbound_classification(
                    self._classify(f"{ranged}.png", target=target_125)
                )
            with self.subTest(label=label, surface="filename_batch"):
                self._assert_unbound_batch(
                    self._validate(f"{ranged}.png", target=target_125)
                )
            with self.subTest(label=label, surface="target_classifier"):
                self._assert_unbound_classification(
                    self._classify(matching_filename, target=ranged)
                )
            with self.subTest(label=label, surface="target_batch"):
                self._assert_unbound_batch(
                    self._validate(matching_filename, target=ranged)
                )

    def test_never_drops_raw_nondecimal_numeric_prefixes(self):
        target_125 = "125 Main Road, Example City, Arizona 85001"
        matching_filename = "125 Main Rd Example City AZ 85001.png"
        numeric_cases = (
            ("chinese", "\u4e00\u4e8c\u4e09"),
            ("roman", "\u216d\u2169\u2169\u2162"),
            ("hangzhou", "\u3021\u3022\u3023"),
            ("ethiopic", "\u137b\u1373\u136b"),
            ("circled_control", "\u2460\u2461\u2462"),
            ("superscript_control", "\u00b9\u00b2\u00b3"),
        )

        for label, competing_number in numeric_cases:
            ranged = (
                f"{competing_number}-125 Main Rd "
                "Example City AZ 85001"
            )
            with self.subTest(label=label, surface="filename_classifier"):
                self._assert_unbound_classification(
                    self._classify(f"{ranged}.png", target=target_125)
                )
            with self.subTest(label=label, surface="filename_batch"):
                self._assert_unbound_batch(
                    self._validate(f"{ranged}.png", target=target_125)
                )
            with self.subTest(label=label, surface="target_classifier"):
                self._assert_unbound_classification(
                    self._classify(matching_filename, target=ranged)
                )
            with self.subTest(label=label, surface="target_batch"):
                self._assert_unbound_batch(
                    self._validate(matching_filename, target=ranged)
                )

    def test_allows_only_structured_trailing_numeric_metadata(self):
        target = "125 Main Road, Example City, Arizona 85001"
        base = "125 Main Rd Example City AZ 85001"
        accepted_suffixes = (
            "photo 2",
            "copy 3",
            "(2)",
            "page 4",
            "2026-08-16",
        )

        for suffix in accepted_suffixes:
            filename = f"{base} {suffix}.png"
            with self.subTest(suffix=suffix, surface="classifier"):
                self._assert_target(self._classify(filename, target=target))
            with self.subTest(suffix=suffix, surface="batch"):
                self.assertEqual(
                    "accepted",
                    self._validate(filename, target=target)["status"],
                )

        rejected_suffixes = (
            "5",
            "front 6",
            "2026-02-31",
            "2026-13-40",
        )
        for suffix in rejected_suffixes:
            filename = f"{base} {suffix}.png"
            with self.subTest(suffix=suffix, surface="classifier"):
                self._assert_unbound_classification(
                    self._classify(filename, target=target)
                )
            with self.subTest(suffix=suffix, surface="batch"):
                self._assert_unbound_batch(
                    self._validate(filename, target=target)
                )

    def test_allows_compatibility_digits_only_in_structured_metadata(self):
        target = "125 Main Road, Example City, Arizona 85001"
        base = "125 Main Rd Example City AZ 85001"
        accepted_suffixes = (
            "photo \u2461",
            "copy \u00b3",
            "(\u00b2)",
            "page \u2084",
        )

        for suffix in accepted_suffixes:
            filename = f"{base} {suffix}.png"
            with self.subTest(suffix=suffix, surface="classifier"):
                self._assert_target(self._classify(filename, target=target))
            with self.subTest(suffix=suffix, surface="batch"):
                self.assertEqual(
                    "accepted",
                    self._validate(filename, target=target)["status"],
                )

        matching_filename = f"{base}.png"
        competing_numbers = (
            ("circled", "\u2460\u2461\u2462"),
            ("superscript", "\u00b9\u00b2\u00b3"),
            ("subscript", "\u2081\u2082\u2083"),
        )
        for label, competing_number in competing_numbers:
            ranged = (
                f"{competing_number}-125 Main Rd "
                "Example City AZ 85001"
            )
            with self.subTest(label=label, surface="filename_classifier"):
                self._assert_unbound_classification(
                    self._classify(f"{ranged}.png", target=target)
                )
            with self.subTest(label=label, surface="filename_batch"):
                self._assert_unbound_batch(
                    self._validate(f"{ranged}.png", target=target)
                )
            with self.subTest(label=label, surface="target_classifier"):
                self._assert_unbound_classification(
                    self._classify(matching_filename, target=ranged)
                )
            with self.subTest(label=label, surface="target_batch"):
                self._assert_unbound_batch(
                    self._validate(matching_filename, target=ranged)
                )

    def test_rejects_compact_alphanumeric_numeric_prefixes(self):
        target = "125 Main Road, Example City, Arizona 85001"
        matching_filename = "125 Main Rd Example City AZ 85001.png"
        compact_prefixes = (
            "Suite123",
            "Unit456",
            "456Front",
            "id1234567",
            "Suite\u2460\u2461\u2462",
            "Unit\u2463\u2464\u2465",
        )

        for compact_prefix in compact_prefixes:
            ranged = (
                f"{compact_prefix}-125 Main Rd Example City AZ 85001"
            )
            with self.subTest(
                compact_prefix=compact_prefix,
                surface="filename_classifier",
            ):
                self._assert_unbound_classification(
                    self._classify(f"{ranged}.png", target=target)
                )
            with self.subTest(
                compact_prefix=compact_prefix,
                surface="target_classifier",
            ):
                self._assert_unbound_classification(
                    self._classify(matching_filename, target=ranged)
                )
            with self.subTest(
                compact_prefix=compact_prefix,
                surface="real_batch",
            ):
                self._assert_unbound_batch(
                    self._validate(f"{ranged}.png", target=target)
                )

    def test_rejects_malformed_zip_plus_three(self):
        malformed_filename = (
            "123 N Sample Rd Example City AZ 85001-123.png"
        )
        malformed_target = (
            "123 N Sample Rd Example City AZ 85001-123"
        )

        self._assert_unbound_classification(
            self._classify(malformed_filename)
        )
        self._assert_unbound_batch(self._validate(malformed_filename))
        self._assert_unbound_classification(
            self._classify(self.MATCHING_FILENAME, target=malformed_target)
        )
        self._assert_unbound_batch(
            self._validate(self.MATCHING_FILENAME, target=malformed_target)
        )

    def test_preserves_contextual_zip_plus_four_and_unit_suffixes(self):
        cases = (
            (
                (
                    "123 Main Rd Suite 4-B Santa Fe New Mexico "
                    "87101-1234 exterior.png"
                ),
                (
                    "123 Main Road Suite 4B, Santa Fe, "
                    "New Mexico 87101"
                ),
            ),
            (
                (
                    "123 Main Rd Suite 4-B Washington District of "
                    "Columbia 20001-1234 exterior.png"
                ),
                (
                    "123 Main Road Suite 4B, Washington, "
                    "District of Columbia 20001"
                ),
            ),
            (
                (
                    "123 Main Rd Suite 4-B Phoenix A.Z. "
                    "85001-1234 exterior.png"
                ),
                (
                    "123 Main Road Suite 4B, Phoenix, Arizona 85001"
                ),
            ),
            (
                (
                    "123 Main Rd Suite 4-B Phoenix AZ. "
                    "85001-1234 exterior.png"
                ),
                (
                    "123 Main Road Suite 4B, Phoenix, Arizona 85001"
                ),
            ),
        )

        for filename, target in cases:
            with self.subTest(filename=filename):
                self._assert_target(self._classify(filename, target=target))
                self.assertEqual(
                    "accepted",
                    self._validate(filename, target=target)["status"],
                )

    def test_allows_only_valid_leading_iso_date_descriptor(self):
        dated_filename = (
            "2026-08-16-123-N-Sample-Rd-Example-City-AZ-85001.png"
        )
        self._assert_target(self._classify(dated_filename))
        self.assertEqual("accepted", self._validate(dated_filename)["status"])

        invalid_date_filenames = (
            "2026-13-40-123-N-Sample-Rd-Example-City-AZ-85001.png",
            "2026-02-31-123-N-Sample-Rd-Example-City-AZ-85001.png",
        )
        for invalid_date_filename in invalid_date_filenames:
            with self.subTest(invalid_date=invalid_date_filename):
                self._assert_unbound_classification(
                    self._classify(invalid_date_filename)
                )
                self._assert_unbound_batch(
                    self._validate(invalid_date_filename)
                )

        dated_with_residual = (
            "2026-08-16-123-N-Sample-Rd-Example-City-AZ-85001-456-Oak.png"
        )
        self._assert_unbound_classification(
            self._classify(dated_with_residual)
        )
        self._assert_unbound_batch(self._validate(dated_with_residual))


class NativeImageCanonicalManifestReviewTests(unittest.TestCase):
    def _asset(self, data):
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
        return {
            "name": GENERIC_IMAGE_NAME,
            "content_type": "image/png",
            "data": data,
            "width": width,
            "height": height,
            "source_bytes": 1,
            "normalized_bytes": len(data),
            "normalized_sha256": hashlib.sha256(data).hexdigest(),
            "property_binding": "target",
            "binding_method": "structured_filename_address",
        }

    def _adapt(self, data):
        return file_handling.build_native_image_manifest_entry(
            {"status": "accepted", "assets": [self._asset(data)]}
        )

    def test_rejects_metadata_trailing_bytes_and_noncanonical_color_mode(self):
        private_text_sentinel = "PRIVATE_TEXT_METADATA_SENTINEL"
        trailing_sentinel = b"PRIVATE_TRAILING_SENTINEL"
        cases = (
            _png_bytes(metadata={"Comment": private_text_sentinel}),
            _png_bytes() + trailing_sentinel,
            _grayscale_png_bytes(),
        )

        for data in cases:
            with self.subTest(data_length=len(data)):
                manifest = self._adapt(data)
                self.assertIsNone(manifest)
                self.assertNotIn(private_text_sentinel, repr(manifest))
                self.assertNotIn(
                    trailing_sentinel.decode("ascii"),
                    repr(manifest),
                )

    def test_rejects_actual_overlimit_dimensions_before_full_decode(self):
        actual_over_edge = _png_bytes(
            size=(file_handling.NATIVE_IMAGE_MAX_EDGE + 1, 1)
        )
        forged_asset = self._asset(actual_over_edge)
        forged_asset["width"] = 8
        forged_asset["height"] = 6
        forbidden = AssertionError(
            "forged dimensions reached verify/canonical normalization"
        )

        with mock.patch.object(
            file_handling,
            "_verify_native_image",
            side_effect=forbidden,
        ) as verify_call, mock.patch.object(
            file_handling,
            "_normalize_native_image",
            side_effect=forbidden,
        ) as normalize_call:
            try:
                manifest = file_handling.build_native_image_manifest_entry(
                    {"status": "accepted", "assets": [forged_asset]}
                )
            except AssertionError as exc:
                self.fail(str(exc))

        self.assertIsNone(manifest)
        verify_call.assert_not_called()
        normalize_call.assert_not_called()


class NativeImageAIPrivacyTests(unittest.TestCase):
    TARGET = "123 North Sample Road, Example City, Arizona 85001"
    MATCHING_FILENAME = "123 N Sample Rd Example City AZ 85001"
    CANONICAL_REVIEW_EVENT = {
        "type": "needs_user_input",
        "reason": "multi_property_attachment",
        "question": (
            "The broker offered multiple properties or suites in an attachment, "
            "but the details could not be bound safely to one row."
        ),
    }

    def _manifest(self, image_specs):
        attachments = [
            _attachment(
                f"{self.MATCHING_FILENAME} {descriptor}.{extension}",
                content_type,
                content,
            )
            for descriptor, extension, content_type, content in image_specs
        ]
        batch = file_handling.validate_and_normalize_native_image_attachments(
            attachments,
            target_property_hint=self.TARGET,
        )
        self.assertEqual("accepted", batch["status"])
        manifest = file_handling.build_native_image_manifest_entry(batch)
        self.assertIsNotNone(manifest)
        return manifest

    def _single_manifest(self, descriptor="exterior", size=(8, 6)):
        return self._manifest([
            (descriptor, "png", "image/png", _png_bytes(size=size)),
        ])

    def _column_config(self):
        return {
            "mappings": {"total_sf": "Total SF"},
            "extractionFields": ["total_sf"],
            "requiredFields": [],
            "formulaFields": [],
            "neverRequest": [],
            "customFields": {},
        }

    def _fake_client(self, output_text=None, file_id="scanned-pdf-file"):
        response = mock.Mock()
        response.output_text = output_text or (
            '{"updates": [], "events": [], "response_email": null, '
            '"notes": ""}'
        )
        response.usage = None
        response.id = "native-image-response"
        fake_client = mock.Mock()
        fake_client.responses.create.return_value = response
        fake_client.files.create.return_value = mock.Mock(id=file_id)
        return fake_client

    def _run_proposal(
        self,
        manifests,
        *,
        output_text=None,
        dry_run=True,
        fake_client=None,
    ):
        fake_client = fake_client or self._fake_client(output_text)
        if output_text is not None:
            fake_client.responses.create.return_value.output_text = output_text
        fake_fs = mock.Mock()
        with mock.patch.object(
            ai_processing,
            "client",
            fake_client,
        ), mock.patch.object(
            file_handling,
            "client",
            fake_client,
        ), mock.patch.object(
            ai_processing,
            "_fs",
            fake_fs,
        ), mock.patch.object(
            ai_processing,
            "track_openai_usage_safely",
        ) as usage_call, mock.patch(
            "builtins.print",
        ) as print_call:
            proposal = ai_processing.propose_sheet_updates(
                uid="native-image-user",
                client_id="native-image-client",
                email="broker@example.com",
                sheet_id="native-image-sheet",
                header=["Property Address", "Total SF"],
                rownum=3,
                rowvals=[self.TARGET, ""],
                thread_id="native-image-thread",
                pdf_manifest=manifests,
                conversation=[{
                    "direction": "inbound",
                    "from": "broker@example.com",
                    "content": "Photos for the target property are attached.",
                }],
                column_config=self._column_config(),
                extraction_fields=["total_sf"],
                dry_run=dry_run,
            )
        return {
            "proposal": proposal,
            "client": fake_client,
            "firestore": fake_fs,
            "usage_call": usage_call,
            "print_call": print_call,
        }

    def test_vision_uses_one_normalized_data_png_input_image_per_asset(self):
        manifest = self._manifest([
            ("front", "jpg", "image/jpeg", _jpeg_bytes(size=(9, 7))),
            ("rear", "png", "image/png", _png_bytes(size=(7, 5))),
        ])
        expected_urls = [
            f"data:image/png;base64,{image}"
            for image in manifest["images"]
        ]
        run = self._run_proposal(
            [manifest],
            output_text=(
                '{"updates": [{"column": "Total SF", "value": "20000", '
                '"confidence": 0.98, "reason": "Visible in target image."}], '
                '"events": [], "response_email": null, '
                '"notes": ""}'
            ),
        )

        self.assertEqual(1, run["client"].responses.create.call_count)
        request_content = (
            run["client"].responses.create.call_args.kwargs["input"][0]["content"]
        )
        self.assertEqual(
            expected_urls,
            [
                item["image_url"]
                for item in request_content
                if item.get("type") == "input_image"
            ],
        )
        self.assertTrue(all(
            item["image_url"].startswith("data:image/png;base64,")
            for item in request_content
            if item.get("type") == "input_image"
        ))
        prompt = next(
            item["text"]
            for item in request_content
            if item.get("type") == "input_text"
        )
        self.assertIn("=== NATIVE IMAGE ATTACHMENTS ===", prompt)
        self.assertNotIn("=== PDF ATTACHMENTS ===", prompt)
        self.assertNotIn("--- PDF: Broker property image", prompt)
        self.assertIn(
            "client_question | negotiation | confidential | legal_contract | "
            "unclear | multi_property_attachment",
            prompt,
        )
        self.assertEqual(
            "20000",
            run["proposal"]["updates"][0]["value"],
            "a benign non-door field extracted from native vision must survive",
        )

    def test_native_images_never_use_input_file_or_files_create(self):
        private_file_id = "PRIVATE_NATIVE_FILE_ID_SENTINEL"
        manifest = self._single_manifest()
        manifest["id"] = private_file_id
        manifest["file_id"] = private_file_id

        run = self._run_proposal([manifest])

        self.assertEqual(1, run["client"].responses.create.call_count)
        self.assertEqual(0, run["client"].files.create.call_count)
        request_content = (
            run["client"].responses.create.call_args.kwargs["input"][0]["content"]
        )
        self.assertFalse(any(
            item.get("type") == "input_file"
            for item in request_content
        ))
        self.assertNotIn(private_file_id, repr(request_content))

    def test_multiple_prevalidated_target_images_are_not_addressless_competitors(self):
        native_manifest = self._manifest([
            ("front", "png", "image/png", _png_bytes(size=(8, 6))),
            ("rear", "png", "image/png", _png_bytes(size=(7, 5))),
        ])
        target_pdf = {
            "name": "123 North Sample Road brochure.pdf",
            "text": f"{self.TARGET} - Total SF: 20,000.",
            "images": [],
            "method": "local_extraction",
            "file_id": None,
            "id": None,
        }
        expected_update = {
            "column": "Total SF",
            "value": "20000",
            "confidence": 0.98,
        }
        proposal = {
            "updates": [expected_update],
            "events": [],
            "response_email": "Thanks for the target-property photos.",
        }

        result = ai_processing._suppress_competing_attachment_updates(
            proposal,
            [{
                "direction": "inbound",
                "content": "The requested target-property photos are attached.",
            }],
            self.TARGET,
            [native_manifest, target_pdf],
        )

        self.assertEqual([expected_update], result["updates"])
        self.assertEqual([], result["events"])
        self.assertEqual(
            "Thanks for the target-property photos.",
            result["response_email"],
        )

    def test_model_multi_property_strips_updates_and_reply_and_pauses(self):
        manifests = [
            self._single_manifest("front", (8, 6)),
            self._single_manifest("rear", (7, 5)),
        ]
        proposal = {
            "updates": [{
                "column": "Total SF",
                "value": "20000",
                "confidence": 0.99,
            }],
            "events": [
                {
                    "type": "new_property",
                    "address": "999 Hostile Early Return Road",
                },
                {
                    "type": "needs_user_input",
                    "reason": "multi_property_attachment",
                    "question": "Which row should receive 20,000 SF?",
                },
                {
                    "type": "call_requested",
                    "question": "Call about the ambiguous images.",
                },
            ],
            "response_email": "I will write 20,000 SF to the target row.",
            "notes": "PRIVATE_UNTRUSTED_NATIVE_NOTE",
        }

        result = ai_processing._suppress_competing_attachment_updates(
            proposal,
            [{
                "direction": "inbound",
                "content": (
                    f"For {self.TARGET}, the target is 20,000 SF. "
                    "The photos are attached."
                ),
            }],
            self.TARGET,
            manifests,
        )

        self.assertEqual([], result["updates"])
        self.assertEqual([self.CANONICAL_REVIEW_EVENT], result["events"])
        self.assertIsNone(result["response_email"])
        self.assertEqual("", result["notes"])

        wrong_pdf = {
            "name": "999 Hostile Early Return Road brochure.pdf",
            "text": "999 Hostile Early Return Road - Total SF: 20,000.",
            "images": [],
            "method": "local_extraction",
            "file_id": None,
            "id": None,
        }
        omitted_model_pause = {
            "updates": [{"column": "Total SF", "value": "20000"}],
            "events": [{
                "type": "new_property",
                "address": "999 Hostile Early Return Road",
            }],
            "response_email": "I will write the competing PDF value.",
            "notes": "PRIVATE_UNTRUSTED_MIXED_NOTE",
        }
        mixed_result = ai_processing._suppress_competing_attachment_updates(
            omitted_model_pause,
            [{
                "direction": "inbound",
                "content": "The target photo and alternate brochure are attached.",
            }],
            self.TARGET,
            [self._single_manifest("mixed-target"), wrong_pdf],
        )
        self.assertEqual([], mixed_result["updates"])
        self.assertEqual(
            [self.CANONICAL_REVIEW_EVENT],
            mixed_result["events"],
        )
        self.assertIsNone(mixed_result["response_email"])
        self.assertEqual("", mixed_result["notes"])

        legacy_pdf_only = {
            "updates": [{"column": "Total SF", "value": "20000"}],
            "events": [{
                "type": "new_property",
                "address": "999 Hostile Early Return Road",
            }],
            "response_email": "Legacy PDF referral response.",
        }
        legacy_result = ai_processing._suppress_competing_attachment_updates(
            legacy_pdf_only,
            [{"direction": "inbound", "content": "Alternate brochure attached."}],
            self.TARGET,
            [wrong_pdf],
        )
        self.assertIs(legacy_pdf_only, legacy_result)
        self.assertEqual(
            "Legacy PDF referral response.",
            legacy_result["response_email"],
        )

        optout = {"type": "contact_optout", "reason": "unsubscribe"}
        native_optout = {
            "updates": [{"column": "Total SF", "value": "20000"}],
            "events": [
                {"type": "new_property", "address": "999 Hostile Early Return Road"},
                optout,
                {
                    "type": "needs_user_input",
                    "reason": "multi_property_attachment",
                },
            ],
            "response_email": "This must not send.",
        }
        optout_result = ai_processing._suppress_competing_attachment_updates(
            native_optout,
            [{"direction": "inbound", "content": "Unsubscribe me."}],
            self.TARGET,
            [self._single_manifest("optout"), wrong_pdf],
        )
        self.assertEqual([], optout_result["updates"])
        self.assertEqual([optout], optout_result["events"])
        self.assertIsNone(optout_result["response_email"])

    def test_safe_manifest_projection_excludes_raw_pixel_exif_and_exception_sentinels(self):
        projector = getattr(
            file_handling,
            "project_safe_native_image_manifest",
            None,
        )
        self.assertTrue(
            callable(projector),
            "safe native-image manifest projector has not been implemented",
        )
        manifest = self._single_manifest()
        pixel_payload = manifest["images"][0]
        manifest.update({
            "raw_filename": "PRIVATE_FILENAME_SENTINEL.png",
            "pixels": "PRIVATE_PIXEL_SENTINEL",
            "exif": {"Owner": "PRIVATE_EXIF_SENTINEL"},
            "icc_profile": "PRIVATE_ICC_SENTINEL",
            "comments": "PRIVATE_COMMENT_SENTINEL",
            "exception": RuntimeError("PRIVATE_EXCEPTION_SENTINEL"),
            "id": "PRIVATE_FILE_ID_SENTINEL",
            "file_id": "PRIVATE_FILE_ID_SENTINEL",
        })
        manifest["image_meta"][0].update({
            "raw_filename": "PRIVATE_META_FILENAME_SENTINEL.png",
            "pixels": "PRIVATE_META_PIXEL_SENTINEL",
            "exif": "PRIVATE_META_EXIF_SENTINEL",
            "icc_profile": "PRIVATE_META_ICC_SENTINEL",
            "comments": "PRIVATE_META_COMMENT_SENTINEL",
            "exception": RuntimeError("PRIVATE_META_EXCEPTION_SENTINEL"),
        })

        projection = projector(manifest)

        self.assertEqual(
            {
                "name",
                "text",
                "method",
                "source_type",
                "property_binding",
                "binding_method",
                "image_meta",
            },
            set(projection),
        )
        self.assertEqual(
            {
                "content_type",
                "width",
                "height",
                "source_bytes",
                "normalized_bytes",
                "normalized_sha256",
            },
            set(projection["image_meta"][0]),
        )
        run = self._run_proposal([manifest], dry_run=False)
        persist_call = (
            run["firestore"].collection.return_value
            .document.return_value
            .collection.return_value
            .document.return_value
            .set
        )
        self.assertEqual(1, persist_call.call_count)
        persisted = persist_call.call_args.args[0]
        self.assertEqual([projection], persisted["pdfManifest"])
        self.assertEqual([], persisted["fileIds"])
        self.assertEqual(1, run["usage_call"].call_count)

        persisted_observable = repr((
            projection,
            run["print_call"].call_args_list,
            persisted,
        ))
        self.assertNotIn(pixel_payload, persisted_observable)
        observable = repr((
            run["client"].responses.create.call_args,
            run["print_call"].call_args_list,
            persisted,
        ))
        for sentinel in (
            "PRIVATE_FILENAME_SENTINEL",
            "PRIVATE_PIXEL_SENTINEL",
            "PRIVATE_EXIF_SENTINEL",
            "PRIVATE_ICC_SENTINEL",
            "PRIVATE_COMMENT_SENTINEL",
            "PRIVATE_EXCEPTION_SENTINEL",
            "PRIVATE_FILE_ID_SENTINEL",
            "PRIVATE_META_FILENAME_SENTINEL",
            "PRIVATE_META_PIXEL_SENTINEL",
            "PRIVATE_META_EXIF_SENTINEL",
            "PRIVATE_META_ICC_SENTINEL",
            "PRIVATE_META_COMMENT_SENTINEL",
            "PRIVATE_META_EXCEPTION_SENTINEL",
        ):
            with self.subTest(sentinel=sentinel):
                self.assertNotIn(sentinel, observable)

        mutating_manifest = self._single_manifest("sealed-snapshot")
        mutating_batch = [mutating_manifest]
        original_image = mutating_manifest["images"][0]
        original_content_type = (
            mutating_manifest["image_meta"][0]["content_type"]
        )
        original_source_bytes = (
            mutating_manifest["image_meta"][0]["source_bytes"]
        )
        transport_sentinel = "PRIVATE_POST_PREFLIGHT_IMAGE_SENTINEL"
        metadata_sentinel = "PRIVATE_POST_PREFLIGHT_META_SENTINEL"
        attachment_sentinel = "PRIVATE_POST_PREFLIGHT_ATTACHMENT_SENTINEL"
        mutated_source_bytes = (
            file_handling.NATIVE_IMAGE_MAX_BATCH_SOURCE_BYTES + 1
        )
        original_project_preflighted = (
            file_handling._project_preflighted_native_image_manifest
        )
        mutation_calls = []

        def mutate_source_after_preflight(preflight):
            mutation_calls.append(preflight)
            if len(mutation_calls) == 1:
                mutating_manifest["images"].extend(
                    [transport_sentinel] * 3
                )
                mutating_manifest["image_meta"][0]["content_type"] = (
                    metadata_sentinel
                )
                mutating_manifest["image_meta"][0]["source_bytes"] = (
                    mutated_source_bytes
                )
                mutating_batch.append({
                    "name": f"{attachment_sentinel}.pdf",
                    "text": attachment_sentinel,
                    "images": [],
                    "method": "local_extraction",
                })
            return original_project_preflighted(preflight)

        with mock.patch.object(
            file_handling,
            "_project_preflighted_native_image_manifest",
            side_effect=mutate_source_after_preflight,
        ):
            sealed_run = self._run_proposal(
                mutating_batch,
                output_text=(
                    '{"updates": [{"column": "Total SF", '
                    '"value": "20000", "confidence": 0.98, '
                    '"reason": "Visible in target image."}], '
                    '"events": [], "response_email": null, "notes": ""}'
                ),
                dry_run=False,
            )

        sealed_request_content = (
            sealed_run["client"].responses.create.call_args.kwargs["input"]
            [0]["content"]
        )
        sealed_persist_call = (
            sealed_run["firestore"].collection.return_value
            .document.return_value
            .collection.return_value
            .document.return_value
            .set
        )
        sealed_persisted = sealed_persist_call.call_args.args[0]
        sealed_observable = repr((
            sealed_request_content,
            sealed_persisted,
            sealed_run["print_call"].call_args_list,
        ))
        with self.subTest(snapshot="post_preflight_source_mutation"):
            self.assertEqual(
                (
                    [f"data:image/png;base64,{original_image}"],
                    original_content_type,
                    original_source_bytes,
                    ["20000"],
                    1,
                    0,
                    1,
                    1,
                    False,
                    False,
                    1,
                    4,
                    metadata_sentinel,
                    mutated_source_bytes,
                    2,
                    1,
                    True,
                    1,
                    False,
                ),
                (
                    [
                        item["image_url"]
                        for item in sealed_request_content
                        if item.get("type") == "input_image"
                    ],
                    sealed_persisted["pdfManifest"][0]["image_meta"]
                    [0]["content_type"],
                    sealed_persisted["pdfManifest"][0]["image_meta"]
                    [0]["source_bytes"],
                    [
                        update.get("value")
                        for update in (
                            sealed_run["proposal"] or {}
                        ).get("updates", [])
                    ],
                    sealed_run["client"].responses.create.call_count,
                    sealed_run["client"].files.create.call_count,
                    sealed_run["usage_call"].call_count,
                    sealed_persist_call.call_count,
                    transport_sentinel in sealed_observable,
                    metadata_sentinel in sealed_observable,
                    len(mutation_calls),
                    len(mutating_manifest["images"]),
                    mutating_manifest["image_meta"][0]["content_type"],
                    mutating_manifest["image_meta"][0]["source_bytes"],
                    len(mutating_batch),
                    sealed_run["usage_call"].call_args.kwargs["metadata"]
                    ["pdfCount"],
                    sealed_run["usage_call"].call_args.kwargs["metadata"]
                    ["hasPdfManifest"],
                    len(sealed_persisted["pdfManifest"]),
                    attachment_sentinel in sealed_observable,
                ),
            )

        cycle_manifest = self._single_manifest("single-cycle")
        with mock.patch.object(
            file_handling.base64,
            "b64decode",
            wraps=file_handling.base64.b64decode,
        ) as decode_call, mock.patch.object(
            file_handling,
            "_inspect_native_image_header",
            wraps=file_handling._inspect_native_image_header,
        ) as header_call, mock.patch.object(
            file_handling,
            "_inspect_native_image_pillow_format",
            wraps=file_handling._inspect_native_image_pillow_format,
        ) as pillow_call, mock.patch.object(
            file_handling.hashlib,
            "sha256",
            wraps=file_handling.hashlib.sha256,
        ) as hash_call, mock.patch.object(
            file_handling,
            "_verify_native_image",
            wraps=file_handling._verify_native_image,
        ) as verify_call, mock.patch.object(
            file_handling,
            "_normalize_native_image",
            wraps=file_handling._normalize_native_image,
        ) as normalize_call:
            cycle_run = self._run_proposal([cycle_manifest])

        with self.subTest(snapshot="single_validation_cycle"):
            self.assertEqual(
                (1, 1, 1, 1, 1, 1, 1),
                (
                    decode_call.call_count,
                    header_call.call_count,
                    pillow_call.call_count,
                    hash_call.call_count,
                    verify_call.call_count,
                    normalize_call.call_count,
                    cycle_run["client"].responses.create.call_count,
                ),
            )

        legacy_page = base64.b64encode(
            _png_bytes(size=(4, 3))
        ).decode("ascii")
        legacy_manifest = {
            "name": "sealed legacy brochure.pdf",
            "text": f"{self.TARGET} - Total SF: 20,000.",
            "images": [legacy_page],
            "method": "local_extraction",
            "file_id": None,
            "id": None,
            "nested": {
                "labels": ["original"],
                "flags": {"verified": True},
            },
        }
        legacy_batch = [legacy_manifest]
        expected_legacy_persistence = {
            "name": "sealed legacy brochure.pdf",
            "text": f"{self.TARGET} - Total SF: 20,000.",
            "method": "local_extraction",
            "file_id": None,
            "id": None,
            "nested": {
                "labels": ["original"],
                "flags": {"verified": True},
            },
        }
        legacy_mutation_sentinel = "PRIVATE_POST_PREPARE_LEGACY_SENTINEL"
        original_prepare_attachments = (
            ai_processing._prepare_ai_attachment_manifest
        )
        legacy_prepare_calls = []

        def prepare_then_mutate_legacy(source_manifest):
            prepared = original_prepare_attachments(source_manifest)
            legacy_prepare_calls.append(prepared)
            legacy_manifest.update({
                "name": f"{legacy_mutation_sentinel}.png",
                "text": legacy_mutation_sentinel,
                "method": "openai_upload",
                "source_type": "native_image",
                "file_id": legacy_mutation_sentinel,
                "id": legacy_mutation_sentinel,
                "private": legacy_mutation_sentinel,
            })
            legacy_manifest["images"].extend(
                [legacy_mutation_sentinel] * 3
            )
            legacy_manifest["nested"]["labels"].append(
                legacy_mutation_sentinel
            )
            legacy_manifest["nested"]["flags"]["verified"] = False
            return prepared

        with mock.patch.object(
            ai_processing,
            "_prepare_ai_attachment_manifest",
            side_effect=prepare_then_mutate_legacy,
        ):
            sealed_legacy_run = self._run_proposal(
                legacy_batch,
                output_text=(
                    '{"updates": [{"column": "Total SF", '
                    '"value": "20000", "confidence": 0.98, '
                    '"reason": "Stated in target brochure."}], '
                    '"events": [], "response_email": null, "notes": ""}'
                ),
                dry_run=False,
            )

        sealed_legacy_content = (
            sealed_legacy_run["client"].responses.create.call_args.kwargs
            ["input"][0]["content"]
        )
        sealed_legacy_prompt = next(
            item["text"]
            for item in sealed_legacy_content
            if item.get("type") == "input_text"
        )
        sealed_legacy_persist_call = (
            sealed_legacy_run["firestore"].collection.return_value
            .document.return_value
            .collection.return_value
            .document.return_value
            .set
        )
        sealed_legacy_persisted = (
            sealed_legacy_persist_call.call_args.args[0]
        )
        sealed_legacy_observable = repr((
            sealed_legacy_content,
            sealed_legacy_run["print_call"].call_args_list,
            sealed_legacy_persisted,
        ))
        with self.subTest(snapshot="post_prepare_legacy_mutation"):
            self.assertEqual(
                (
                    [("input_image", f"data:image/png;base64,{legacy_page}")],
                    True,
                    True,
                    ["20000"],
                    [expected_legacy_persistence],
                    [],
                    1,
                    0,
                    1,
                    1,
                    1,
                    4,
                    2,
                    False,
                    False,
                    False,
                ),
                (
                    [
                        (
                            item["type"],
                            item.get("image_url") or item.get("file_id"),
                        )
                        for item in sealed_legacy_content
                        if item.get("type") in ("input_image", "input_file")
                    ],
                    "sealed legacy brochure.pdf" in sealed_legacy_prompt,
                    "Total SF: 20,000." in sealed_legacy_prompt,
                    [
                        update.get("value")
                        for update in (
                            sealed_legacy_run["proposal"] or {}
                        ).get("updates", [])
                    ],
                    sealed_legacy_persisted["pdfManifest"],
                    sealed_legacy_persisted["fileIds"],
                    sealed_legacy_run["client"].responses.create.call_count,
                    sealed_legacy_run["client"].files.create.call_count,
                    sealed_legacy_run["usage_call"].call_count,
                    sealed_legacy_persist_call.call_count,
                    len(legacy_prepare_calls),
                    len(legacy_manifest["images"]),
                    len(legacy_manifest["nested"]["labels"]),
                    legacy_mutation_sentinel in sealed_legacy_observable,
                    "input_file" in repr(sealed_legacy_content),
                    "openai_upload" in sealed_legacy_prompt,
                ),
            )

        classifier_race_page = base64.b64encode(
            _png_bytes(size=(5, 4))
        ).decode("ascii")
        classifier_race_manifest = {
            "name": "classifier race brochure.pdf",
            "text": f"{self.TARGET} - Total SF: 20,000.",
            "images": [classifier_race_page],
            "method": "local_extraction",
            "file_id": None,
            "id": None,
            "nested": {"labels": ["sealed-before-classification"]},
        }
        classifier_race_batch = [classifier_race_manifest]
        classifier_race_sentinel = "PRIVATE_CLASSIFIER_RACE_SENTINEL"
        original_classifier = (
            ai_processing._is_native_image_manifest_candidate
        )
        classifier_aliases = []

        def classify_then_mutate_original(candidate):
            decision = original_classifier(candidate)
            classifier_aliases.append(candidate is classifier_race_manifest)
            if len(classifier_aliases) == 1:
                classifier_race_manifest.clear()
                classifier_race_manifest.update({
                    "name": GENERIC_IMAGE_NAME,
                    "text": "",
                    "images": [classifier_race_sentinel] * 4,
                    "method": "native_image_normalized",
                    "source_type": "native_image",
                    "property_binding": "target",
                    "binding_method": "structured_filename_address",
                    "image_meta": [
                        {"private": classifier_race_sentinel}
                        for _ in range(4)
                    ],
                    "id": classifier_race_sentinel,
                    "file_id": classifier_race_sentinel,
                    "private": classifier_race_sentinel,
                })
            return decision

        with mock.patch.object(
            ai_processing,
            "_is_native_image_manifest_candidate",
            side_effect=classify_then_mutate_original,
        ):
            classifier_race_run = self._run_proposal(
                classifier_race_batch,
                output_text=(
                    '{"updates": [{"column": "Total SF", '
                    '"value": "20000", "confidence": 0.98, '
                    '"reason": "Stated in target brochure."}], '
                    '"events": [], "response_email": null, "notes": ""}'
                ),
                dry_run=False,
            )

        classifier_race_content = (
            classifier_race_run["client"].responses.create.call_args.kwargs
            ["input"][0]["content"]
        )
        classifier_race_prompt = next(
            item["text"]
            for item in classifier_race_content
            if item.get("type") == "input_text"
        )
        classifier_race_persist_call = (
            classifier_race_run["firestore"].collection.return_value
            .document.return_value
            .collection.return_value
            .document.return_value
            .set
        )
        classifier_race_persisted = (
            classifier_race_persist_call.call_args.args[0]
        )
        classifier_race_observable = repr((
            classifier_race_content,
            classifier_race_run["proposal"],
            classifier_race_run["usage_call"].call_args_list,
            classifier_race_run["print_call"].call_args_list,
            classifier_race_persisted,
        ))
        with self.subTest(snapshot="preclassification_legacy_snapshot"):
            self.assertEqual(
                (
                    [(
                        "input_image",
                        f"data:image/png;base64,{classifier_race_page}",
                    )],
                    True,
                    True,
                    ["20000"],
                    [],
                    [{
                        "name": "classifier race brochure.pdf",
                        "text": f"{self.TARGET} - Total SF: 20,000.",
                        "method": "local_extraction",
                        "file_id": None,
                        "id": None,
                        "nested": {
                            "labels": ["sealed-before-classification"],
                        },
                    }],
                    [],
                    1,
                    0,
                    1,
                    1,
                    1,
                    True,
                    True,
                    False,
                    False,
                    [False, False],
                    4,
                ),
                (
                    [
                        (
                            item["type"],
                            item.get("image_url") or item.get("file_id"),
                        )
                        for item in classifier_race_content
                        if item.get("type") in ("input_image", "input_file")
                    ],
                    "classifier race brochure.pdf" in classifier_race_prompt,
                    "Total SF: 20,000." in classifier_race_prompt,
                    [
                        update.get("value")
                        for update in (
                            classifier_race_run["proposal"] or {}
                        ).get("updates", [])
                    ],
                    (
                        classifier_race_run["proposal"] or {}
                    ).get("events", []),
                    classifier_race_persisted["pdfManifest"],
                    classifier_race_persisted["fileIds"],
                    classifier_race_run["client"].responses.create.call_count,
                    classifier_race_run["client"].files.create.call_count,
                    classifier_race_run["usage_call"].call_count,
                    classifier_race_persist_call.call_count,
                    classifier_race_run["usage_call"].call_args.kwargs
                    ["metadata"]["pdfCount"],
                    classifier_race_run["usage_call"].call_args.kwargs
                    ["metadata"]["hasPdfManifest"],
                    len(classifier_race_persisted["pdfManifest"]) == 1,
                    classifier_race_sentinel in classifier_race_observable,
                    "native_image_normalized" in classifier_race_observable,
                    classifier_aliases,
                    len(classifier_race_manifest["images"]),
                ),
            )

        protocol_sentinel = "PRIVATE_PREFREEZE_PROTOCOL_SENTINEL"
        protocol_value = _ExplodingCopyValue(protocol_sentinel)
        protocol_manifest = {
            "name": "protocol control.pdf",
            "text": f"{self.TARGET} - protocol control.",
            "images": [],
            "method": "local_extraction",
            "nested": protocol_value,
        }
        protocol_classifier_inputs = []

        def inspect_protocol_snapshot(candidate):
            protocol_classifier_inputs.append((
                candidate is protocol_manifest,
                dict.get(candidate, "nested") is protocol_value,
            ))
            return original_classifier(candidate)

        with mock.patch.object(
            ai_processing,
            "_is_native_image_manifest_candidate",
            side_effect=inspect_protocol_snapshot,
        ):
            protocol_run = self._run_proposal([protocol_manifest])

        with self.subTest(snapshot="protocol_leaf_is_not_aliased"):
            self.assertEqual(
                (None, 0, 0, 0, [(False, False)], False),
                (
                    protocol_run["proposal"],
                    protocol_run["client"].responses.create.call_count,
                    protocol_run["client"].files.create.call_count,
                    protocol_run["usage_call"].call_count,
                    protocol_classifier_inputs,
                    protocol_sentinel in repr(
                        protocol_run["print_call"].call_args_list
                    ),
                ),
            )

        cyclic_value = []
        cyclic_value.append(cyclic_value)
        cycle_control_manifest = {
            "name": "cycle control.pdf",
            "text": f"{self.TARGET} - cycle control.",
            "images": [],
            "method": "local_extraction",
            "nested": cyclic_value,
        }
        with mock.patch.object(
            ai_processing,
            "_is_native_image_manifest_candidate",
            wraps=original_classifier,
        ) as cycle_classifier:
            cycle_control_run = self._run_proposal(
                [cycle_control_manifest]
            )

        with self.subTest(snapshot="cycle_rejected_before_classification"):
            self.assertEqual(
                (None, 0, 0, 0, 0),
                (
                    cycle_control_run["proposal"],
                    cycle_control_run["client"].responses.create.call_count,
                    cycle_control_run["client"].files.create.call_count,
                    cycle_control_run["usage_call"].call_count,
                    cycle_classifier.call_count,
                ),
            )

    def test_malformed_native_manifest_fails_before_model_or_persistence(self):
        wrong_binding = self._single_manifest("wrong-binding")
        wrong_binding["property_binding"] = "competing"

        wrong_method = self._single_manifest("wrong-method")
        wrong_method["method"] = "native_image_unvalidated"

        bad_cardinality = self._single_manifest("bad-cardinality")
        bad_cardinality["images"].append(bad_cardinality["images"][0])

        bad_hash = self._single_manifest("bad-hash")
        bad_hash["image_meta"][0]["normalized_sha256"] = "0" * 64

        bad_png = self._single_manifest("bad-png")
        invalid_png = b"not a canonical PNG"
        bad_png["images"][0] = base64.b64encode(invalid_png).decode("ascii")
        bad_png["image_meta"][0]["normalized_bytes"] = len(invalid_png)
        bad_png["image_meta"][0]["normalized_sha256"] = hashlib.sha256(
            invalid_png
        ).hexdigest()

        missing_markers = self._single_manifest("missing-markers")
        missing_markers.pop("method")
        missing_markers.pop("source_type")

        corrupt_markers = self._single_manifest("corrupt-markers")
        corrupt_markers["method"] = "native_image_normalized_CORRUPT"
        corrupt_markers["source_type"] = "native_image_CORRUPT"

        marker_subclasses = self._single_manifest("marker-subclasses")
        marker_subclasses["method"] = _PrivateHashString(
            "native_image_normalized",
            "PRIVATE_METHOD_MARKER_SENTINEL",
        )
        marker_subclasses["source_type"] = _PrivateHashString(
            "native_image",
            "PRIVATE_SOURCE_MARKER_SENTINEL",
        )

        manifest_subclass = _PrivateNativeManifestDict(
            self._single_manifest("manifest-subclass")
        )
        manifest_subclass.update({
            "raw_filename": "PRIVATE_SUBCLASS_FILENAME_SENTINEL.png",
            "id": "PRIVATE_SUBCLASS_FILE_ID_SENTINEL",
        })

        casefold_marker_manifests = []
        routing_sentinels = {}
        for label, marker in (
            ("uppercase_native_marker", "NATIVE_IMAGE"),
            ("mixed_case_native_marker", "NaTiVe_ImAgE"),
        ):
            sentinel = f"PRIVATE_{label.upper()}_SENTINEL"
            manifest = self._single_manifest(label)
            manifest.pop("property_binding")
            manifest.pop("binding_method")
            manifest.pop("image_meta")
            manifest.update({
                "source_type": marker,
                "method": "openai_upload",
                "id": sentinel,
                "raw_filename": f"{sentinel}.png",
                "text": sentinel,
            })
            casefold_marker_manifests.append((label, manifest))
            routing_sentinels[label] = sentinel

        malformed_source_type_manifests = []
        for label, marker, method in (
            (
                "space_prefixed_native_marker",
                " native_image",
                "openai_upload",
            ),
            (
                "tab_prefixed_native_marker",
                "\tnative_image",
                "openai_upload",
            ),
            (
                "hyphenated_native_marker",
                "native-image",
                "openai_upload",
            ),
            (
                "newline_native_marker",
                "\nnative_image",
                "openai_upload",
            ),
            (
                "spaced_native_marker",
                "native image",
                "openai_upload",
            ),
            (
                "trailing_space_native_marker",
                "native_image ",
                "openai_upload",
            ),
            ("bytes_native_marker", b"native_image", "openai_upload"),
            (
                "bytearray_native_marker",
                bytearray(b"native_image"),
                "openai_upload",
            ),
            (
                "string_subclass_source_marker",
                _PrivateHashString(
                    "google_drive_pdf",
                    "PRIVATE_SOURCE_TYPE_SUBCLASS_SENTINEL",
                ),
                "local_extraction",
            ),
            (
                "object_native_marker",
                object(),
                "openai_upload",
            ),
            ("failed_google_drive_pdf", "google_drive_pdf", "failed"),
            ("failed_dropbox_pdf", "dropbox_pdf", "failed"),
            ("failed_public_pdf", "public_pdf", "failed"),
            ("failed_direct_image", "direct_image", "failed"),
            (
                "broker_file_share_stub",
                "broker_file_share_link",
                "manual_review_required",
            ),
            (
                "broker_unverified_stub",
                "broker_unverified_property_link",
                "manual_review_required",
            ),
            (
                "unknown_source_type",
                "unknown_linked_asset",
                "openai_upload",
            ),
        ):
            sentinel = f"PRIVATE_{label.upper()}_SENTINEL"
            manifest = self._single_manifest("exterior")
            manifest.pop("property_binding")
            manifest.pop("binding_method")
            manifest.pop("image_meta")
            manifest.update({
                "source_type": marker,
                "method": method,
                "id": sentinel,
                "raw_filename": f"{sentinel}.png",
                "text": sentinel,
            })
            malformed_source_type_manifests.append(
                (label, manifest, sentinel)
            )

        known_pair_with_native_key_sentinel = (
            "PRIVATE_KNOWN_PAIR_NATIVE_KEY_SENTINEL"
        )
        known_pair_with_native_key = {
            "name": "linked.pdf",
            "text": known_pair_with_native_key_sentinel,
            "images": [],
            "method": "local_extraction",
            "source_type": "google_drive_pdf",
            "image_meta": [],
            "id": known_pair_with_native_key_sentinel,
        }
        method_subclass_sentinel = "PRIVATE_METHOD_SUBCLASS_LEGACY_SENTINEL"
        method_subclass_legacy = {
            "name": "legacy.pdf",
            "text": method_subclass_sentinel,
            "images": [],
            "method": _PrivateHashString(
                "local_extraction",
                method_subclass_sentinel,
            ),
            "id": method_subclass_sentinel,
        }
        malformed_source_type_manifests.extend((
            (
                "known_pair_with_native_key",
                known_pair_with_native_key,
                known_pair_with_native_key_sentinel,
            ),
            (
                "method_subclass_legacy",
                method_subclass_legacy,
                method_subclass_sentinel,
            ),
        ))
        for label, method in (
            ("integer_method", 7),
            ("bytes_method", b"local_extraction"),
            ("bytearray_method", bytearray(b"local_extraction")),
            ("object_method", object()),
            ("none_method", None),
        ):
            sentinel = f"PRIVATE_{label.upper()}_SENTINEL"
            malformed_source_type_manifests.append((
                label,
                {
                    "name": "legacy.pdf",
                    "text": sentinel,
                    "images": [],
                    "method": method,
                    "id": sentinel,
                },
                sentinel,
            ))

        for label, method in (
            ("space_prefixed_native_method", " native_image_normalized"),
            ("tab_prefixed_native_method", "\tnative_image_normalized"),
            ("newline_prefixed_native_method", "\nnative_image_normalized"),
            ("hyphenated_native_method", "native-image-normalized"),
            ("spaced_native_method", "native image normalized"),
            ("mixed_separator_native_method", "NaTiVe-Image_Normalized"),
            ("embedded_letter_native_method", "nativeXimage_normalized"),
            (
                "unicode_confusable_native_method",
                "nat\u0131ve_image_normalized",
            ),
            (
                "greek_omicron_native_method",
                "native_image_n\u03bfrmalized",
            ),
            ("unknown_method", "private_custom_parser"),
            ("unscoped_direct_image_method", "direct_image_link"),
        ):
            sentinel = f"PRIVATE_{label.upper()}_SENTINEL"
            malformed_source_type_manifests.append((
                label,
                {
                    "name": "legacy-looking.pdf",
                    "text": sentinel,
                    "images": [],
                    "method": method,
                    "id": sentinel,
                    "raw_filename": f"{sentinel}.png",
                },
                sentinel,
            ))

        for label, variant_key, variant_value, exact_fields in (
            (
                "capitalized_source_type_key",
                "Source_Type",
                "native_image",
                {"method": "openai_upload"},
            ),
            (
                "hyphenated_source_type_key",
                "source-type",
                "native_image",
                {"method": "openai_upload"},
            ),
            (
                "capitalized_method_key",
                "Method",
                "native_image_normalized",
                {},
            ),
            (
                "hyphenated_property_binding_key",
                "Property-Binding",
                "target",
                {
                    "source_type": "google_drive_pdf",
                    "method": "local_extraction",
                },
            ),
            (
                "spaced_binding_method_key",
                "Binding Method",
                "structured_filename_address",
                {
                    "source_type": "dropbox_pdf",
                    "method": "openai_upload",
                },
            ),
            (
                "hyphenated_image_meta_key",
                "Image-Meta",
                [],
                {
                    "source_type": "public_pdf",
                    "method": "local_extraction+images",
                },
            ),
            (
                "greek_omicron_source_type_key",
                "s\u03bfurce_type",
                "native_image",
                {"method": "local_extraction"},
            ),
            (
                "greek_omicron_method_key",
                "meth\u03bfd",
                "native_image_normalized",
                {},
            ),
            (
                "cyrillic_i_property_binding_key",
                "property_bind\u0456ng",
                "target",
                {"method": "local_extraction"},
            ),
            (
                "cyrillic_i_binding_method_key",
                "b\u0456nding_method",
                "structured_filename_address",
                {"method": "local_extraction"},
            ),
            (
                "cyrillic_a_image_meta_key",
                "image_met\u0430",
                [],
                {"method": "local_extraction"},
            ),
            (
                "fullwidth_source_type_key",
                "\uff53\uff4f\uff55\uff52\uff43\uff45\uff3f"
                "\uff54\uff59\uff50\uff45",
                "native_image",
                {"method": "local_extraction"},
            ),
            (
                "fullwidth_method_key",
                "\uff4d\uff45\uff54\uff48\uff4f\uff44",
                "native_image_normalized",
                {},
            ),
            (
                "fullwidth_property_binding_key",
                "\uff50\uff52\uff4f\uff50\uff45\uff52\uff54\uff59\uff3f"
                "\uff42\uff49\uff4e\uff44\uff49\uff4e\uff47",
                "target",
                {"method": "local_extraction"},
            ),
            (
                "fullwidth_binding_method_key",
                "\uff42\uff49\uff4e\uff44\uff49\uff4e\uff47\uff3f"
                "\uff4d\uff45\uff54\uff48\uff4f\uff44",
                "structured_filename_address",
                {"method": "local_extraction"},
            ),
            (
                "fullwidth_image_meta_key",
                "\uff49\uff4d\uff41\uff47\uff45\uff3f"
                "\uff4d\uff45\uff54\uff41",
                [],
                {"method": "local_extraction"},
            ),
        ):
            sentinel = f"PRIVATE_{label.upper()}_SENTINEL"
            manifest = {
                "name": "legacy-looking.pdf",
                "text": sentinel,
                "images": [],
                "id": sentinel,
                "raw_filename": f"{sentinel}.png",
                variant_key: variant_value,
            }
            manifest.update(exact_fields)
            malformed_source_type_manifests.append(
                (label, manifest, sentinel)
            )

        non_plain_key_sentinel = "PRIVATE_NON_PLAIN_KEY_SENTINEL"
        malformed_source_type_manifests.append((
            "non_plain_string_key",
            {
                "name": "legacy-looking.pdf",
                "text": non_plain_key_sentinel,
                "images": [],
                "method": "local_extraction",
                _PrivateHashString(
                    "private_extension",
                    non_plain_key_sentinel,
                ): non_plain_key_sentinel,
            },
            non_plain_key_sentinel,
        ))

        for label, reserved_name in (
            ("exact_reserved_native_name", GENERIC_IMAGE_NAME),
            ("casefold_reserved_native_name", "BROKER PROPERTY IMAGE"),
            ("separator_reserved_native_name", "Broker-property_image"),
            (
                "fullwidth_reserved_native_name",
                "\uff22\uff52\uff4f\uff4b\uff45\uff52\u3000"
                "\uff50\uff52\uff4f\uff50\uff45\uff52\uff54\uff59\u3000"
                "\uff49\uff4d\uff41\uff47\uff45",
            ),
            (
                "greek_omicron_reserved_native_name",
                "Br\u03bfker property image",
            ),
            (
                "unicode_hyphen_reserved_native_name",
                "Broker\u2010property image",
            ),
            (
                "zero_width_reserved_native_name",
                "Broker\u200b property image",
            ),
            (
                "variation_selector_reserved_native_name",
                "Broker\ufe0f property image",
            ),
            (
                "grapheme_joiner_reserved_native_name",
                "Broker\u034f property image",
            ),
            (
                "exact_reserved_native_name_pdf_suffix",
                f"{GENERIC_IMAGE_NAME}.pdf",
            ),
            (
                "fullwidth_reserved_native_name_pdf_suffix",
                "\uff22\uff52\uff4f\uff4b\uff45\uff52\u3000"
                "\uff50\uff52\uff4f\uff50\uff45\uff52\uff54\uff59\u3000"
                "\uff49\uff4d\uff41\uff47\uff45.pdf",
            ),
            (
                "greek_omicron_reserved_native_name_pdf_suffix",
                "Br\u03bfker property image.pdf",
            ),
            (
                "unicode_hyphen_reserved_native_name_pdf_suffix",
                "Broker\u2010property image.pdf",
            ),
            (
                "zero_width_reserved_native_name_pdf_suffix",
                "Broker\u200b property image.pdf",
            ),
            (
                "variation_selector_reserved_native_name_pdf_suffix",
                "Broker\ufe0f property image.pdf",
            ),
            (
                "grapheme_joiner_reserved_native_name_pdf_suffix",
                "Broker\u034f property image.pdf",
            ),
            (
                "dotless_i_reserved_native_name_pdf_suffix",
                "Broker property \u0131mage.pdf",
            ),
            (
                "script_g_reserved_native_name_pdf_suffix",
                "Broker property ima\u0261e.pdf",
            ),
            (
                "reserved_native_name_png_suffix",
                f"{GENERIC_IMAGE_NAME}.png",
            ),
            (
                "reserved_native_name_jpg_suffix",
                f"{GENERIC_IMAGE_NAME}.jpg",
            ),
            (
                "reserved_native_name_jpeg_suffix",
                f"{GENERIC_IMAGE_NAME}.jpeg",
            ),
            (
                "reserved_native_name_webp_suffix",
                f"{GENERIC_IMAGE_NAME}.webp",
            ),
            (
                "reserved_native_name_gif_suffix",
                f"{GENERIC_IMAGE_NAME}.gif",
            ),
        ):
            sentinel = f"PRIVATE_{label.upper()}_SENTINEL"
            downgraded = self._single_manifest(label)
            downgraded.pop("property_binding")
            downgraded.pop("binding_method")
            downgraded.pop("image_meta")
            downgraded.update({
                "name": reserved_name,
                "source_type": "google_drive_pdf",
                "method": "openai_upload",
                "id": sentinel,
                "file_id": sentinel,
                "filename": reserved_name,
                "source_url": (
                    "https://drive.google.com/file/d/forged-native/"
                    f"{quote(reserved_name)}"
                ),
                "drive_link": None,
            })
            malformed_source_type_manifests.append(
                (label, downgraded, sentinel)
            )

        for label, include_benign_name in (
            ("filename_only_reserved_native_name", False),
            ("secondary_filename_reserved_native_name", True),
        ):
            sentinel = f"PRIVATE_{label.upper()}_SENTINEL"
            filename_downgrade = self._single_manifest(label)
            for key in (
                "name", "source_type", "property_binding",
                "binding_method", "image_meta",
            ):
                filename_downgrade.pop(key)
            filename_downgrade.update({
                "filename": f"{GENERIC_IMAGE_NAME}.pdf",
                "method": "local_extraction",
                "id": sentinel,
                "file_id": sentinel,
            })
            if include_benign_name:
                filename_downgrade["name"] = "legacy-looking.pdf"
            malformed_source_type_manifests.append((
                label,
                filename_downgrade,
                sentinel,
            ))

        for label, reserved_name in (
            (
                "producer_shape_dotless_i_reserved_name",
                "Broker property \u0131mage.pdf",
            ),
            (
                "producer_shape_script_g_reserved_name",
                "Broker property ima\u0261e.pdf",
            ),
            (
                "producer_shape_reserved_name_png",
                f"{GENERIC_IMAGE_NAME}.png",
            ),
            (
                "producer_shape_reserved_name_jpg",
                f"{GENERIC_IMAGE_NAME}.jpg",
            ),
            (
                "producer_shape_reserved_name_jpeg",
                f"{GENERIC_IMAGE_NAME}.jpeg",
            ),
            (
                "producer_shape_reserved_name_webp",
                f"{GENERIC_IMAGE_NAME}.webp",
            ),
            (
                "producer_shape_reserved_name_gif",
                f"{GENERIC_IMAGE_NAME}.gif",
            ),
        ):
            sentinel = f"PRIVATE_{label.upper()}_SENTINEL"
            malformed_source_type_manifests.append((
                label,
                {
                    "name": reserved_name,
                    "filename": reserved_name,
                    "text": f"{self.TARGET}\n{sentinel}",
                    "images": [],
                    "method": "openai_upload",
                    "file_id": sentinel,
                    "id": sentinel,
                    "source_type": "public_pdf",
                    "source_url": (
                        "https://assets.example.test/"
                        f"{quote(reserved_name)}"
                    ),
                    "drive_link": None,
                },
                sentinel,
            ))

        over_limit_name_sentinel = (
            "PRIVATE_OVER_LIMIT_RESERVED_NAME_SENTINEL"
        )
        over_limit_reserved_name = (
            "Broker" + "\u200b" * 80 + " property image.pdf"
        )
        malformed_source_type_manifests.append((
            "over_limit_reserved_native_name",
            {
                "name": over_limit_reserved_name,
                "filename": over_limit_reserved_name,
                "text": f"{self.TARGET}\n{over_limit_name_sentinel}",
                "images": [],
                "method": "openai_upload",
                "file_id": over_limit_name_sentinel,
                "id": over_limit_name_sentinel,
                "source_type": "public_pdf",
                "source_url": (
                    "https://assets.example.test/"
                    f"{quote(over_limit_reserved_name)}"
                ),
                "drive_link": None,
            },
            over_limit_name_sentinel,
        ))

        public_trailing_sentinel = "PRIVATE_PUBLIC_TRAILING_SENTINEL"
        malformed_source_type_manifests.append((
            "public_pdf_trailing_slash_fallback",
            {
                "name": "broker flyer.pdf",
                "filename": "broker flyer.pdf",
                "text": f"{self.TARGET}\n{public_trailing_sentinel}",
                "images": [],
                "method": "local_extraction",
                "file_id": None,
                "id": None,
                "source_type": "public_pdf",
                "source_url": "https://assets.example.test/",
                "drive_link": None,
            },
            public_trailing_sentinel,
        ))

        arbitrary_direct_sentinel = "PRIVATE_ARBITRARY_DIRECT_SENTINEL"
        malformed_source_type_manifests.append((
            "generic_direct_image_arbitrary_https_host",
            {
                "name": f"{GENERIC_IMAGE_NAME}.png",
                "text": "",
                "images": [],
                "method": "direct_image_link",
                "source_type": "direct_image",
                "source_url": (
                    "https://assets.example.test/"
                    f"{arbitrary_direct_sentinel}"
                ),
                "drive_link": None,
                "property_image_url": (
                    "https://drive.google.com/uc?export=view&id=arbitrary"
                ),
                "property_image_source": (
                    f"Broker image link: {GENERIC_IMAGE_NAME}.png"
                ),
                "property_image_source_type": "broker_image_link",
                "property_image_meta": {
                    "strategy": "direct_image_link_v1",
                    "selectionReason": "broker-provided public image link",
                    "contentType": "image/png",
                    "byteCount": 17,
                    "sha256": "f" * 64,
                    "driveLink": (
                        "https://drive.google.com/file/d/arbitrary/view"
                    ),
                },
            },
            arbitrary_direct_sentinel,
        ))

        for label, name, source_url in (
            (
                "generic_jpg_name_for_opaque_google_source",
                f"{GENERIC_IMAGE_NAME}.jpg",
                "https://lh3.googleusercontent.com/p/"
                "PRIVATE_GENERIC_JPG_OPAQUE_SENTINEL",
            ),
            (
                "generic_png_name_for_suffixed_google_source",
                f"{GENERIC_IMAGE_NAME}.png",
                "https://lh3.googleusercontent.com/p/"
                "PRIVATE_GENERIC_PNG_SUFFIXED_SENTINEL.jpg",
            ),
        ):
            sentinel = source_url.rsplit("/", 1)[-1].removesuffix(".jpg")
            malformed_source_type_manifests.append((
                label,
                {
                    "name": name,
                    "text": "",
                    "images": [],
                    "method": "direct_image_link",
                    "source_type": "direct_image",
                    "source_url": source_url,
                    "drive_link": None,
                    "property_image_url": (
                        "https://drive.google.com/uc?export=view&id=impossible"
                    ),
                    "property_image_source": f"Broker image link: {name}",
                    "property_image_source_type": "broker_image_link",
                    "property_image_meta": {
                        "strategy": "direct_image_link_v1",
                        "selectionReason": (
                            "broker-provided public image link"
                        ),
                        "contentType": "image/png",
                        "byteCount": 17,
                        "sha256": "9" * 64,
                        "driveLink": (
                            "https://drive.google.com/file/d/impossible/view"
                        ),
                    },
                },
                sentinel,
            ))

        custom_value_sentinel = "PRIVATE_NESTED_CUSTOM_VALUE_SENTINEL"
        malformed_source_type_manifests.append((
            "legacy_nested_custom_value",
            {
                "name": "legacy.pdf",
                "text": f"{self.TARGET}\nLegacy PDF text.",
                "images": [],
                "method": "local_extraction",
                "nested": _ExplodingCopyValue(custom_value_sentinel),
            },
            custom_value_sentinel,
        ))

        nested_subclass_sentinel = "PRIVATE_NESTED_DICT_SUBCLASS_SENTINEL"
        malformed_source_type_manifests.append((
            "legacy_nested_dict_subclass",
            {
                "name": "legacy.pdf",
                "text": f"{self.TARGET}\nLegacy PDF text.",
                "images": [],
                "method": "local_extraction",
                "nested": _PrivateNativeManifestDict({
                    "private": nested_subclass_sentinel,
                }),
            },
            nested_subclass_sentinel,
        ))

        cycle_sentinel = "PRIVATE_LEGACY_CYCLE_SENTINEL"
        cyclic_list = [cycle_sentinel]
        cyclic_list.append(cyclic_list)
        malformed_source_type_manifests.append((
            "legacy_nested_cycle",
            {
                "name": "legacy.pdf",
                "text": f"{self.TARGET}\nLegacy PDF text.",
                "images": [],
                "method": "local_extraction",
                "nested": cyclic_list,
            },
            cycle_sentinel,
        ))

        depth_sentinel = "PRIVATE_LEGACY_DEPTH_SENTINEL"
        deeply_nested = depth_sentinel
        for _ in range(33):
            deeply_nested = [deeply_nested]
        malformed_source_type_manifests.append((
            "legacy_nested_depth",
            {
                "name": "legacy.pdf",
                "text": f"{self.TARGET}\nLegacy PDF text.",
                "images": [],
                "method": "local_extraction",
                "nested": deeply_nested,
            },
            depth_sentinel,
        ))

        preview_protocol_sentinel = (
            "PRIVATE_LINKED_PREVIEW_PROTOCOL_SENTINEL"
        )
        malformed_source_type_manifests.append((
            "linked_preview_non_plain_source_type",
            {
                "name": "linked-preview.pdf",
                "filename": "linked-preview.pdf",
                "text": f"{self.TARGET}\nLegacy PDF text.",
                "images": [],
                "method": "local_extraction",
                "file_id": None,
                "id": None,
                "source_type": "google_drive_pdf",
                "source_url": (
                    "https://drive.google.com/file/d/fixture/"
                    "linked-preview.pdf"
                ),
                "drive_link": None,
                "property_image_url": (
                    "https://drive.google.com/uc?export=view&id=preview"
                ),
                "property_image_source": (
                    "Broker flyer link preview: linked-preview.pdf, page 1"
                ),
                "property_image_source_type": _ExplodingEqualityValue(
                    preview_protocol_sentinel
                ),
                "property_image_meta": {},
            },
            preview_protocol_sentinel,
        ))

        direct_meta_sentinel = "PRIVATE_DIRECT_META_EXTRA_SENTINEL"
        malformed_source_type_manifests.append((
            "direct_image_nested_metadata_extra",
            {
                "name": f"{GENERIC_IMAGE_NAME}.png",
                "text": "",
                "images": [],
                "method": "direct_image_link",
                "source_type": "direct_image",
                "source_url": (
                    "https://lh3.googleusercontent.com/p/direct-meta-fixture"
                ),
                "drive_link": None,
                "property_image_url": (
                    "https://drive.google.com/uc?export=view&id=direct-meta"
                ),
                "property_image_source": (
                    f"Broker image link: {GENERIC_IMAGE_NAME}.png"
                ),
                "property_image_source_type": "broker_image_link",
                "property_image_meta": {
                    "strategy": "direct_image_link_v1",
                    "selectionReason": "broker-provided public image link",
                    "contentType": "image/png",
                    "byteCount": 17,
                    "sha256": "a" * 64,
                    "driveLink": (
                        "https://drive.google.com/file/d/direct-meta/view"
                    ),
                    "raw_filename": direct_meta_sentinel,
                    "exif": {"Comment": direct_meta_sentinel},
                    "unknown": {"private": direct_meta_sentinel},
                },
            },
            direct_meta_sentinel,
        ))

        linked_meta_sentinel = "PRIVATE_LINKED_META_EXTRA_SENTINEL"
        malformed_source_type_manifests.append((
            "linked_pdf_nested_metadata_extra",
            {
                "name": "linked-meta.pdf",
                "filename": "linked-meta.pdf",
                "text": f"{self.TARGET}\nLegacy PDF text.",
                "images": [],
                "method": "local_extraction",
                "file_id": None,
                "id": None,
                "source_type": "public_pdf",
                "source_url": (
                    "https://assets.example.test/linked-meta.pdf"
                ),
                "drive_link": None,
                "property_image_url": (
                    "https://drive.google.com/uc?export=view&id=linked-meta"
                ),
                "property_image_source": (
                    "Broker flyer link preview: linked-meta.pdf, page 1"
                ),
                "property_image_source_type": "broker_pdf_link_preview",
                "property_image_meta": {
                    "pageNumber": 1,
                    "pageCount": 1,
                    "strategy": "first_page_preview_fallback",
                    "selectionReason": (
                        "fallback to first available preview page"
                    ),
                    "score": 0,
                    "signals": {
                        "imageAreaRatio": 0.42,
                        "textChars": 320,
                        "positiveTerms": ["sf", "clear height"],
                        "negativeTerms": [],
                    },
                    "contentType": "image/png",
                    "byteCount": 19,
                    "sha256": "b" * 64,
                    "driveLink": (
                        "https://drive.google.com/file/d/linked-meta/view"
                    ),
                    "raw_filename": linked_meta_sentinel,
                    "exif": {"Comment": linked_meta_sentinel},
                },
            },
            linked_meta_sentinel,
        ))

        linked_signal_sentinel = "PRIVATE_LINKED_SIGNAL_EXTRA_SENTINEL"
        malformed_source_type_manifests.append((
            "linked_pdf_nested_signal_extra",
            {
                "name": "linked-signal.pdf",
                "filename": "linked-signal.pdf",
                "text": f"{self.TARGET}\nLegacy PDF text.",
                "images": [],
                "method": "local_extraction",
                "file_id": None,
                "id": None,
                "source_type": "public_pdf",
                "source_url": (
                    "https://assets.example.test/linked-signal.pdf"
                ),
                "drive_link": None,
                "property_image_url": (
                    "https://drive.google.com/uc?export=view&id=linked-signal"
                ),
                "property_image_source": (
                    "Broker flyer link preview: linked-signal.pdf, page 1"
                ),
                "property_image_source_type": "broker_pdf_link_preview",
                "property_image_meta": {
                    "pageNumber": 1,
                    "pageCount": 1,
                    "strategy": "first_page_preview_fallback",
                    "selectionReason": (
                        "fallback to first available preview page"
                    ),
                    "score": 0,
                    "signals": {
                        "unknown": linked_signal_sentinel,
                    },
                    "contentType": "image/png",
                    "byteCount": 19,
                    "sha256": "b" * 64,
                    "driveLink": (
                        "https://drive.google.com/file/d/linked-signal/view"
                    ),
                },
            },
            linked_signal_sentinel,
        ))

        def assert_strict_rejection(manifest, sentinel=None):
            run = self._run_proposal([manifest], dry_run=False)
            persist_call = (
                run["firestore"].collection.return_value
                .document.return_value
                .collection.return_value
                .document.return_value
                .set
            )
            observable = repr((
                run["client"].responses.create.call_args_list,
                run["client"].files.create.call_args_list,
                persist_call.call_args_list,
                run["print_call"].call_args_list,
            ))
            print_observable = repr(run["print_call"].call_args_list)
            self.assertEqual(
                (None, 0, 0, 0, 0, False, True, False),
                (
                    run["proposal"],
                    run["client"].responses.create.call_count,
                    run["client"].files.create.call_count,
                    run["usage_call"].call_count,
                    persist_call.call_count,
                    bool(sentinel and sentinel in observable),
                    "Refusing malformed native-image attachment manifest"
                    in print_observable,
                    "Failed to propose sheet updates" in print_observable,
                ),
            )

        for label, manifest, sentinel in malformed_source_type_manifests:
            with self.subTest(case=label):
                assert_strict_rejection(manifest, sentinel)

        def legacy_mapping_values(sentinel):
            return {
                "name": "linked.pdf",
                "text": sentinel,
                "images": [],
                "method": "local_extraction",
                "source_type": "google_drive_pdf",
                "id": sentinel,
                "raw_filename": f"{sentinel}.pdf",
            }

        hostile_mapping_cases = []
        for label, factory in (
            (
                "legacy_pair_dict_subclass",
                lambda values, sentinel: _PrivateNativeManifestDict(values),
            ),
            (
                "legacy_pair_user_dict",
                lambda values, sentinel: UserDict(values),
            ),
            (
                "legacy_pair_mapping_proxy",
                lambda values, sentinel: MappingProxyType(values),
            ),
            (
                "exploding_getter_object",
                lambda values, sentinel: _ExplodingGetterManifest(sentinel),
            ),
        ):
            sentinel = f"PRIVATE_{label.upper()}_SENTINEL"
            hostile_mapping_cases.append((
                label,
                factory(legacy_mapping_values(sentinel), sentinel),
                sentinel,
            ))
        hostile_mapping_cases.append(("none_manifest", None, None))

        for label, manifest, sentinel in hostile_mapping_cases:
            with self.subTest(hostile_mapping=label):
                assert_strict_rejection(manifest, sentinel)

        legacy_page = base64.b64encode(
            _png_bytes(size=(4, 3))
        ).decode("ascii")
        legacy_manifests = []
        for source_type in (
            "google_drive_pdf",
            "dropbox_pdf",
            "public_pdf",
        ):
            for method in (
                "local_extraction",
                "local_extraction+images",
                "openai_upload",
                "openai_upload+images",
            ):
                has_images = method.endswith("+images")
                has_file = method.startswith("openai_upload")
                file_id = (
                    f"legacy-{source_type}-{method}"
                    if has_file
                    else None
                )
                if source_type == "google_drive_pdf":
                    linked_name = "view"
                    source_url = (
                        "https://drive.google.com/file/d/legacy-fixture/view"
                    )
                elif source_type == "dropbox_pdf":
                    linked_name = "dropbox-linked.pdf"
                    source_url = (
                        "https://www.dropbox.com/scl/fi/key/"
                        "dropbox-linked.pdf?dl=0"
                    )
                else:
                    linked_name = "public-linked.pdf"
                    source_url = (
                        "https://assets.example.test/public-linked.pdf"
                    )
                legacy_manifests.append((
                    f"{source_type}:{method}",
                    {
                        "name": linked_name,
                        "filename": linked_name,
                        "text": f"{self.TARGET}\nLegacy linked PDF text.",
                        "images": [legacy_page] if has_images else [],
                        "method": method,
                        "file_id": file_id,
                        "id": file_id,
                        "source_type": source_type,
                        "source_url": source_url,
                        "drive_link": None,
                    },
                    (
                        [
                            (
                                "input_image",
                                f"data:image/png;base64,{legacy_page}",
                            )
                        ]
                        if has_images
                        else []
                    ) + (
                        [("input_file", file_id)] if has_file else []
                    ),
                ))

        legacy_manifests.append((
            "public_pdf:local_extraction:preview",
            {
                "name": "public-preview.pdf",
                "filename": "public-preview.pdf",
                "text": f"{self.TARGET}\nLegacy linked PDF text.",
                "images": [],
                "method": "local_extraction",
                "file_id": None,
                "id": None,
                "source_type": "public_pdf",
                "source_url": (
                    "https://assets.example.test/public-preview.pdf"
                ),
                "drive_link": None,
                "property_image_url": (
                    "https://drive.google.com/uc?export=view&id=preview"
                ),
                "property_image_source": (
                    "Broker flyer link preview: public-preview.pdf, page 1"
                ),
                "property_image_source_type": "broker_pdf_link_preview",
                "property_image_meta": {
                    "pageNumber": 1,
                    "pageCount": 1,
                    "strategy": "first_page_preview_fallback",
                    "selectionReason": (
                        "fallback to first available preview page"
                    ),
                    "score": 0,
                    "signals": {
                        "imageAreaRatio": 0.42,
                        "textChars": 320,
                        "positiveTerms": ["sf", "clear height"],
                        "negativeTerms": [],
                    },
                    "contentType": "image/png",
                    "byteCount": 23,
                    "sha256": "c" * 64,
                    "driveLink": (
                        "https://drive.google.com/file/d/preview/view"
                    ),
                },
            },
            [],
        ))

        for source_type, method in (
            ("direct_image", "direct_image_link"),
        ):
            legacy_manifests.append((
                f"{source_type}:{method}",
                {
                    "name": "broker-linked-asset.png",
                    "text": "",
                    "images": [],
                    "method": method,
                    "source_type": source_type,
                    "source_url": (
                        "https://assets.example.test/broker-linked-asset.png"
                    ),
                    "drive_link": None,
                    "property_image_url": (
                        "https://drive.google.com/uc?export=view&id=image"
                    ),
                    "property_image_source": (
                        "Broker image link: broker-linked-asset.png"
                    ),
                    "property_image_source_type": "broker_image_link",
                    "property_image_meta": {
                        "strategy": "direct_image_link_v1",
                        "selectionReason": (
                            "broker-provided public image link"
                        ),
                        "contentType": "image/png",
                        "byteCount": 17,
                        "sha256": "d" * 64,
                        "driveLink": (
                            "https://drive.google.com/file/d/image/view"
                        ),
                    },
                },
                [],
            ))

        for method in (
            "local_extraction",
            "local_extraction+images",
            "openai_upload",
            "openai_upload+images",
            "pdfplumber",
            "local",
            "text",
            "production-replay",
        ):
            has_images = method.endswith("+images")
            has_file = method.startswith("openai_upload")
            file_id = f"legacy-no-source-{method}" if has_file else None
            legacy_manifests.append((
                f"no-source:{method}",
                {
                    "name": "legacy-no-source.pdf",
                    "text": f"{self.TARGET}\nLegacy PDF text.",
                    "images": [legacy_page] if has_images else [],
                    "method": method,
                    "file_id": file_id,
                    "id": file_id,
                },
                (
                    [(
                        "input_image",
                        f"data:image/png;base64,{legacy_page}",
                    )]
                    if has_images
                    else []
                ) + ([('input_file', file_id)] if has_file else []),
            ))

        legacy_manifests.append((
            "no-source:missing-method",
            {
                "name": "legacy-no-source.pdf",
                "text": f"{self.TARGET}\nLegacy PDF text.",
                "images": [],
                "file_id": None,
                "id": None,
            },
            [],
        ))

        for label, manifest, expected_transport in legacy_manifests:
            with self.subTest(legacy_source_pair=label):
                run = self._run_proposal([manifest])
                request_content = (
                    run["client"].responses.create.call_args.kwargs["input"]
                    [0]["content"]
                    if run["client"].responses.create.call_count
                    else []
                )
                transport = [
                    (
                        item["type"],
                        item.get("image_url") or item.get("file_id"),
                    )
                    for item in request_content
                    if item.get("type") in ("input_image", "input_file")
                ]
                self.assertEqual(
                    (True, 1, 0, expected_transport),
                    (
                        run["proposal"] is not None,
                        run["client"].responses.create.call_count,
                        run["client"].files.create.call_count,
                        transport,
                    ),
                )

        drive_view_url = (
            "https://drive.google.com/file/d/actual-producer-fixture/view"
        )
        drive_view_text = (
            f"{self.TARGET}\n"
            + "Actual Google Drive linked PDF producer text. " * 4
        )
        with mock.patch.object(
            file_handling,
            "_download_linked_asset",
            return_value=(b"%PDF-1.4 linked fixture", "application/pdf"),
        ), mock.patch.object(
            file_handling,
            "extract_pdf_text",
            return_value=(drive_view_text, []),
        ), mock.patch.object(
            file_handling,
            "upload_pdf_to_drive",
            return_value=None,
        ), mock.patch.object(
            file_handling,
            "render_pdf_property_preview",
            return_value=None,
        ), mock.patch.object(
            file_handling,
            "render_pdf_first_page_preview",
            return_value=None,
        ), mock.patch(
            "builtins.print",
        ):
            drive_view_manifest = (
                file_handling.fetch_and_process_linked_assets(
                    [drive_view_url]
                )
            )

        with self.subTest(legacy_source_pair="actual_google_drive_view"):
            drive_view_run = self._run_proposal(
                drive_view_manifest,
                dry_run=False,
            )
            drive_view_content = (
                drive_view_run["client"].responses.create.call_args.kwargs
                ["input"][0]["content"]
                if drive_view_run["client"].responses.create.call_count
                else []
            )
            drive_view_prompt = next((
                item.get("text", "")
                for item in drive_view_content
                if item.get("type") == "input_text"
            ), "")
            drive_view_persist_call = (
                drive_view_run["firestore"].collection.return_value
                .document.return_value
                .collection.return_value
                .document.return_value
                .set
            )
            self.assertEqual(
                (
                    1,
                    "view",
                    "view",
                    "google_drive_pdf",
                    "local_extraction",
                    True,
                    1,
                    0,
                    1,
                    1,
                    True,
                ),
                (
                    len(drive_view_manifest),
                    (drive_view_manifest[0] or {}).get("name"),
                    (drive_view_manifest[0] or {}).get("filename"),
                    (drive_view_manifest[0] or {}).get("source_type"),
                    (drive_view_manifest[0] or {}).get("method"),
                    drive_view_run["proposal"] is not None,
                    drive_view_run["client"].responses.create.call_count,
                    drive_view_run["client"].files.create.call_count,
                    drive_view_run["usage_call"].call_count,
                    drive_view_persist_call.call_count,
                    "Actual Google Drive linked PDF producer text."
                    in drive_view_prompt,
                ),
            )

        for trailing_label, trailing_url, expected_source_type in (
            (
                "actual_google_drive_trailing_slash",
                "https://drive.google.com/file/d/trailing-fixture/view/",
                "google_drive_pdf",
            ),
            (
                "actual_dropbox_trailing_slash",
                "https://www.dropbox.com/scl/fi/trailing-fixture/",
                "dropbox_pdf",
            ),
        ):
            with mock.patch.object(
                file_handling,
                "_download_linked_asset",
                return_value=(
                    b"%PDF-1.4 trailing linked fixture",
                    "application/pdf",
                ),
            ), mock.patch.object(
                file_handling,
                "extract_pdf_text",
                return_value=(drive_view_text, []),
            ), mock.patch.object(
                file_handling,
                "upload_pdf_to_drive",
                return_value=None,
            ), mock.patch.object(
                file_handling,
                "render_pdf_property_preview",
                return_value=None,
            ), mock.patch.object(
                file_handling,
                "render_pdf_first_page_preview",
                return_value=None,
            ), mock.patch(
                "builtins.print",
            ):
                trailing_manifest = (
                    file_handling.fetch_and_process_linked_assets(
                        [trailing_url]
                    )
                )

            with self.subTest(legacy_source_pair=trailing_label):
                trailing_run = self._run_proposal(
                    trailing_manifest,
                    dry_run=False,
                )
                trailing_persist_call = (
                    trailing_run["firestore"].collection.return_value
                    .document.return_value
                    .collection.return_value
                    .document.return_value
                    .set
                )
                self.assertEqual(
                    (
                        1,
                        "broker flyer.pdf",
                        "broker flyer.pdf",
                        expected_source_type,
                        True,
                        1,
                        1,
                    ),
                    (
                        len(trailing_manifest),
                        (trailing_manifest[0] or {}).get("name"),
                        (trailing_manifest[0] or {}).get("filename"),
                        (trailing_manifest[0] or {}).get("source_type"),
                        trailing_run["proposal"] is not None,
                        trailing_run["client"].responses.create.call_count,
                        trailing_persist_call.call_count,
                    ),
                )

        direct_image_url = (
            "https://lh3.googleusercontent.com/p/"
            "AF1QipActualProducer=w1200-h800"
        )
        with mock.patch.object(
            file_handling,
            "_download_linked_asset",
            return_value=(b"linked image fixture", "image/jpeg"),
        ), mock.patch.object(
            file_handling,
            "_image_link_to_png_preview",
            return_value=b"normalized linked image fixture",
        ), mock.patch.object(
            file_handling,
            "upload_property_image_to_drive",
            return_value={
                "url": (
                    "https://drive.google.com/uc?export=view&id=linked-image"
                ),
                "driveLink": (
                    "https://drive.google.com/file/d/linked-image/view"
                ),
                "contentType": "image/png",
                "byteCount": 31,
                "sha256": "e" * 64,
            },
        ), mock.patch(
            "builtins.print",
        ):
            direct_image_manifest = (
                file_handling.fetch_and_process_linked_assets(
                    [direct_image_url]
                )
            )

        with self.subTest(legacy_source_pair="actual_direct_image_fallback"):
            direct_image_run = self._run_proposal(direct_image_manifest)
            direct_image_content = (
                direct_image_run["client"].responses.create.call_args.kwargs
                ["input"][0]["content"]
                if direct_image_run["client"].responses.create.call_count
                else []
            )
            self.assertEqual(
                (
                    1,
                    "broker property image.png",
                    "direct_image",
                    "direct_image_link",
                    True,
                    1,
                    0,
                    [],
                ),
                (
                    len(direct_image_manifest),
                    (direct_image_manifest[0] or {}).get("name"),
                    (direct_image_manifest[0] or {}).get("source_type"),
                    (direct_image_manifest[0] or {}).get("method"),
                    direct_image_run["proposal"] is not None,
                    direct_image_run["client"].responses.create.call_count,
                    direct_image_run["client"].files.create.call_count,
                    [
                        item.get("type")
                        for item in direct_image_content
                        if item.get("type")
                        in ("input_image", "input_file")
                    ],
                ),
            )

        normal_direct_image_url = (
            "https://assets.example.test/normal-property-photo.jpg"
        )
        with mock.patch.object(
            file_handling,
            "_download_linked_asset",
            return_value=(b"normal linked image fixture", "image/jpeg"),
        ), mock.patch.object(
            file_handling,
            "_image_link_to_png_preview",
            return_value=b"normalized normal linked image fixture",
        ), mock.patch.object(
            file_handling,
            "upload_property_image_to_drive",
            return_value={
                "url": (
                    "https://drive.google.com/uc?export=view&id=normal-image"
                ),
                "driveLink": (
                    "https://drive.google.com/file/d/normal-image/view"
                ),
                "contentType": "image/png",
                "byteCount": 38,
                "sha256": "8" * 64,
            },
        ), mock.patch(
            "builtins.print",
        ):
            normal_direct_manifest = (
                file_handling.fetch_and_process_linked_assets(
                    [normal_direct_image_url]
                )
            )

        with self.subTest(
            legacy_source_pair="actual_extension_direct_image"
        ):
            normal_direct_run = self._run_proposal(normal_direct_manifest)
            normal_direct_content = (
                normal_direct_run["client"].responses.create.call_args.kwargs
                ["input"][0]["content"]
                if normal_direct_run["client"].responses.create.call_count
                else []
            )
            self.assertEqual(
                (
                    1,
                    "normal-property-photo.jpg",
                    "direct_image",
                    "direct_image_link",
                    True,
                    1,
                    0,
                    [],
                ),
                (
                    len(normal_direct_manifest),
                    (normal_direct_manifest[0] or {}).get("name"),
                    (normal_direct_manifest[0] or {}).get("source_type"),
                    (normal_direct_manifest[0] or {}).get("method"),
                    normal_direct_run["proposal"] is not None,
                    normal_direct_run["client"].responses.create.call_count,
                    normal_direct_run["client"].files.create.call_count,
                    [
                        item.get("type")
                        for item in normal_direct_content
                        if item.get("type")
                        in ("input_image", "input_file")
                    ],
                ),
            )

        claiming_subclass_sentinel = (
            "PRIVATE_CLAIMING_DICT_SUBCLASS_SENTINEL"
        )
        claiming_subclass = _ClaimingNativeManifestDict(
            self._single_manifest("claiming-subclass")
        )
        claiming_subclass.pop("source_type")
        claiming_subclass.pop("property_binding")
        claiming_subclass.pop("binding_method")
        claiming_subclass.pop("image_meta")
        claiming_subclass.update({
            "method": "openai_upload",
            "id": claiming_subclass_sentinel,
            "raw_filename": f"{claiming_subclass_sentinel}.png",
            "text": claiming_subclass_sentinel,
        })
        routing_sentinels["claiming_native_subclass"] = (
            claiming_subclass_sentinel
        )

        hostile_false_marker = self._single_manifest("false-marker")
        hostile_raising_marker = self._single_manifest("raising-marker")
        for hostile, marker_type, sentinel in (
            (
                hostile_false_marker,
                _FalseNativeMarkerString,
                "PRIVATE_FALSE_MARKER_SENTINEL",
            ),
            (
                hostile_raising_marker,
                _RaisingNativeMarkerString,
                "PRIVATE_RAISING_MARKER_SENTINEL",
            ),
        ):
            hostile["method"] = marker_type(
                "native_image_normalized",
                sentinel,
            )
            hostile.pop("source_type")
            hostile.pop("property_binding")
            hostile.pop("binding_method")
            hostile.pop("image_meta")
            hostile.update({
                "raw_filename": f"{sentinel}.png",
                "id": sentinel,
            })

        for name, manifest in (
            ("binding", wrong_binding),
            ("method", wrong_method),
            ("cardinality", bad_cardinality),
            ("hash", bad_hash),
            ("png", bad_png),
            ("missing_markers", missing_markers),
            ("corrupt_markers", corrupt_markers),
            ("marker_subclasses", marker_subclasses),
            ("manifest_subclass", manifest_subclass),
            *casefold_marker_manifests,
            ("claiming_native_subclass", claiming_subclass),
            ("hostile_false_marker", hostile_false_marker),
            ("hostile_raising_marker", hostile_raising_marker),
        ):
            with self.subTest(case=name):
                run = self._run_proposal([manifest], dry_run=False)
                self.assertIsNone(run["proposal"])
                self.assertEqual(0, run["client"].responses.create.call_count)
                self.assertEqual(0, run["client"].files.create.call_count)
                self.assertEqual(0, run["usage_call"].call_count)
                persist_call = (
                    run["firestore"].collection.return_value
                    .document.return_value
                    .collection.return_value
                    .document.return_value
                    .set
                )
                self.assertEqual(0, persist_call.call_count)
                routing_sentinel = routing_sentinels.get(name)
                if routing_sentinel:
                    self.assertNotIn(
                        routing_sentinel,
                        repr((
                            run["client"].responses.create.call_args_list,
                            run["client"].files.create.call_args_list,
                            persist_call.call_args_list,
                            run["print_call"].call_args_list,
                        )),
                    )
                self.assertNotIn(
                    "PRIVATE_FALSE_MARKER_SENTINEL",
                    repr(run["print_call"].call_args_list),
                )
                self.assertNotIn(
                    "PRIVATE_RAISING_MARKER_SENTINEL",
                    repr(run["print_call"].call_args_list),
                )

        projector = getattr(
            file_handling,
            "project_safe_native_image_manifest",
            None,
        )
        self.assertTrue(
            callable(projector),
            "safe native-image manifest projector has not been implemented",
        )
        forged_dimensions = self._single_manifest("forged-dimensions")
        actual_over_edge = _png_bytes(
            size=(file_handling.NATIVE_IMAGE_MAX_EDGE + 1, 1)
        )
        forged_dimensions["images"][0] = base64.b64encode(
            actual_over_edge
        ).decode("ascii")
        forged_dimensions["image_meta"][0]["normalized_bytes"] = len(
            actual_over_edge
        )
        forged_dimensions["image_meta"][0]["normalized_sha256"] = (
            hashlib.sha256(actual_over_edge).hexdigest()
        )
        forbidden = AssertionError(
            "over-limit actual dimensions reached verify/normalization"
        )
        with mock.patch.object(
            file_handling,
            "_verify_native_image",
            side_effect=forbidden,
        ) as verify_call, mock.patch.object(
            file_handling,
            "_normalize_native_image",
            side_effect=forbidden,
        ) as normalize_call:
            try:
                projection = projector(forged_dimensions)
            except AssertionError as exc:
                self.fail(str(exc))
        self.assertIsNone(projection)
        verify_call.assert_not_called()
        normalize_call.assert_not_called()

        oversized_encoded = self._single_manifest("oversized-encoded")
        oversized_payload = b"X" * 12
        oversized_encoded["images"][0] = base64.b64encode(
            oversized_payload
        ).decode("ascii")
        oversized_encoded["image_meta"][0].update({
            "source_bytes": 1,
            "normalized_bytes": 1,
            "normalized_sha256": hashlib.sha256(
                oversized_payload
            ).hexdigest(),
        })
        forbidden_decode = AssertionError(
            "over-limit encoded payload reached base64 decode"
        )
        with mock.patch.object(
            file_handling,
            "NATIVE_IMAGE_MAX_SOURCE_BYTES",
            10,
        ), mock.patch.object(
            file_handling.base64,
            "b64decode",
            side_effect=forbidden_decode,
        ) as decode_call:
            try:
                projection = projector(oversized_encoded)
            except AssertionError as exc:
                self.fail(str(exc))
        self.assertIsNone(projection)
        decode_call.assert_not_called()

        def assert_batch_preflight_rejects(manifest, batch_cap):
            forbidden_resource_work = AssertionError(
                "aggregate-overlimit manifest reached decode or Pillow work"
            )
            with mock.patch.object(
                file_handling,
                "NATIVE_IMAGE_MAX_BATCH_SOURCE_BYTES",
                batch_cap,
            ), mock.patch.object(
                file_handling.base64,
                "b64decode",
                side_effect=forbidden_resource_work,
            ) as batch_decode, mock.patch.object(
                file_handling,
                "_inspect_native_image_pillow_format",
                side_effect=forbidden_resource_work,
            ) as pillow_inspect, mock.patch.object(
                file_handling,
                "_verify_native_image",
                side_effect=forbidden_resource_work,
            ) as batch_verify, mock.patch.object(
                file_handling,
                "_normalize_native_image",
                side_effect=forbidden_resource_work,
            ) as batch_normalize:
                try:
                    batch_projection = projector(manifest)
                except AssertionError as exc:
                    self.fail(str(exc))
            self.assertIsNone(batch_projection)
            batch_decode.assert_not_called()
            pillow_inspect.assert_not_called()
            batch_verify.assert_not_called()
            batch_normalize.assert_not_called()

        three_image_normalized_over = self._manifest([
            ("normalized-a", "png", "image/png", _png_bytes(size=(8, 6))),
            ("normalized-b", "png", "image/png", _png_bytes(size=(7, 5))),
            ("normalized-c", "png", "image/png", _png_bytes(size=(6, 4))),
        ])
        for metadata in three_image_normalized_over["image_meta"]:
            metadata["source_bytes"] = 1
        normalized_total = sum(
            metadata["normalized_bytes"]
            for metadata in three_image_normalized_over["image_meta"]
        )
        with self.subTest(resource_case="batch_normalized_one_over"):
            assert_batch_preflight_rejects(
                three_image_normalized_over,
                normalized_total - 1,
            )

        source_one_over = self._manifest([
            ("source-a", "png", "image/png", _png_bytes(size=(8, 6))),
            ("source-b", "png", "image/png", _png_bytes(size=(7, 5))),
        ])
        source_batch_cap = sum(
            metadata["normalized_bytes"]
            for metadata in source_one_over["image_meta"]
        )
        source_one_over["image_meta"][0]["source_bytes"] = (
            source_batch_cap // 2
        )
        source_one_over["image_meta"][1]["source_bytes"] = (
            source_batch_cap + 1
            - source_one_over["image_meta"][0]["source_bytes"]
        )
        with self.subTest(resource_case="batch_source_one_over"):
            assert_batch_preflight_rejects(
                source_one_over,
                source_batch_cap,
            )

        def assert_request_rejected_before_effects(manifests):
            with mock.patch.object(
                file_handling.base64,
                "b64decode",
                wraps=file_handling.base64.b64decode,
            ) as request_decode, mock.patch.object(
                file_handling,
                "_inspect_native_image_header",
                wraps=file_handling._inspect_native_image_header,
            ) as request_header, mock.patch.object(
                file_handling,
                "_inspect_native_image_pillow_format",
                wraps=file_handling._inspect_native_image_pillow_format,
            ) as request_pillow, mock.patch.object(
                file_handling.hashlib,
                "sha256",
                wraps=file_handling.hashlib.sha256,
            ) as request_hash, mock.patch.object(
                file_handling,
                "_verify_native_image",
                wraps=file_handling._verify_native_image,
            ) as request_verify, mock.patch.object(
                file_handling,
                "_normalize_native_image",
                wraps=file_handling._normalize_native_image,
            ) as request_normalize:
                rejected = self._run_proposal(manifests, dry_run=False)
            rejected_persist = (
                rejected["firestore"].collection.return_value
                .document.return_value
                .collection.return_value
                .document.return_value
                .set
            )
            self.assertEqual(
                (None, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
                (
                    rejected["proposal"],
                    request_decode.call_count,
                    request_header.call_count,
                    request_pillow.call_count,
                    request_hash.call_count,
                    request_verify.call_count,
                    request_normalize.call_count,
                    rejected["client"].responses.create.call_count,
                    rejected["client"].files.create.call_count,
                    rejected["usage_call"].call_count,
                    rejected_persist.call_count,
                ),
            )

        with self.subTest(resource_case="request_native_asset_count_four"):
            count_manifests = [
                self._single_manifest(f"request-count-{label}")
                for label in ("alpha", "beta", "gamma", "delta")
            ]
            legacy_interleaved = {
                "name": "legacy.pdf",
                "text": f"{self.TARGET}\nLegacy text.",
                "images": [],
                "method": "local_extraction",
                "file_id": None,
                "id": None,
            }
            assert_request_rejected_before_effects([
                count_manifests[0],
                legacy_interleaved,
                *count_manifests[1:],
            ])

        malformed_after_valid = self._single_manifest(
            "request-malformed-second"
        )
        malformed_after_valid["method"] = "native_image_unvalidated"
        with self.subTest(resource_case="valid_then_malformed_native"):
            assert_request_rejected_before_effects([
                self._single_manifest("request-valid-first"),
                malformed_after_valid,
            ])

        request_source_manifests = [
            self._single_manifest("request-source-a"),
            self._single_manifest("request-source-b"),
        ]
        request_source_cap = sum(
            manifest["image_meta"][0]["normalized_bytes"]
            for manifest in request_source_manifests
        )
        first_request_source = request_source_cap // 2
        request_source_manifests[0]["image_meta"][0]["source_bytes"] = (
            first_request_source
        )
        request_source_manifests[1]["image_meta"][0]["source_bytes"] = (
            request_source_cap + 1 - first_request_source
        )
        for order, manifests in (
            ("forward", request_source_manifests),
            ("reverse", list(reversed(request_source_manifests))),
        ):
            with self.subTest(
                resource_case="request_source_one_over",
                order=order,
            ):
                with mock.patch.object(
                    file_handling,
                    "NATIVE_IMAGE_MAX_BATCH_SOURCE_BYTES",
                    request_source_cap,
                ):
                    assert_request_rejected_before_effects(manifests)

        request_normalized_manifests = [
            self._single_manifest("request-normalized-a"),
            self._single_manifest("request-normalized-b"),
        ]
        for manifest in request_normalized_manifests:
            manifest["image_meta"][0]["source_bytes"] = 1
        request_normalized_total = sum(
            manifest["image_meta"][0]["normalized_bytes"]
            for manifest in request_normalized_manifests
        )
        for order, manifests in (
            ("forward", request_normalized_manifests),
            ("reverse", list(reversed(request_normalized_manifests))),
        ):
            with self.subTest(
                resource_case="request_normalized_one_over",
                order=order,
            ):
                with mock.patch.object(
                    file_handling,
                    "NATIVE_IMAGE_MAX_BATCH_SOURCE_BYTES",
                    request_normalized_total - 1,
                ):
                    assert_request_rejected_before_effects(manifests)

        exact_count_manifests = [
            self._single_manifest(f"exact-count-{label}")
            for label in ("alpha", "beta", "gamma")
        ]
        with self.subTest(resource_case="request_count_exact_boundary"):
            exact_count_run = self._run_proposal([
                exact_count_manifests[0],
                legacy_interleaved,
                *exact_count_manifests[1:],
            ])
            self.assertIsNotNone(exact_count_run["proposal"])
            self.assertEqual(
                1,
                exact_count_run["client"].responses.create.call_count,
            )

        exact_source_manifests = [
            self._single_manifest("exact-source-a"),
            self._single_manifest("exact-source-b"),
        ]
        exact_normalized_total = sum(
            manifest["image_meta"][0]["normalized_bytes"]
            for manifest in exact_source_manifests
        )
        exact_source_cap = exact_normalized_total + 2
        exact_source_manifests[0]["image_meta"][0]["source_bytes"] = 1
        exact_source_manifests[1]["image_meta"][0]["source_bytes"] = (
            exact_source_cap - 1
        )
        with self.subTest(resource_case="request_source_exact_boundary"):
            with mock.patch.object(
                file_handling,
                "NATIVE_IMAGE_MAX_BATCH_SOURCE_BYTES",
                exact_source_cap,
            ):
                exact_source_run = self._run_proposal(
                    exact_source_manifests
                )
            self.assertIsNotNone(exact_source_run["proposal"])
            self.assertEqual(
                1,
                exact_source_run["client"].responses.create.call_count,
            )

        exact_normalized_manifests = [
            self._single_manifest("exact-normalized-a"),
            self._single_manifest("exact-normalized-b"),
        ]
        for manifest in exact_normalized_manifests:
            manifest["image_meta"][0]["source_bytes"] = 1
        exact_normalized_cap = sum(
            manifest["image_meta"][0]["normalized_bytes"]
            for manifest in exact_normalized_manifests
        )
        with self.subTest(
            resource_case="request_normalized_exact_boundary"
        ):
            with mock.patch.object(
                file_handling,
                "NATIVE_IMAGE_MAX_BATCH_SOURCE_BYTES",
                exact_normalized_cap,
            ):
                exact_normalized_run = self._run_proposal(
                    exact_normalized_manifests
                )
            self.assertIsNotNone(exact_normalized_run["proposal"])
            self.assertEqual(
                1,
                exact_normalized_run["client"].responses.create.call_count,
            )

        def assert_snapshot_rejected_without_effects(run):
            persist_call = (
                run["firestore"].collection.return_value
                .document.return_value
                .collection.return_value
                .document.return_value
                .set
            )
            self.assertEqual(
                (None, 0, 0, 0, 0),
                (
                    run["proposal"],
                    run["client"].responses.create.call_count,
                    run["client"].files.create.call_count,
                    run["usage_call"].call_count,
                    persist_call.call_count,
                ),
            )

        over_count = self._manifest([
            ("snapshot-a", "png", "image/png", _png_bytes(size=(8, 6))),
            ("snapshot-b", "png", "image/png", _png_bytes(size=(7, 5))),
            ("snapshot-c", "png", "image/png", _png_bytes(size=(6, 4))),
        ])
        fourth_manifest = self._single_manifest("snapshot-fourth")
        fourth_image = fourth_manifest["images"][0]
        fourth_meta = fourth_manifest["image_meta"][0]
        over_count["images"].append(fourth_image)
        over_count["image_meta"].append(fourth_meta)
        original_freeze = ai_processing._freeze_legacy_json_value
        with self.subTest(resource_case="snapshot_native_count_preflight"):
            with mock.patch.object(
                ai_processing,
                "_freeze_legacy_json_value",
                wraps=original_freeze,
            ) as freeze_call:
                over_count_run = self._run_proposal(
                    [over_count],
                    dry_run=False,
                )
            assert_snapshot_rejected_without_effects(over_count_run)
            visited_values = [
                call.args[0]
                for call in freeze_call.call_args_list
                if call.args
            ]
            self.assertFalse(any(
                value is fourth_image or value is fourth_meta
                for value in visited_values
            ))

        aggregate_count = [
            self._single_manifest(descriptor, size=(8 + index, 6 + index))
            for index, descriptor in enumerate((
                "request-alpha",
                "request-beta",
                "request-gamma",
                "request-delta",
            ))
        ]
        fourth_request_image = aggregate_count[3]["images"][0]
        fourth_request_meta = aggregate_count[3]["image_meta"][0]
        fourth_request_sentinel = (
            "PRIVATE_SNAPSHOT_FOURTH_NATIVE_SENTINEL"
        )
        aggregate_count[3]["ignored_tree"] = [fourth_request_sentinel]
        with self.subTest(
            resource_case="snapshot_request_native_count_preflight"
        ):
            with mock.patch.object(
                ai_processing,
                "_freeze_legacy_json_value",
                wraps=original_freeze,
            ) as freeze_call:
                aggregate_count_run = self._run_proposal(
                    aggregate_count,
                    dry_run=False,
                )
            assert_snapshot_rejected_without_effects(aggregate_count_run)
            visited_values = [
                call.args[0]
                for call in freeze_call.call_args_list
                if call.args
            ]
            self.assertFalse(any(
                value is fourth_request_image
                or value is fourth_request_meta
                or value is fourth_request_sentinel
                for value in visited_values
            ))

        over_width = self._single_manifest("snapshot-over-width")
        first_width_sentinel = "PRIVATE_SNAPSHOT_WIDTH_FIRST_SENTINEL"
        last_width_sentinel = "PRIVATE_SNAPSHOT_WIDTH_LAST_SENTINEL"
        over_width["ignored_tree"] = [
            first_width_sentinel,
            *[f"snapshot-width-{index}" for index in range(63)],
            last_width_sentinel,
        ]
        with self.subTest(resource_case="snapshot_container_width_preflight"):
            with mock.patch.object(
                ai_processing,
                "_ATTACHMENT_SNAPSHOT_MAX_CONTAINER_ITEMS",
                64,
                create=True,
            ), mock.patch.object(
                ai_processing,
                "_ATTACHMENT_SNAPSHOT_MAX_NODES",
                4096,
                create=True,
            ), mock.patch.object(
                ai_processing,
                "_freeze_legacy_json_value",
                wraps=original_freeze,
            ) as freeze_call:
                over_width_run = self._run_proposal(
                    [over_width],
                    dry_run=False,
                )
            assert_snapshot_rejected_without_effects(over_width_run)
            visited_values = [
                call.args[0]
                for call in freeze_call.call_args_list
                if call.args
            ]
            self.assertFalse(any(
                value is first_width_sentinel
                or value is last_width_sentinel
                for value in visited_values
            ))

        def build_snapshot_node_boundary(*, one_over):
            manifest = self._single_manifest("snapshot-node-budget")
            branches = [
                [
                    f"snapshot-node-{outer}-{inner}"
                    for inner in range(64)
                ]
                for outer in range(62)
            ]
            branches.extend([
                [f"snapshot-node-tail-{inner}" for inner in range(45)],
                [],
            ])
            if one_over:
                branches[-1].append(
                    "PRIVATE_SNAPSHOT_NODE_SENTINEL"
                )
            manifest["ignored_tree"] = branches
            return manifest

        ignored_budget = build_snapshot_node_boundary(one_over=True)
        ignored_node_sentinel = ignored_budget["ignored_tree"][-1][0]
        with self.subTest(resource_case="snapshot_ignored_tree_node_budget"):
            with mock.patch.object(
                ai_processing,
                "_ATTACHMENT_SNAPSHOT_MAX_CONTAINER_ITEMS",
                64,
                create=True,
            ), mock.patch.object(
                ai_processing,
                "_ATTACHMENT_SNAPSHOT_MAX_NODES",
                4096,
                create=True,
            ), mock.patch.object(
                ai_processing,
                "_freeze_legacy_json_value",
                wraps=original_freeze,
            ) as freeze_call:
                ignored_budget_run = self._run_proposal(
                    [ignored_budget],
                    dry_run=False,
                )
            assert_snapshot_rejected_without_effects(ignored_budget_run)
            self.assertLessEqual(freeze_call.call_count, 4097)
            self.assertFalse(any(
                call.args and call.args[0] is ignored_node_sentinel
                for call in freeze_call.call_args_list
            ))

        exact_snapshot_boundary = build_snapshot_node_boundary(
            one_over=False
        )
        with self.subTest(
            resource_case="snapshot_exact_width_and_node_boundaries"
        ):
            with mock.patch.object(
                ai_processing,
                "_ATTACHMENT_SNAPSHOT_MAX_CONTAINER_ITEMS",
                64,
                create=True,
            ), mock.patch.object(
                ai_processing,
                "_ATTACHMENT_SNAPSHOT_MAX_NODES",
                4096,
                create=True,
            ):
                exact_snapshot_run = self._run_proposal(
                    [exact_snapshot_boundary],
                    dry_run=False,
                )
            exact_snapshot_persist = (
                exact_snapshot_run["firestore"].collection.return_value
                .document.return_value
                .collection.return_value
                .document.return_value
                .set
            )
            self.assertEqual(
                (True, 1, 0, 1, 1),
                (
                    exact_snapshot_run["proposal"] is not None,
                    exact_snapshot_run[
                        "client"
                    ].responses.create.call_count,
                    exact_snapshot_run["client"].files.create.call_count,
                    exact_snapshot_run["usage_call"].call_count,
                    exact_snapshot_persist.call_count,
                ),
            )

    def test_mixed_native_and_scanned_pdf_preserves_pdf_file_semantics(self):
        native_manifest = self._single_manifest()
        native_url = f"data:image/png;base64,{native_manifest['images'][0]}"
        scanned_page = _png_bytes(size=(4, 3))
        scanned_page_url = (
            "data:image/png;base64,"
            + base64.b64encode(scanned_page).decode("ascii")
        )
        fake_client = self._fake_client(file_id="scanned-pdf-file")
        fake_fs = mock.Mock()

        with mock.patch.object(
            ai_processing,
            "client",
            fake_client,
        ), mock.patch.object(
            file_handling,
            "client",
            fake_client,
        ), mock.patch.object(
            ai_processing,
            "_fs",
            fake_fs,
        ), mock.patch.object(
            ai_processing,
            "track_openai_usage_safely",
        ) as usage_call, mock.patch.object(
            file_handling,
            "extract_pdf_text",
            return_value=("", [scanned_page]),
        ), mock.patch(
            "builtins.print",
        ):
            pdf_manifest = file_handling.process_pdf_for_ai(
                b"%PDF-1.4 bounded scanned fixture",
                "scanned.pdf",
            )
            pdf_manifest["name"] = "scanned.pdf"
            pdf_manifest["text"] = (
                f"{self.TARGET}\nLEGACY_PDF_TEXT_SENTINEL"
            )
            proposal = ai_processing.propose_sheet_updates(
                uid="native-image-user",
                client_id="native-image-client",
                email="broker@example.com",
                sheet_id="native-image-sheet",
                header=["Property Address", "Total SF"],
                rownum=3,
                rowvals=[self.TARGET, ""],
                thread_id="native-image-thread",
                pdf_manifest=[native_manifest, pdf_manifest],
                conversation=[{
                    "direction": "inbound",
                    "from": "broker@example.com",
                    "content": "The target image and scanned PDF are attached.",
                }],
                column_config=self._column_config(),
                extraction_fields=["total_sf"],
                dry_run=False,
            )

        self.assertIsNotNone(proposal)
        self.assertEqual(1, fake_client.files.create.call_count)
        self.assertEqual(
            "user_data",
            fake_client.files.create.call_args.kwargs["purpose"],
        )
        self.assertEqual(1, fake_client.responses.create.call_count)
        self.assertEqual(1, usage_call.call_count)
        request_content = (
            fake_client.responses.create.call_args.kwargs["input"][0]["content"]
        )
        def transport_signature(content):
            return [
                (
                    item["type"],
                    item.get("image_url") or item.get("file_id"),
                )
                for item in content
                if item.get("type") in ("input_image", "input_file")
            ]

        self.assertEqual(
            [native_url, scanned_page_url],
            [
                item["image_url"]
                for item in request_content
                if item.get("type") == "input_image"
            ],
        )
        self.assertEqual(
            [{"type": "input_file", "file_id": "scanned-pdf-file"}],
            [
                item
                for item in request_content
                if item.get("type") == "input_file"
            ],
        )
        self.assertEqual(
            [
                ("input_image", native_url),
                ("input_image", scanned_page_url),
                ("input_file", "scanned-pdf-file"),
            ],
            transport_signature(request_content),
        )
        prompt = next(
            item["text"]
            for item in request_content
            if item.get("type") == "input_text"
        )
        self.assertIn("LEGACY_PDF_TEXT_SENTINEL", prompt)
        native_first_descriptor = (
            "Attachment 1: type=prevalidated_native_target_images; "
            "image_count=1"
        )
        pdf_second_descriptor = (
            "Attachment 2: type=legacy_pdf; preview_image_count=1; "
            "input_file_fallback=yes"
        )
        with self.subTest(order="native_then_pdf"):
            self.assertIn(native_first_descriptor, prompt)
            self.assertIn(pdf_second_descriptor, prompt)
            self.assertLess(
                prompt.index(native_first_descriptor),
                prompt.index(pdf_second_descriptor),
            )

        reversed_run = self._run_proposal(
            [pdf_manifest, native_manifest],
            fake_client=self._fake_client(file_id="scanned-pdf-file"),
        )
        reversed_content = (
            reversed_run["client"].responses.create.call_args.kwargs["input"]
            [0]["content"]
        )
        self.assertEqual(
            [
                ("input_image", scanned_page_url),
                ("input_file", "scanned-pdf-file"),
                ("input_image", native_url),
            ],
            transport_signature(reversed_content),
        )
        self.assertEqual(0, reversed_run["client"].files.create.call_count)
        reversed_prompt = next(
            item["text"]
            for item in reversed_content
            if item.get("type") == "input_text"
        )
        pdf_first_descriptor = (
            "Attachment 1: type=legacy_pdf; preview_image_count=1; "
            "input_file_fallback=yes"
        )
        native_second_descriptor = (
            "Attachment 2: type=prevalidated_native_target_images; "
            "image_count=1"
        )
        with self.subTest(order="pdf_then_native"):
            self.assertIn(pdf_first_descriptor, reversed_prompt)
            self.assertIn(native_second_descriptor, reversed_prompt)
            self.assertLess(
                reversed_prompt.index(pdf_first_descriptor),
                reversed_prompt.index(native_second_descriptor),
            )
        persist_call = (
            fake_fs.collection.return_value
            .document.return_value
            .collection.return_value
            .document.return_value
            .set
        )
        self.assertEqual(1, persist_call.call_count)
        persisted = persist_call.call_args.args[0]
        projector = getattr(
            file_handling,
            "project_safe_native_image_manifest",
            None,
        )
        self.assertTrue(
            callable(projector),
            "safe native-image manifest projector has not been implemented",
        )
        self.assertEqual(
            projector(native_manifest),
            persisted["pdfManifest"][0],
        )
        self.assertEqual(
            {key: value for key, value in pdf_manifest.items() if key != "images"},
            persisted["pdfManifest"][1],
        )
        self.assertEqual(["scanned-pdf-file"], persisted["fileIds"])


class NativeImageProcessingIntegrationTests(unittest.TestCase):
    TARGET = "123 North Sample Road, Example City, AZ 85001"
    MATCHING_FILENAME = "123 N Sample Rd Example City AZ 85001"

    @staticmethod
    def _column_config():
        return {
            "mappings": {"total_sf": "Total SF"},
            "extractionFields": ["total_sf"],
            "requiredFields": [],
            "formulaFields": [],
            "neverRequest": [],
            "customFields": {},
        }

    @staticmethod
    def _graph_page(values, next_link=None):
        response = mock.MagicMock()
        response.status_code = 200
        payload = {"value": list(values)}
        if next_link:
            payload["@odata.nextLink"] = next_link
        response.json.return_value = payload
        return response

    def _run_process(
        self,
        *,
        attachments,
        body="",
        property_address=None,
        city="",
        property_image="",
        proposal=None,
        apply_result=None,
        graph_pages=None,
        host_result=None,
        proposal_side_effect=None,
        include_property_image_column=True,
        boundary_order=None,
    ):
        from tests.test_compound_nonviable_processing import (
            FakeDocumentRef,
            FakeFirestore,
        )

        property_address = (
            self.TARGET if property_address is None else property_address
        )
        thread_id = "native-image-processing-thread"
        thread_ref = FakeDocumentRef({
            "status": "active",
            "clientId": "native-image-client",
            "email": ["broker@example.test"],
        })
        client_ref = FakeDocumentRef({"criteria": "Industrial search"})
        header = [
            "Property Address",
            "City",
            "Leasing Contact",
            "Email",
            "Total SF",
        ]
        rowvals = [
            property_address,
            city,
            "Dana",
            "broker@example.test",
            "",
        ]
        if include_property_image_column:
            header[4:4] = ["Property Image", "Property Image Source"]
            rowvals[4:4] = [property_image, ""]
        msg = {
            "id": "native-image-message",
            "subject": "RE: target property",
            "from": {
                "emailAddress": {
                    "address": "broker@example.test",
                    "name": "Dana",
                }
            },
            "toRecipients": [
                {"emailAddress": {"address": "me@ourdomain.test"}}
            ],
            "internetMessageId": "<native-image-message@example.test>",
            "conversationId": "native-image-conversation",
            "receivedDateTime": "2026-08-16T08:00:00Z",
            "bodyPreview": body[:200],
            "hasAttachments": True,
            "internetMessageHeaders": [
                {"name": "In-Reply-To", "value": "<our-outbound@example.test>"},
            ],
        }

        full_body_response = mock.MagicMock()
        full_body_response.json.return_value = {
            "body": {"content": body, "contentType": "Text"},
            "hasAttachments": True,
        }
        pages = list(graph_pages or [self._graph_page(attachments)])
        graph_get = mock.MagicMock(side_effect=pages)

        default_proposal = {
            "updates": [],
            "events": [],
            "response_email": None,
            "notes": "",
            "skip_response": True,
        }
        proposal_value = dict(default_proposal)
        if proposal is not None:
            proposal_value.update(proposal)
        propose = mock.MagicMock(
            side_effect=proposal_side_effect,
            return_value=proposal_value,
        )
        applied = apply_result
        if applied is None:
            applied = {
                "applied": list(proposal_value.get("updates") or []),
                "skipped": [],
            }
        apply_proposal = mock.MagicMock(return_value=applied)
        linked_assets = mock.MagicMock(return_value=[])
        warning_recorder = mock.MagicMock(return_value=True)
        send_reply = mock.MagicMock(return_value=True)
        resolved_host_result = (
            host_result
            if host_result is not None
            else {
                "url": "https://drive.google.com/uc?export=view&id=native-hosted",
                "driveLink": "PRIVATE_DRIVE_VIEW_LINK",
                "contentType": "image/png",
                "byteCount": 123,
                "sha256": "host-returned-sha",
            }
        )

        def host_side_effect(*_args, **_kwargs):
            if boundary_order is not None:
                boundary_order.append("host")
            return resolved_host_result

        host_upload = mock.MagicMock(side_effect=host_side_effect)
        image_writer = mock.MagicMock(
            side_effect=lambda _sheets, _sheet_id, _header, _rownum, updates: updates
        )
        image_change_store = mock.MagicMock(return_value="image-change")
        real_validator = (
            file_handling.validate_and_normalize_native_image_attachments
        )

        def validator_side_effect(*args, **kwargs):
            if boundary_order is not None:
                boundary_order.append("validate")
            return real_validator(*args, **kwargs)

        validator_spy = mock.MagicMock(side_effect=validator_side_effect)

        campaign_decision = CampaignAutomationDecision(
            state="allow",
            reason="",
            client_data={"status": "live", "automationPaused": False},
            metadata={
                "source": "systemConfig/campaignAccess",
                "terminal": False,
            },
        )
        fake_sheets = mock.MagicMock()
        patchers = [
            mock.patch.object(
                processing,
                "_fs",
                FakeFirestore(thread_ref, client_ref),
            ),
            mock.patch.object(
                processing,
                "get_client_automation_decision",
                return_value=campaign_decision,
            ),
            mock.patch.object(
                processing,
                "exponential_backoff_request",
                return_value=full_body_response,
            ),
            mock.patch.object(
                processing,
                "lookup_thread_by_message_id",
                return_value=thread_id,
            ),
            mock.patch.object(
                processing,
                "lookup_thread_by_conversation_id",
                return_value=None,
            ),
            mock.patch.object(
                processing,
                "get_thread_status",
                return_value=processing.THREAD_STATUS["active"],
            ),
            mock.patch.object(processing, "save_message", return_value=True),
            mock.patch.object(processing, "index_message_id", return_value=True),
            mock.patch.object(processing, "dump_thread_from_firestore"),
            mock.patch("email_automation.followup.cancel_followup_on_response"),
            mock.patch.object(
                processing,
                "fetch_and_log_sheet_for_thread",
                return_value=(
                    "native-image-client",
                    "native-image-sheet",
                    header,
                    3,
                    rowvals,
                    self._column_config(),
                    ["total_sf"],
                ),
            ),
            mock.patch.object(
                processing,
                "_resolve_reply_identity",
                return_value={
                    "recipient_email": "broker@example.test",
                    "contact_name": "Dana",
                    "original_email": "broker@example.test",
                    "source": "test",
                },
            ),
            mock.patch.object(processing, "write_message_order_test"),
            mock.patch.object(processing, "fetch_url_as_text", return_value=None),
            mock.patch.object(
                processing,
                "fetch_and_process_linked_assets",
                new=linked_assets,
            ),
            mock.patch.object(
                processing,
                "propose_sheet_updates",
                new=propose,
            ),
            mock.patch.object(
                processing,
                "apply_proposal_to_sheet",
                new=apply_proposal,
            ),
            mock.patch.object(processing, "add_client_notifications"),
            mock.patch.object(
                processing,
                "_record_asset_extraction_warning",
                new=warning_recorder,
            ),
            mock.patch.object(processing, "_sheets_client", return_value=fake_sheets),
            mock.patch.object(processing, "_get_first_tab_title", return_value="Sheet1"),
            mock.patch.object(processing, "_read_header_row2", return_value=header),
            mock.patch.object(processing, "format_sheet_columns_autosize_with_exceptions"),
            mock.patch.object(processing, "write_property_image_columns", new=image_writer),
            mock.patch.object(
                processing,
                "_store_property_image_sheet_change",
                new=image_change_store,
            ),
            mock.patch.object(processing, "_append_ai_meta"),
            mock.patch.object(processing, "write_notification", return_value="notification"),
            mock.patch.object(processing, "is_event_handled", return_value=False),
            mock.patch.object(processing, "mark_event_handled"),
            mock.patch.object(processing, "update_thread_status"),
            mock.patch.object(processing, "highlight_row"),
            mock.patch.object(processing, "send_reply_in_thread", new=send_reply),
            mock.patch.object(file_handling.requests, "get", new=graph_get),
            mock.patch.object(
                file_handling,
                "validate_and_normalize_native_image_attachments",
                new=validator_spy,
            ),
            mock.patch.object(
                file_handling,
                "upload_property_image_to_drive",
                new=host_upload,
            ),
        ]

        raised = None
        with contextlib.ExitStack() as stack:
            for patcher in patchers:
                stack.enter_context(patcher)
            try:
                processing.process_inbox_message(
                    "native-image-user",
                    {"Authorization": "Bearer fake"},
                    msg,
                    allow_outbound_reply=False,
                    authenticated_mailbox_email="me@ourdomain.test",
                )
            except Exception as exc:  # noqa: BLE001 - assertion inspects boundary
                raised = exc

        return {
            "error": raised,
            "propose": propose,
            "apply": apply_proposal,
            "linked": linked_assets,
            "warning": warning_recorder,
            "send": send_reply,
            "host": host_upload,
            "image_writer": image_writer,
            "image_store": image_change_store,
            "validator": validator_spy,
            "graph_get": graph_get,
        }

    def _valid_attachment(self, descriptor="exterior", size=(9, 7)):
        return _attachment(
            f"{self.MATCHING_FILENAME} {descriptor}.png",
            "image/png",
            _png_bytes(size=size),
        )

    def test_row_anchor_reaches_native_binding_and_linked_asset_hint(self):
        run = self._run_process(
            attachments=[self._valid_attachment()],
            body="Photos are attached. https://example.com/details",
        )

        self.assertIsNone(run["error"])
        run["validator"].assert_called_once()
        self.assertEqual(
            self.TARGET,
            run["validator"].call_args.kwargs["target_property_hint"],
        )
        run["linked"].assert_called_once_with(
            ["https://example.com/details"],
            target_property_hint=self.TARGET,
        )
        self.assertIs(
            run["validator"].call_args.kwargs["target_property_hint"],
            run["linked"].call_args.kwargs["target_property_hint"],
        )

    def test_valid_image_only_message_reaches_vision_not_processed_noop(self):
        response = mock.MagicMock()
        response.output_text = (
            '{"updates": [{"column": "Total SF", "value": "18500", '
            '"confidence": 0.99}], "events": [], "response_email": null, '
            '"notes": ""}'
        )
        response.usage = None
        response.id = "native-processing-vision-response"
        fake_client = mock.MagicMock()
        fake_client.responses.create.return_value = response

        def real_vision_proposal(*args, **kwargs):
            return ai_processing.propose_sheet_updates(
                uid=args[0],
                client_id=args[1],
                email=args[2],
                sheet_id=args[3],
                header=args[4],
                rownum=args[5],
                rowvals=args[6],
                thread_id=args[7],
                pdf_manifest=kwargs["pdf_manifest"],
                url_texts=kwargs["url_texts"],
                contact_name=kwargs["contact_name"],
                conversation=[{
                    "direction": "inbound",
                    "from": "broker@example.test",
                    "content": "",
                }],
                column_config=kwargs["column_config"],
                extraction_fields=kwargs["extraction_fields"],
                dry_run=True,
            )

        with mock.patch.object(
            ai_processing,
            "client",
            fake_client,
        ), mock.patch.object(
            ai_processing,
            "track_openai_usage_safely",
        ):
            run = self._run_process(
                attachments=[self._valid_attachment()],
                body="",
                proposal_side_effect=real_vision_proposal,
            )

        self.assertIsNone(run["error"])
        run["propose"].assert_called_once()
        manifests = run["propose"].call_args.kwargs["pdf_manifest"]
        self.assertEqual(1, len(manifests))
        self.assertEqual("native_image_normalized", manifests[0]["method"])
        self.assertEqual("target", manifests[0]["property_binding"])
        self.assertEqual(1, fake_client.responses.create.call_count)
        request_content = (
            fake_client.responses.create.call_args.kwargs["input"][0]["content"]
        )
        self.assertEqual(
            1,
            sum(item.get("type") == "input_image" for item in request_content),
        )
        self.assertFalse(any(
            item.get("type") == "input_file" for item in request_content
        ))
        fake_client.files.create.assert_not_called()
        applied_proposal = run["apply"].call_args.args[-1]
        self.assertEqual("18500", applied_proposal["updates"][0]["value"])

    def test_malformed_image_only_is_retryable_unprocessed_and_sends_nothing(self):
        raw_filename = f"{self.MATCHING_FILENAME} PRIVATE_RAW_NAME.png"
        run = self._run_process(
            attachments=[_attachment(
                raw_filename,
                "image/png",
                content_bytes="not strict base64!!",
            )],
            body="",
        )

        self.assertIsInstance(run["error"], processing.RetryableProcessingError)
        self.assertFalse(processing._should_mark_processed_after_error(run["error"]))
        run["propose"].assert_not_called()
        run["host"].assert_not_called()
        run["image_writer"].assert_not_called()
        run["send"].assert_not_called()
        self.assertNotIn(raw_filename, str(run["error"]))

    def test_independent_text_plus_bad_image_commits_text_and_sanitized_warning_only(self):
        private_name = (
            f"{self.MATCHING_FILENAME} PRIVATE_NATIVE_FILENAME_SENTINEL.png"
        )
        proposal = {
            "updates": [{
                "column": "Total SF",
                "value": "18500",
                "confidence": 0.99,
            }],
            "events": [],
        }
        run = self._run_process(
            attachments=[_attachment(
                private_name,
                "image/png",
                content_bytes="invalid!",
            )],
            body="The target space contains 18,500 square feet.",
            proposal=proposal,
        )

        self.assertIsNone(run["error"])
        run["apply"].assert_called_once()
        run["warning"].assert_called_once()
        failures = run["warning"].call_args.args[4]
        self.assertEqual(1, len(failures))
        serialized = repr(failures[0])
        self.assertIn("image_attachment_invalid_base64", serialized)
        self.assertNotIn(private_name, serialized)
        self.assertNotIn("contentBytes", serialized)
        self.assertNotIn("drive_link", serialized)
        self.assertEqual([], run["propose"].call_args.kwargs["pdf_manifest"])

        warning_fs = mock.MagicMock()
        with mock.patch.object(processing, "_fs", warning_fs):
            self.assertTrue(processing._record_asset_extraction_warning(
                "native-image-user",
                "native-image-client",
                "native-image-processing-thread",
                "native-image-message",
                failures,
            ))
        warning_set = (
            warning_fs.collection.return_value
            .document.return_value
            .collection.return_value
            .document.return_value
            .set
        )
        warning_payload = warning_set.call_args.args[0]
        self.assertEqual("degraded_text_processed", warning_payload["status"])
        self.assertEqual([{
            "name": GENERIC_IMAGE_NAME,
            "sourceType": "native_image",
            "method": "native_image_quarantined",
            "failureCode": "image_attachment_invalid_base64",
        }], warning_payload["assets"])
        self.assertNotIn(private_name, repr(warning_payload))
        run["host"].assert_not_called()
        run["image_writer"].assert_not_called()
        run["send"].assert_not_called()

    def test_hosting_waits_for_full_validation_and_safe_model_classification(self):
        order = []

        def proposal(*_args, **_kwargs):
            order.append("model")
            return {
                "updates": [],
                "events": [],
                "response_email": None,
                "notes": "",
                "skip_response": True,
            }

        run = self._run_process(
            attachments=[self._valid_attachment()],
            body="",
            proposal_side_effect=proposal,
            boundary_order=order,
        )

        self.assertIsNone(run["error"])
        self.assertEqual(["validate", "model", "host"], order)
        run["host"].assert_called_once()

    def test_hosts_only_first_eligible_image_when_property_image_blank(self):
        attachments = [
            self._valid_attachment("front", (11, 7)),
            self._valid_attachment("rear", (8, 6)),
        ]
        expected = file_handling.validate_and_normalize_native_image_attachments(
            attachments,
            target_property_hint=self.TARGET,
        )["assets"][0]

        run = self._run_process(attachments=attachments, body="")

        self.assertIsNone(run["error"])
        vision_manifests = run["propose"].call_args.kwargs["pdf_manifest"]
        self.assertEqual(2, len(vision_manifests))
        self.assertEqual([1, 1], [
            len(entry["images"])
            for entry in vision_manifests
        ])
        self.assertEqual([1, 1], [
            len(entry["image_meta"])
            for entry in vision_manifests
        ])
        run["host"].assert_called_once()
        upload_name, upload_bytes = run["host"].call_args.args[:2]
        self.assertEqual(expected["data"], upload_bytes)
        self.assertRegex(
            upload_name,
            rf"^broker-property-image-{expected['normalized_sha256'][:16]}\.png$",
        )
        run["image_writer"].assert_called_once()

    def test_existing_property_image_skips_hosting(self):
        run = self._run_process(
            attachments=[self._valid_attachment()],
            body="",
            property_image="https://images.example.test/existing.png",
        )

        self.assertIsNone(run["error"])
        manifests = run["propose"].call_args.kwargs["pdf_manifest"]
        self.assertEqual(1, len(manifests))
        self.assertEqual("native_image_normalized", manifests[0]["method"])
        run["host"].assert_not_called()
        run["image_writer"].assert_not_called()

        absent = self._run_process(
            attachments=[self._valid_attachment()],
            body="",
            include_property_image_column=False,
        )
        self.assertIsNone(absent["error"])
        self.assertEqual(
            "native_image_normalized",
            absent["propose"].call_args.kwargs["pdf_manifest"][0]["method"],
        )
        absent["host"].assert_not_called()
        absent["image_writer"].assert_not_called()

    def test_model_multi_property_or_image_failure_prevents_host_and_row_mutation(self):
        with self.subTest(reason="model_multi_property"):
            run = self._run_process(
                attachments=[self._valid_attachment()],
                body="",
                proposal={
                    "updates": [],
                    "events": [{
                        "type": "needs_user_input",
                        "reason": "multi_property_attachment",
                        "question": "Which property?",
                    }],
                },
            )
            self.assertIsNone(run["error"])
            run["host"].assert_not_called()
            run["apply"].assert_not_called()
            run["image_writer"].assert_not_called()
            run["send"].assert_not_called()

        with self.subTest(reason="model_none"):
            run = self._run_process(
                attachments=[self._valid_attachment()],
                body="",
                proposal_side_effect=lambda *_args, **_kwargs: None,
            )
            self.assertIsInstance(
                run["error"],
                processing.RetryableProcessingError,
            )
            run["host"].assert_not_called()
            run["apply"].assert_not_called()
            run["image_writer"].assert_not_called()
            run["send"].assert_not_called()

        with self.subTest(reason="image_failure"):
            run = self._run_process(
                attachments=[_attachment(
                    f"{self.MATCHING_FILENAME} corrupt.png",
                    "image/png",
                    content_bytes="corrupt!",
                )],
                body="",
                proposal={
                    "updates": [{"column": "Total SF", "value": "99999"}],
                    "events": [],
                },
            )
            self.assertIsInstance(
                run["error"],
                processing.RetryableProcessingError,
            )
            run["propose"].assert_not_called()
            run["apply"].assert_not_called()
            run["host"].assert_not_called()
            run["image_writer"].assert_not_called()
            run["send"].assert_not_called()

    def test_native_image_result_has_property_image_url_and_never_drive_link(self):
        private_drive_link = "PRIVATE_UPLOADER_DRIVE_LINK_SENTINEL"
        run = self._run_process(
            attachments=[self._valid_attachment()],
            body="",
            host_result={
                "url": "https://drive.google.com/uc?export=view&id=safe-native",
                "driveLink": private_drive_link,
                "contentType": "image/png",
                "byteCount": 321,
                "sha256": "uploader-sha",
            },
        )

        self.assertIsNone(run["error"])
        run["image_store"].assert_called_once()
        candidate = run["image_store"].call_args.args[8]
        self.assertEqual(
            "https://drive.google.com/uc?export=view&id=safe-native",
            candidate["url"],
        )
        self.assertNotIn(private_drive_link, repr(candidate))
        self.assertNotIn("sourceDriveLink", candidate)

        def recursive_keys(value):
            if isinstance(value, dict):
                return set(value) | {
                    nested_key
                    for nested in value.values()
                    for nested_key in recursive_keys(nested)
                }
            if isinstance(value, list):
                return {
                    nested_key
                    for nested in value
                    for nested_key in recursive_keys(nested)
                }
            return set()

        self.assertTrue(
            {"drive_link", "driveLink", "sourceDriveLink"}.isdisjoint(
                recursive_keys(candidate)
            )
        )

        private_host_exception = "PRIVATE_NATIVE_HOST_EXCEPTION_SENTINEL"
        native_batch = (
            file_handling.validate_and_normalize_native_image_attachments(
                [self._valid_attachment()],
                target_property_hint=self.TARGET,
            )
        )
        native_manifest = file_handling.build_native_image_manifest_entry(
            native_batch
        )
        native_host_output = io.StringIO()
        with mock.patch.object(
            file_handling,
            "_helper_google_creds",
            side_effect=RuntimeError(private_host_exception),
        ), contextlib.redirect_stdout(native_host_output):
            self.assertIsNone(
                file_handling.host_first_native_image_manifest_asset(
                    native_manifest
                )
            )
        self.assertNotIn(private_host_exception, native_host_output.getvalue())
        self.assertIn("native_image_host_failed", native_host_output.getvalue())

        private_folder_exception = "PRIVATE_NATIVE_FOLDER_EXCEPTION_SENTINEL"
        fake_drive = mock.MagicMock()
        fake_drive.files.return_value.create.return_value.execute.return_value = {
            "id": "native-folder-fallback-image",
            "webViewLink": (
                "https://drive.google.com/file/d/"
                "native-folder-fallback-image/view"
            ),
        }
        nested_folder_output = io.StringIO()
        with mock.patch.object(
            file_handling,
            "_helper_google_creds",
            side_effect=[object(), RuntimeError(private_folder_exception)],
        ), mock.patch.object(
            file_handling,
            "build",
            return_value=fake_drive,
        ), contextlib.redirect_stdout(nested_folder_output):
            nested_result = (
                file_handling.host_first_native_image_manifest_asset(
                    native_manifest
                )
            )
        self.assertIsNotNone(nested_result)
        self.assertNotIn(
            private_folder_exception,
            nested_folder_output.getvalue(),
        )
        self.assertIn(
            "native_image_host_failed",
            nested_folder_output.getvalue(),
        )

        legacy_host_output = io.StringIO()
        legacy_exception_detail = "LEGACY_PROPERTY_PREVIEW_FAILURE_DETAIL"
        with mock.patch.object(
            file_handling,
            "_helper_google_creds",
            side_effect=RuntimeError(legacy_exception_detail),
        ), contextlib.redirect_stdout(legacy_host_output):
            self.assertIsNone(file_handling.upload_property_image_to_drive(
                "legacy-preview.png",
                b"legacy-preview",
            ))
        self.assertIn(legacy_exception_detail, legacy_host_output.getvalue())

    def test_standard_address_city_row_quarantines_without_filename_or_body_rescue(self):
        run = self._run_process(
            attachments=[self._valid_attachment()],
            body=(
                "These photos are for 123 North Sample Road, Example City, "
                "AZ 85001."
            ),
            property_address="123 North Sample Road",
            city="Example City, AZ 85001",
        )

        self.assertIsInstance(run["error"], processing.RetryableProcessingError)
        run["propose"].assert_called_once()
        self.assertEqual(
            [],
            run["propose"].call_args.kwargs["pdf_manifest"],
        )
        run["host"].assert_not_called()
        run["image_writer"].assert_not_called()

    def test_current_message_graph_snapshot_preserves_pdf_native_order_and_later_page_count(self):
        next_link = "https://graph.microsoft.com/v1.0/next-page-token"
        first_native = self._valid_attachment("front", (8, 6))
        middle_pdf = _attachment(
            "middle.pdf",
            "application/pdf",
            b"middle-pdf",
        )
        later_native = self._valid_attachment("rear", (9, 6))
        first_page = self._graph_page([first_native, middle_pdf], next_link)
        second_page = self._graph_page([later_native])

        with mock.patch.object(
            file_handling.requests,
            "get",
            side_effect=[first_page, second_page],
        ) as graph_get, mock.patch.object(
            file_handling,
            "process_pdf_for_ai",
            side_effect=lambda content, name: {
                "text": content.decode("ascii"),
                "images": [],
                "method": "local_extraction",
                "file_id": None,
                "id": None,
            },
        ) as process_pdf, mock.patch.object(
            file_handling,
            "upload_pdf_to_drive",
            side_effect=lambda name, _content: f"https://drive/{name}",
        ), mock.patch.object(
            file_handling,
            "_attach_pdf_property_preview",
        ):
            try:
                manifest = file_handling.fetch_and_process_pdfs(
                    {"Authorization": "Bearer fake"},
                    "graph-message",
                    target_property_hint=self.TARGET,
                )
            except TypeError as exc:
                self.fail(
                    "current-message attachment assembly does not yet accept "
                    f"a target property hint: {exc}"
                )

        self.assertEqual(2, graph_get.call_count)
        self.assertEqual(next_link, graph_get.call_args_list[1].args[0])
        self.assertEqual(1, process_pdf.call_count)
        self.assertEqual(
            [GENERIC_IMAGE_NAME, "middle.pdf", GENERIC_IMAGE_NAME],
            [entry["name"] for entry in manifest],
        )
        self.assertEqual(
            ["middle-pdf"],
            [
                entry["text"]
                for entry in manifest
                if entry.get("method") == "local_extraction"
            ],
        )
        native_manifests = [
            entry
            for entry in manifest
            if entry.get("method") == "native_image_normalized"
        ]
        self.assertEqual([1, 1], [
            len(entry["images"])
            for entry in native_manifests
        ])
        model_transport = ai_processing._prepare_ai_attachment_manifest(manifest)
        self.assertIsNotNone(model_transport)
        self.assertEqual(
            ["native", "legacy", "native"],
            [
                "native" if entry.native is not None else "legacy"
                for entry in model_transport
            ],
        )

        failing_later_page = mock.MagicMock()
        failing_later_page.raise_for_status.side_effect = (
            file_handling.requests.exceptions.HTTPError("Graph page 2 failed")
        )
        with mock.patch.object(
            file_handling.requests,
            "get",
            side_effect=[self._graph_page([], next_link), failing_later_page],
        ):
            with self.assertRaises(file_handling.requests.exceptions.RequestException):
                file_handling.fetch_and_process_pdfs(
                    {"Authorization": "Bearer fake"},
                    "graph-message-page-failure",
                    target_property_hint=self.TARGET,
                )

        private_page_sentinel = "PRIVATE_GRAPH_PAGE_SHAPE_SENTINEL"
        malformed_pages = (
            [private_page_sentinel],
            {private_page_sentinel: "missing-value"},
            {"value": {private_page_sentinel: "not-a-list"}},
            {
                "value": [],
                "@odata.nextLink": {private_page_sentinel: "not-a-url"},
            },
        )
        for malformed_page in malformed_pages:
            with self.subTest(malformed_page=type(malformed_page).__name__):
                malformed_response = mock.MagicMock()
                malformed_response.status_code = 200
                malformed_response.json.return_value = malformed_page
                captured_output = io.StringIO()
                with mock.patch.object(
                    file_handling.requests,
                    "get",
                    return_value=malformed_response,
                ), mock.patch.object(
                    file_handling,
                    "process_pdf_for_ai",
                ) as malformed_pdf_processor, contextlib.redirect_stdout(
                    captured_output
                ):
                    with self.assertRaises(
                        file_handling.requests.exceptions.RequestException
                    ) as raised:
                        file_handling.fetch_and_process_pdfs(
                            {"Authorization": "Bearer fake"},
                            "graph-message-malformed-page",
                            target_property_hint=self.TARGET,
                        )
                malformed_pdf_processor.assert_not_called()
                self.assertNotIn(private_page_sentinel, str(raised.exception))
                self.assertNotIn(private_page_sentinel, captured_output.getvalue())

        four_native = [
            self._valid_attachment(f"angle-{index}", (8 + index, 6))
            for index in range(4)
        ]
        with mock.patch.object(
            file_handling.requests,
            "get",
            side_effect=[
                self._graph_page(four_native[:2], next_link),
                self._graph_page(four_native[2:]),
            ],
        ), mock.patch.object(
            file_handling,
            "process_pdf_for_ai",
        ) as capped_pdf_processor:
            over_count = file_handling.fetch_and_process_pdfs(
                {"Authorization": "Bearer fake"},
                "graph-message-over-count",
                target_property_hint=self.TARGET,
            )
        capped_pdf_processor.assert_not_called()
        self.assertEqual(1, len(over_count))
        self.assertEqual(
            "image_attachment_too_many",
            over_count[0]["failure_code"],
        )

        native_claim_with_pdf_type = _attachment(
            f"{self.MATCHING_FILENAME} mismatched.jpg",
            "application/pdf",
            b"%PDF-1.7 not an image",
        )
        with mock.patch.object(
            file_handling.requests,
            "get",
            return_value=self._graph_page([native_claim_with_pdf_type]),
        ), mock.patch.object(
            file_handling,
            "process_pdf_for_ai",
        ) as mismatched_pdf_processor:
            mismatch = file_handling.fetch_and_process_pdfs(
                {"Authorization": "Bearer fake"},
                "graph-message-type-mismatch",
                target_property_hint=self.TARGET,
            )
        mismatched_pdf_processor.assert_not_called()
        self.assertEqual(
            "image_attachment_type_mismatch",
            mismatch[0]["failure_code"],
        )

    def test_graph_snapshot_rejects_oversized_page_before_materializing_copy(self):
        oversized_values = [
            {"id": "oversized-attachment"}
        ] * (file_handling.GRAPH_ATTACHMENT_SNAPSHOT_MAX_ITEMS * 1000)
        self.assertIs(type(oversized_values), list)

        response = mock.MagicMock()
        response.status_code = 200
        response.json.return_value = {"value": oversized_values}
        materialized_lengths = []
        snapshot_code = file_handling.fetch_message_attachment_snapshot.__code__

        def record_snapshot_allocation(frame, event, _arg):
            if frame.f_code is snapshot_code and event == "line":
                attachments = frame.f_locals.get("attachments")
                if type(attachments) is list:
                    materialized_lengths.append(len(attachments))
            return record_snapshot_allocation

        previous_trace = sys.gettrace()
        with mock.patch.object(
            file_handling.requests,
            "get",
            return_value=response,
        ):
            try:
                sys.settrace(record_snapshot_allocation)
                with self.assertRaises(
                    file_handling.requests.exceptions.RequestException
                ) as raised:
                    file_handling.fetch_message_attachment_snapshot(
                        {"Authorization": "Bearer fake"},
                        "graph-message-oversized-page",
                    )
            finally:
                sys.settrace(previous_trace)

        self.assertEqual(
            "Graph attachment snapshot exceeded the item limit",
            str(raised.exception),
        )
        self.assertTrue(materialized_lengths)
        self.assertEqual(0, max(materialized_lengths))

    def test_prevalidated_native_target_remains_current_when_body_emits_new_property(self):
        attachment = self._valid_attachment()
        batch = file_handling.validate_and_normalize_native_image_attachments(
            [attachment],
            target_property_hint=self.TARGET,
        )
        native_manifest = file_handling.build_native_image_manifest_entry(batch)
        alternate_pdf = {
            "name": "999 Alternate Road Flyer.pdf",
            "text": "999 Alternate Road, Other City, AZ 85002",
            "images": [],
            "method": "local_extraction",
        }
        event = {
            "type": "new_property",
            "address": "999 Alternate Road",
            "city": "Other City",
        }

        current, by_event = processing._partition_property_attachments(
            [native_manifest, alternate_pdf],
            current_anchor=self.TARGET,
            events=[event],
        )

        self.assertEqual([native_manifest], current)
        self.assertEqual([[alternate_pdf]], by_event)


if __name__ == "__main__":
    unittest.main()
