import subprocess
import tempfile
import unittest
from pathlib import Path
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


class LoginUrlOnlyTests(unittest.TestCase):
    def setUp(self) -> None:
        p = patch.object(browser, "relogin", return_value="relogin called")
        p.start()
        self.addCleanup(p.stop)

    def test_login_link_in_the_middle_of_text_does_not_count(self) -> None:
        out = '["overview"] see https://accounts.google.com/RotateCookiesPage in the iframe'
        self.assertIsNone(browser._logged_out_console(out))
        browser._falhar_se_deslogou(out)

    def test_page_url_on_the_login_screen_counts(self) -> None:
        with self.assertRaisesRegex(browser.BrowserError, "play"):
            browser._falhar_se_deslogou("https://accounts.google.com/v3/signin/identifier?x=1\nSign in")


class HeadlessReturnTests(unittest.TestCase):
    """The dedicated Chrome goes back to headless by itself once a login is done."""

    def setUp(self) -> None:
        for p in (
            patch.object(type(browser.PROFILE_DIR), "exists", lambda self: True),
            patch.object(browser.time, "sleep", lambda _s: None),
        ):
            p.start()
            self.addCleanup(p.stop)

    def test_window_of_a_finished_login_goes_back_to_headless(self) -> None:
        calls = []
        alive = iter([True, False, True])
        with patch.object(browser, "_cdp_alive", lambda: next(alive, True)), \
                patch.object(browser, "_running_headless", return_value=False), \
                patch.object(browser, "_login_in_progress", return_value=False), \
                patch.object(browser, "return_to_headless", lambda: calls.append("closed") or True), \
                patch.object(browser, "_spawn", lambda headless, urls=(): calls.append(f"headless={headless}")), \
                patch.object(browser, "restore_cookies", lambda: None):
            self.assertEqual(browser.ensure_headless(), browser.CDP_URL)
        self.assertEqual(calls, ["closed", "headless=True"])

    def test_keeps_the_window_while_someone_logs_in(self) -> None:
        with patch.object(browser, "_cdp_alive", return_value=True), \
                patch.object(browser, "_running_headless", return_value=False), \
                patch.object(browser, "_login_in_progress", return_value=True), \
                patch.object(browser, "return_to_headless", side_effect=AssertionError("closed the login window")):
            self.assertEqual(browser.ensure_headless(), browser.CDP_URL)

    def test_login_in_progress_includes_apples_own_login_page(self) -> None:
        for url, expected in (
            ("https://appstoreconnect.apple.com/login?targetUrl=%2Fapps", True),
            ("https://idmsa.apple.com/appleauth/auth/signin", True),
            ("https://accounts.google.com/v3/signin", True),
            ("https://appstoreconnect.apple.com/apps", False),
        ):
            with self.subTest(url=url):
                tabs = [{"type": "page", "url": url}]
                with patch.object(browser.httpx, "get") as get:
                    get.return_value.json.return_value = tabs
                    self.assertIs(browser._login_in_progress(), expected)

    def test_new_chrome_gets_the_saved_cookies(self) -> None:
        calls = []
        alive = iter([False, True])
        with patch.object(browser, "_cdp_alive", lambda: next(alive, True)), \
                patch.object(browser, "_spawn", lambda headless, urls=(): calls.append("started")), \
                patch.object(browser, "restore_cookies", lambda: calls.append("restored")):
            self.assertEqual(browser.ensure_headless(), browser.CDP_URL)
        self.assertEqual(calls, ["started", "restored"])

    def test_closing_the_window_saves_cookies_first(self) -> None:
        calls = []
        with patch.object(browser, "_cdp_alive", lambda: "closed" not in calls), \
                patch.object(browser, "_running_headless", return_value=False), \
                patch.object(browser, "save_cookies", lambda: calls.append("saved")), \
                patch.object(browser, "_close_chrome", lambda: calls.append("closed")):
            self.assertTrue(browser.return_to_headless())
        self.assertEqual(calls, ["saved", "closed"])

    def test_relogin_with_a_window_open_only_adds_tabs(self) -> None:
        with patch.object(browser, "_cdp_alive", return_value=True), \
                patch.object(browser, "_running_headless", return_value=False), \
                patch.object(browser.httpx, "put") as put, \
                patch.object(browser.subprocess, "run", side_effect=AssertionError("killed Chrome")):
            message = browser.relogin(["apple"])
        put.assert_called_once()
        self.assertIn("appstoreconnect.apple.com", put.call_args.args[0])
        self.assertIn("Login tabs opened", message)


class SavedCookieTests(unittest.TestCase):
    """Apple's console login lives in a session cookie that Chrome drops on close."""

    def test_session_cookie_becomes_a_param_without_expiry(self) -> None:
        cookie = {"name": "session", "value": "x", "domain": ".apple.com", "path": "/", "expires": -1,
                  "size": 9, "httpOnly": True, "secure": True, "session": True, "sameSite": "Lax",
                  "priority": "Medium"}
        self.assertEqual(browser._cookie_param(cookie), {
            "name": "session", "value": "x", "domain": ".apple.com", "path": "/",
            "httpOnly": True, "secure": True, "sameSite": "Lax",
        })

    def test_persistent_cookie_keeps_its_expiry(self) -> None:
        param = browser._cookie_param({"name": "p", "value": "y", "domain": "appstoreconnect.apple.com",
                                       "path": "/", "expires": 1900000000.5, "session": False})
        self.assertEqual(param["expires"], 1900000000.5)

    def test_only_console_cookies_are_kept(self) -> None:
        cookies = [{"name": "a", "domain": ".apple.com"}, {"name": "b", "domain": "accounts.google.com"},
                   {"name": "c", "domain": ".example.com"}]
        self.assertEqual([c["name"] for c in browser._console_cookies(cookies)], ["a", "b"])

    def test_saved_file_is_private_and_round_trips(self) -> None:
        cookies = [{"name": "a", "value": "1", "domain": ".apple.com", "path": "/", "session": True}]
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "console-session.json"
            with patch.object(browser, "COOKIES_FILE", target), \
                    patch.object(browser, "_browser_cdp", return_value={"cookies": cookies}):
                browser.save_cookies()
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            with patch.object(browser, "COOKIES_FILE", target), \
                    patch.object(browser, "_browser_cdp") as cdp:
                browser.restore_cookies()
            cdp.assert_called_once_with("Storage.setCookies", {"cookies": [
                {"name": "a", "value": "1", "domain": ".apple.com", "path": "/"}]})


class UserBrowserFallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        patches = [
            patch.object(browser, "ensure_headless", return_value="http://127.0.0.1:9333"),
            patch.object(browser.shutil, "which", return_value="/bin/browser-harness"),
            patch.object(browser, "relogin", return_value="relogin called"),
            patch.object(browser, "save_cookies"),
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
