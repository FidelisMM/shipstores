"""cdp_direct: a missing answer becomes CdpError, a stuck navigation switches tabs,
and SystemExit from a script never reaches the server."""

import sys
import unittest
from unittest.mock import patch

from shipstores import cdp_direct


class _SilentWebSocket:
    def send(self, _msg: str) -> None:
        pass

    def recv(self, timeout: float) -> str:
        raise TimeoutError(f"timed out in {timeout:.1f}s")


class _FakeSession:
    """Simulates Chrome: the first `stuck` navigations never answer."""

    stuck = 1
    last: "_FakeSession | None" = None

    def __init__(self, _cdp_url: str) -> None:
        self.session_id: str | None = None
        self.tabs: list[str] = []
        self.closed: list[str] = []
        self.created = 0
        self.navigations = 0
        _FakeSession.last = self

    def send(self, method: str, session_id=None, timeout: float = 120, **params):
        if method == "Target.createTarget":
            self.created += 1
            return {"targetId": f"tab{self.created}"}
        if method == "Target.closeTarget":
            self.closed.append(params["targetId"])
            return {}
        if method == "Page.navigate":
            self.navigations += 1
            if self.navigations <= self.stuck:
                raise cdp_direct.CdpError("Page.navigate: no response within 45s")
            return {"frameId": "f"}
        return {}

    def pages(self) -> list[str]:
        return []

    def attach(self, target_id: str) -> None:
        self.session_id = target_id

    def evaluate(self, _expression: str):
        return None

    def close(self) -> None:
        pass


class SendTests(unittest.TestCase):
    def test_no_answer_becomes_cdp_error(self) -> None:
        session = cdp_direct._Session.__new__(cdp_direct._Session)
        session.ws = _SilentWebSocket()
        session.ids = iter(range(1, 10))
        session.session_id = "s1"
        with self.assertRaisesRegex(cdp_direct.CdpError, "no response"):
            session.send("Page.navigate", url="https://example.com", timeout=0.2)


class RunTests(unittest.TestCase):
    def setUp(self) -> None:
        _FakeSession.stuck = 0
        p = patch.object(cdp_direct, "_Session", _FakeSession)
        p.start()
        self.addCleanup(p.stop)

    def test_stuck_navigation_retries_on_a_new_tab(self) -> None:
        _FakeSession.stuck = 1
        out = cdp_direct.run("print(new_tab('https://appstoreconnect.apple.com/apps'))", "http://x")
        s = _FakeSession.last
        self.assertEqual(s.navigations, 2)
        self.assertEqual(s.closed, ["tab1"])  # the stuck tab is closed
        self.assertEqual(s.tabs, ["tab2"])
        self.assertIn("tab1", out)  # the returned id follows browser-harness's contract

    def test_two_stuck_navigations_give_a_clear_error(self) -> None:
        _FakeSession.stuck = 2
        with self.assertRaisesRegex(cdp_direct.CdpError, "store_browser_session"):
            cdp_direct.run("new_tab('https://appstoreconnect.apple.com/apps')", "http://x")
        self.assertEqual(_FakeSession.last.navigations, 2)

    def test_network_error_on_navigation_is_not_swallowed(self) -> None:
        original = _FakeSession.send

        def with_error(self, method, session_id=None, timeout=120, **params):
            if method == "Page.navigate":
                return {"frameId": "f", "errorText": "net::ERR_NAME_NOT_RESOLVED"}
            return original(self, method, session_id, timeout, **params)

        with patch.object(_FakeSession, "send", with_error):
            with self.assertRaisesRegex(cdp_direct.CdpError, "ERR_NAME_NOT_RESOLVED"):
                cdp_direct.run("new_tab('https://example.invalid')", "http://x")

    def test_system_exit_from_the_script_becomes_cdp_error(self) -> None:
        with self.assertRaises(cdp_direct.CdpError) as ctx:
            cdp_direct.run("print('step 1'); raise SystemExit('ERROR: button did not appear')", "http://x")
        self.assertIn("step 1", str(ctx.exception))
        self.assertIn("\nERROR: button did not appear", str(ctx.exception))

    def test_system_exit_zero_is_a_normal_exit(self) -> None:
        self.assertIn("done", cdp_direct.run("print('done'); raise SystemExit(0)", "http://x"))

    def test_script_exception_carries_output_and_traceback(self) -> None:
        with self.assertRaises(cdp_direct.CdpError) as ctx:
            cdp_direct.run("print('before'); 1/0", "http://x")
        self.assertIn("before", str(ctx.exception))
        self.assertIn("ZeroDivisionError", str(ctx.exception))

    def test_prelude_imports_resolve_to_the_cdp_helpers(self) -> None:
        before = sys.modules.get("browser_harness.helpers")
        out = cdp_direct.run(
            "from browser_harness.helpers import cdp, current_tab\nprint(current_tab()['targetId'])",
            "http://x",
        )
        self.assertEqual(out.strip(), "tab1")
        self.assertIs(sys.modules.get("browser_harness.helpers"), before)

    def test_connection_failure_is_a_cdp_error(self) -> None:
        with patch.object(cdp_direct, "_Session", side_effect=OSError("connection refused")):
            with self.assertRaisesRegex(cdp_direct.CdpError, "connection refused"):
                cdp_direct.run("print(1)", "http://x")


if __name__ == "__main__":
    unittest.main()
