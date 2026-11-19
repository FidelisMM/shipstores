import subprocess
import unittest
from unittest.mock import patch

from shipstores import browser, cdp_direct


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

    def _dedicated(self, output: str):
        return patch.object(cdp_direct, "run", return_value=output)

    def test_dedicated_runs_over_cdp_without_browser_harness(self) -> None:
        with self._dedicated("__SP__[]"), patch.object(browser, "_harness") as harness:
            self.assertEqual(browser.run_harness("script"), "__SP__[]")
        harness.assert_not_called()

    def test_dedicated_logged_out_uses_the_user_browser(self) -> None:
        with self._dedicated(f"{browser.LOGGED_OUT_MARK} apple"), \
                patch.object(browser, "_harness", return_value="__SP__[]"):
            self.assertEqual(browser.run_harness("script"), "__SP__[]")

    def test_no_session_anywhere_asks_for_login(self) -> None:
        with self._dedicated(f"{browser.LOGGED_OUT_MARK} apple"), \
                patch.object(browser, "_harness", return_value=f"{browser.LOGGED_OUT_MARK} apple"):
            with self.assertRaisesRegex(browser.BrowserError, "relogin called"):
                browser.run_harness("script")

    def test_failed_script_is_reported_as_script_failed(self) -> None:
        with patch.object(cdp_direct, "run", side_effect=cdp_direct.CdpError("ERROR: no button")):
            with self.assertRaisesRegex(browser.ScriptFailed, "ERROR: no button"):
                browser.run_harness("script")


class NoProfileTests(unittest.TestCase):
    def test_without_profile_uses_browser_harness_without_cdp_url(self) -> None:
        completed = subprocess.CompletedProcess([], 0, stdout="ok", stderr="")
        with patch.object(browser, "ensure_headless", return_value=None), \
                patch.object(browser.shutil, "which", return_value="/bin/browser-harness"), \
                patch.dict(browser.os.environ, {"BU_CDP_URL": "http://elsewhere"}), \
                patch.object(browser.subprocess, "run", return_value=completed) as run:
            self.assertEqual(browser.run_harness("script"), "ok")
        self.assertNotIn("BU_CDP_URL", run.call_args.kwargs["env"])


if __name__ == "__main__":
    unittest.main()
