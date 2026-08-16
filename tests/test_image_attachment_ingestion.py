import base64
import hashlib
import io
import os
import unittest
from unittest import mock

from PIL import Image, PngImagePlugin


os.environ.setdefault("FIRESTORE_EMULATOR_HOST", "127.0.0.1:1")
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "email-automation-cache")
os.environ.setdefault("E2E_TEST_MODE", "true")

from email_automation import file_handling
from email_automation import ai_processing
from email_automation import property_images


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
            ("bytes_native_marker", b"native_image", "openai_upload"),
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

        for label, manifest, sentinel in malformed_source_type_manifests:
            with self.subTest(case=label):
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
                self.assertEqual(
                    (None, 0, 0, 0, 0, False),
                    (
                        run["proposal"],
                        run["client"].responses.create.call_count,
                        run["client"].files.create.call_count,
                        run["usage_call"].call_count,
                        persist_call.call_count,
                        sentinel in observable,
                    ),
                )

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
                legacy_manifests.append((
                    f"{source_type}:{method}",
                    {
                        "name": f"{source_type}.pdf",
                        "text": f"{self.TARGET}\nLegacy linked PDF text.",
                        "images": [legacy_page] if has_images else [],
                        "method": method,
                        "file_id": file_id,
                        "id": file_id,
                        "source_type": source_type,
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

        for source_type, method in (
            ("direct_image", "direct_image_link"),
        ):
            legacy_manifests.append((
                f"{source_type}:{method}",
                {
                    "name": "broker linked asset",
                    "text": "",
                    "images": [],
                    "method": method,
                    "file_id": None,
                    "id": None,
                    "source_type": source_type,
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
            rejected = self._run_proposal(manifests, dry_run=False)
            self.assertIsNone(rejected["proposal"])
            self.assertEqual(
                0,
                rejected["client"].responses.create.call_count,
            )
            self.assertEqual(0, rejected["client"].files.create.call_count)
            self.assertEqual(0, rejected["usage_call"].call_count)
            rejected_persist = (
                rejected["firestore"].collection.return_value
                .document.return_value
                .collection.return_value
                .document.return_value
                .set
            )
            self.assertEqual(0, rejected_persist.call_count)

        with self.subTest(resource_case="request_native_asset_count_four"):
            assert_request_rejected_before_effects([
                self._single_manifest(f"request-count-{label}")
                for label in ("alpha", "beta", "gamma", "delta")
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
        with self.subTest(resource_case="request_source_one_over"):
            with mock.patch.object(
                file_handling,
                "NATIVE_IMAGE_MAX_BATCH_SOURCE_BYTES",
                request_source_cap,
            ):
                assert_request_rejected_before_effects(
                    request_source_manifests
                )

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
        with self.subTest(resource_case="request_normalized_one_over"):
            with mock.patch.object(
                file_handling,
                "NATIVE_IMAGE_MAX_BATCH_SOURCE_BYTES",
                request_normalized_total - 1,
            ):
                assert_request_rejected_before_effects(
                    request_normalized_manifests
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


if __name__ == "__main__":
    unittest.main()
