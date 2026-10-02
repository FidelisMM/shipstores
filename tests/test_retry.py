import unittest
from unittest.mock import patch

import httpx

from shipstores import apple, play


class Flaky:
    """Fails `failures` times with `error`, then returns `value`."""

    def __init__(self, failures, error, value="ok"):
        self.failures = failures
        self.error = error
        self.value = value
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self.calls <= self.failures:
            raise self.error
        return self.value


def transport_error():
    return httpx.ConnectError("connection dropped")


def status_error(code):
    request = httpx.Request("PUT", "https://example.test/upload")
    response = httpx.Response(code, request=request)
    return httpx.HTTPStatusError("bad status", request=request, response=response)


class AppleWithRetryTests(unittest.TestCase):
    def test_succeeds_after_transient_failures(self):
        fn = Flaky(2, transport_error(), value="uploaded")
        with patch.object(apple.time, "sleep") as sleep:
            result = apple.with_retry(fn, "upload")
        self.assertEqual(result, "uploaded")
        self.assertEqual(fn.calls, 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [2, 4])

    def test_retries_http_status_errors(self):
        fn = Flaky(1, status_error(503), value="uploaded")
        with patch.object(apple.time, "sleep") as sleep:
            result = apple.with_retry(fn, "upload")
        self.assertEqual(result, "uploaded")
        self.assertEqual(fn.calls, 2)
        sleep.assert_called_once_with(2)

    def test_gives_up_with_apple_error_after_tentativas(self):
        original = transport_error()
        fn = Flaky(99, original)
        with patch.object(apple.time, "sleep") as sleep:
            with self.assertRaises(apple.AppleError) as ctx:
                apple.with_retry(fn, "upload", tentativas=3)
        self.assertEqual(fn.calls, 3)
        self.assertIn("upload", str(ctx.exception))
        self.assertIn("3 attempts", str(ctx.exception))
        self.assertIs(ctx.exception.__cause__, original)
        self.assertEqual(sleep.call_count, 2)

    def test_backoff_doubles_with_default_attempts(self):
        fn = Flaky(99, transport_error())
        with patch.object(apple.time, "sleep") as sleep:
            with self.assertRaises(apple.AppleError):
                apple.with_retry(fn, "upload")
        self.assertEqual(fn.calls, apple.RETRY_TENTATIVAS)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [2, 4, 8, 16])


class PlayWithRetryTests(unittest.TestCase):
    def test_succeeds_after_transient_failures(self):
        fn = Flaky(2, transport_error(), value="uploaded")
        with patch.object(play.time, "sleep") as sleep:
            result = play.with_retry(fn, "upload")
        self.assertEqual(result, "uploaded")
        self.assertEqual(fn.calls, 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [2, 4])

    def test_transport_error_is_reraised_after_tentativas(self):
        original = transport_error()
        fn = Flaky(99, original)
        with patch.object(play.time, "sleep") as sleep:
            with self.assertRaises(httpx.ConnectError) as ctx:
                play.with_retry(fn, "upload", tentativas=3)
        self.assertIs(ctx.exception, original)
        self.assertEqual(fn.calls, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_5xx_play_error_is_retried(self):
        fn = Flaky(2, play.PlayError("Play upload -> HTTP 503: unavailable"), value="uploaded")
        with patch.object(play.time, "sleep") as sleep:
            result = play.with_retry(fn, "upload")
        self.assertEqual(result, "uploaded")
        self.assertEqual(fn.calls, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_5xx_play_error_is_reraised_after_tentativas(self):
        error = play.PlayError("Play upload -> HTTP 500: boom")
        fn = Flaky(99, error)
        with patch.object(play.time, "sleep"):
            with self.assertRaises(play.PlayError) as ctx:
                play.with_retry(fn, "upload", tentativas=3)
        self.assertIs(ctx.exception, error)
        self.assertEqual(fn.calls, 3)

    def test_4xx_play_error_is_not_retried(self):
        error = play.PlayError("Play upload -> HTTP 404: not found")
        fn = Flaky(99, error)
        with patch.object(play.time, "sleep") as sleep:
            with self.assertRaises(play.PlayError) as ctx:
                play.with_retry(fn, "upload")
        self.assertIs(ctx.exception, error)
        self.assertEqual(fn.calls, 1)
        sleep.assert_not_called()

    def test_backoff_doubles_with_default_attempts(self):
        fn = Flaky(99, transport_error())
        with patch.object(play.time, "sleep") as sleep:
            with self.assertRaises(httpx.ConnectError):
                play.with_retry(fn, "upload")
        self.assertEqual(fn.calls, play.RETRY_TENTATIVAS)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [2, 4, 8, 16])


if __name__ == "__main__":
    unittest.main()
