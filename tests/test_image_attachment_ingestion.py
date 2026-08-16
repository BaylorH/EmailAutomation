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


FILE_ATTACHMENT_TYPE = "#microsoft.graph.fileAttachment"
GENERIC_IMAGE_NAME = "Broker property image"


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
        "isInline": is_inline,
    }
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
                        return_value=pillow_format_override,
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


if __name__ == "__main__":
    unittest.main()
