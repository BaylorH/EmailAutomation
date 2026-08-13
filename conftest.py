"""Hermetic pytest configuration for repository-wide collection."""

from unittest.mock import MagicMock, patch


_FIRESTORE_PATCH_ATTRIBUTE = "_credential_free_collection_firestore_patch"


def pytest_configure(config) -> None:
    """Prevent provider initialization only while pytest inventories tests."""
    if not getattr(config.option, "collectonly", False):
        return
    if getattr(config, _FIRESTORE_PATCH_ATTRIBUTE, None) is not None:
        return

    from google.cloud import firestore

    collection_client = MagicMock(name="collection_firestore_client")
    client_patch = patch.object(
        firestore,
        "Client",
        return_value=collection_client,
    )
    client_patch.start()
    setattr(config, _FIRESTORE_PATCH_ATTRIBUTE, client_patch)


def pytest_unconfigure(config) -> None:
    """Restore the real provider constructor after collection finishes."""
    client_patch = getattr(config, _FIRESTORE_PATCH_ATTRIBUTE, None)
    if client_patch is None:
        return

    client_patch.stop()
    delattr(config, _FIRESTORE_PATCH_ATTRIBUTE)
