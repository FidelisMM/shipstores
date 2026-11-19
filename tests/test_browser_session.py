import unittest
from unittest.mock import patch

from shipstores import browser


class LoggedOutDetectionTests(unittest.TestCase):
    def test_expired_apple_session_on_the_console_itself(self) -> None:
        # Apple does not redirect to idmsa when the session expires: it stays on /login.
        out = "URL=https://appstoreconnect.apple.com/login?targetUrl=%2Fapps&authResult=FAILED"
        self.assertEqual(browser._logged_out_console(out), "apple")

    def test_internal_api_401_counts_as_logged_out(self) -> None:
        self.assertEqual(browser._logged_out_console(f"__SP__ERROR {browser.LOGGED_OUT_MARK} apple"), "apple")
        self.assertIsNone(browser._logged_out_console('__SP__[{"text": "ok"}]'))


class UserBrowserFallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        patches = [
            patch.object(browser, "ensure_headless", return_value="http://127.0.0.1:9333"),
            patch.object(browser.shutil, "which", return_value="/bin/browser-harness"),
            patch.object(browser, "relogin", return_value="relogin called"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_dedicated_logged_out_uses_the_user_browser(self) -> None:
        outputs = {"http://127.0.0.1:9333": f"{browser.LOGGED_OUT_MARK} apple", None: "__SP__[]"}
        with patch.object(browser, "_harness", side_effect=lambda b, s, t, cdp: outputs[cdp]):
            self.assertEqual(browser.run_harness("script"), "__SP__[]")

    def test_no_session_anywhere_asks_for_login(self) -> None:
        with patch.object(browser, "_harness", return_value=f"{browser.LOGGED_OUT_MARK} apple"):
            with self.assertRaisesRegex(browser.BrowserError, "relogin called"):
                browser.run_harness("script")


if __name__ == "__main__":
    unittest.main()
