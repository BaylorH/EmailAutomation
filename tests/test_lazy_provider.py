from concurrent.futures import ThreadPoolExecutor
import threading
import time
import unittest

from email_automation.lazy_provider import LazyProviderProxy


class LazyProviderProxyTests(unittest.TestCase):
    def test_concurrent_first_access_constructs_exactly_once(self):
        calls = 0
        calls_lock = threading.Lock()
        instance = object()

        def factory():
            nonlocal calls
            with calls_lock:
                calls += 1
            time.sleep(0.03)
            return instance

        proxy = LazyProviderProxy("test", factory)
        workers = 16
        barrier = threading.Barrier(workers + 1)

        def read():
            barrier.wait(timeout=5)
            return proxy.get()

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(read) for _ in range(workers)]
            barrier.wait(timeout=5)
            results = [future.result(timeout=5) for future in futures]

        self.assertEqual(1, calls)
        self.assertTrue(all(value is instance for value in results))

    def test_factory_failure_is_not_cached(self):
        attempts = 0
        instance = object()

        def factory():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("first failure")
            return instance

        proxy = LazyProviderProxy("retry", factory)
        with self.assertRaisesRegex(RuntimeError, "first failure"):
            proxy.get()
        self.assertIs(instance, proxy.get())
        self.assertEqual(2, attempts)

    def test_repr_and_initialized_do_not_construct(self):
        calls = []
        proxy = LazyProviderProxy("quiet", lambda: calls.append(1) or object())
        self.assertFalse(proxy.initialized)
        self.assertIn("quiet", repr(proxy))
        self.assertEqual([], calls)

    def test_attribute_access_delegates_and_constructs_once(self):
        calls = []

        class Provider:
            def collection(self, name):
                return ("collection", name)

        proxy = LazyProviderProxy(
            "delegate", lambda: calls.append("factory") or Provider()
        )
        self.assertTrue(proxy)
        self.assertEqual([], calls)
        self.assertEqual(("collection", "users"), proxy.collection("users"))
        self.assertEqual(["factory"], calls)
