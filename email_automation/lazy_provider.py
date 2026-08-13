from __future__ import annotations

from collections.abc import Callable
from threading import Lock
from typing import Generic, TypeVar, cast

T = TypeVar("T")
_UNSET = object()


class LazyProviderProxy(Generic[T]):
    def __init__(self, name: str, factory: Callable[[], T]) -> None:
        self._name = name
        self._factory = factory
        self._instance: T | object = _UNSET
        self._lock = Lock()

    @property
    def initialized(self) -> bool:
        return self._instance is not _UNSET

    def get(self) -> T:
        instance = self._instance
        if instance is _UNSET:
            with self._lock:
                instance = self._instance
                if instance is _UNSET:
                    instance = self._factory()
                    self._instance = instance
        return cast(T, instance)

    def __getattr__(self, attribute: str):
        if (
            attribute.startswith("__") and attribute.endswith("__")
        ) or attribute in {"_is_coroutine", "_is_coroutine_marker"}:
            raise AttributeError(attribute)
        return getattr(self.get(), attribute)

    def __repr__(self) -> str:
        state = "ready" if self.initialized else "uninitialized"
        return f"LazyProviderProxy(name={self._name!r}, state={state})"
