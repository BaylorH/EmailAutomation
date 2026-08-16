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


if __name__ == "__main__":
    unittest.main()
