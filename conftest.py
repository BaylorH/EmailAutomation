"""Hermetic pytest configuration for repository-wide collection."""

from contextlib import ExitStack
from unittest.mock import MagicMock, patch


_PROVIDER_PATCH_STACK_ATTRIBUTE = "_credential_free_collection_provider_patches"


def pytest_configure(config) -> None:
    """Prevent provider initialization only while pytest inventories tests."""
    if not getattr(config.option, "collectonly", False):
        return
    if getattr(config, _PROVIDER_PATCH_STACK_ATTRIBUTE, None) is not None:
        return

    import firebase_admin
    from google.cloud import firestore
    import msal

    collection_client = MagicMock(name="collection_firestore_client")
    collection_firebase_app = MagicMock(name="collection_firebase_app")
    collection_msal_app = MagicMock(name="collection_msal_app")
    provider_patches = ExitStack()
    try:
        provider_patches.enter_context(
            patch.object(firestore, "Client", return_value=collection_client)
        )
        provider_patches.enter_context(
            patch.object(
                firebase_admin,
                "initialize_app",
                return_value=collection_firebase_app,
            )
        )
        provider_patches.enter_context(
            patch.object(
                msal,
                "PublicClientApplication",
                return_value=collection_msal_app,
            )
        )
        setattr(config, _PROVIDER_PATCH_STACK_ATTRIBUTE, provider_patches)
    except BaseException:
        provider_patches.close()
        raise


def pytest_unconfigure(config) -> None:
    """Restore every provider constructor after collection finishes."""
    provider_patches = getattr(config, _PROVIDER_PATCH_STACK_ATTRIBUTE, None)
    if provider_patches is None:
        return

    try:
        provider_patches.close()
    finally:
        delattr(config, _PROVIDER_PATCH_STACK_ATTRIBUTE)
